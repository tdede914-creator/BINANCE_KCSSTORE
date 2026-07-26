"""24/7 signal scanner engine.

Responsibilities
----------------
1. Read the current :class:`UserConfig` from the DB every tick.
2. If ``scanner_enabled`` is False, sleep and retry.
3. For each symbol in the watchlist:
   a. Fetch klines for bias / setup / entry timeframes.
   b. Run :class:`MTFConfluenceStrategy`.
   c. If a proposal is returned:
      - Ensure not already in an open trade for the same symbol.
      - Ensure max_concurrent_positions is respected.
      - Compute size via :class:`RiskManager` (equity = paper_balance or
        live USDT balance).
      - Execute via the correct executor.
      - Persist the :class:`Signal` row and publish to :data:`event_bus`.
4. Periodically call ``executor.check_open_trades(current_prices)`` so
   paper trades transition (SL/TP hit) and live trades reconcile with
   Binance state.

The engine is designed to run as a single asyncio task inside FastAPI's
lifespan. It gracefully handles config changes between ticks — you can
enable/disable, change watchlist, change timeframes without restart.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime, timezone

from sqlalchemy import select

from app.api.ws import event_bus
from app.core.config import settings
from app.core.logging import get_logger
from app.core.security import decrypt_secret
from app.datasource.base import MarketDataSource
from app.datasource.binance_source import BinanceDataSource
from app.datasource.factory import get_data_source
from app.db.database import session_scope
from app.db.models import (
    MarketMode,
    Signal,
    SignalStatus,
    Trade,
    TradingMode,
    UserConfig,
)
from app.executor.base import BaseExecutor
from app.executor.live import LiveExecutor
from app.executor.paper import PaperExecutor
from app.risk.manager import RiskManager, RiskRejected
from app.risk.trailing import TrailingConfig
from app.strategy.indicators import atr as atr_series
from app.strategy.mtf_confluence import MTFConfluenceStrategy
from app.strategy.types import SignalProposal, StrategyContext

log = get_logger(__name__)


class ScannerEngine:
    """Async scan loop. One instance per process."""

    def __init__(self) -> None:
        self._stop = asyncio.Event()
        # Current data source (rebuilt whenever market_mode changes).
        self._source: MarketDataSource | None = None
        self._source_mode: MarketMode | None = None
        # Cache filters per symbol to avoid re-fetching exchangeInfo every tick.
        self._filter_cache: dict[str, dict] = {}
        # In-memory diagnostics for the most recent evaluation of each
        # watchlist symbol. Exposed via GET /api/scanner/diagnostics so the
        # UI can show *why* a signal did / didn't fire this tick.
        self._diagnostics: dict[str, dict] = {}
        self._last_tick_ts: datetime | None = None
        self._last_tick_market: MarketMode | None = None

    def stop(self) -> None:
        self._stop.set()

    async def _ensure_source(self, mode: MarketMode) -> MarketDataSource | None:
        """Return the data source that matches ``mode``, creating it on
        demand and disposing of the previous one when the market changes.

        Returns None if the source can't be built (e.g. missing
        TwelveData API key for FOREX). Callers should skip the tick in
        that case rather than crash.
        """
        if self._source is not None and self._source_mode == mode:
            return self._source

        # Dispose of the old source (if any) before creating a new one.
        if self._source is not None:
            try:
                await self._source.close()
            except Exception as e:  # noqa: BLE001
                log.warning("scanner.source_close_error", error=str(e))
            self._source = None
            self._source_mode = None
            self._filter_cache = {}

        try:
            self._source = await get_data_source(mode)
            self._source_mode = mode
        except RuntimeError as e:
            log.warning("scanner.data_source_unavailable", error=str(e), mode=mode.value)
            return None
        return self._source

    # ----------------------------------------------------------------------
    # Main loop
    # ----------------------------------------------------------------------

    async def run_forever(self) -> None:
        log.info("scanner.starting")
        try:
            while not self._stop.is_set():
                try:
                    await self._tick()
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    log.error("scanner.tick_error", error=str(e))
                await asyncio.wait(
                    [asyncio.create_task(self._stop.wait())],
                    timeout=settings.SCAN_INTERVAL_SECONDS,
                )
        finally:
            if self._source is not None:
                try:
                    await self._source.close()
                except Exception:  # noqa: BLE001
                    pass
            log.info("scanner.stopped")

    async def _tick(self) -> None:
        cfg = await self._load_config()
        self._last_tick_ts = datetime.now(tz=timezone.utc)
        self._last_tick_market = cfg.market_mode

        # Pick / refresh the data source for the current market mode.
        source = await self._ensure_source(cfg.market_mode)
        if source is None:
            # Missing credentials for the selected market — noop this tick.
            return

        # 1) Reconcile open trades in CRYPTO mode. FOREX mode is signal-only
        # (no executor), so there's nothing to reconcile.
        if cfg.market_mode == MarketMode.CRYPTO:
            prices = await self._fetch_current_prices(
                source, self._all_relevant_symbols(cfg)
            )
            changed = await self._executor_for(cfg).check_open_trades(prices)
            for trade in changed:
                await event_bus.publish(
                    {
                        "type": "trade.update",
                        "data": _trade_dict(trade),
                        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                    }
                )

        # 2) If scanner disabled, we stop here.
        if not cfg.scanner_enabled:
            return

        strategy = MTFConfluenceStrategy(self._ctx_from_config(cfg))
        watchlist_csv = (
            cfg.forex_watchlist_csv
            if cfg.market_mode == MarketMode.FOREX
            else cfg.watchlist_csv
        )
        watchlist = [s for s in watchlist_csv.split(",") if s]

        # Concurrency guard only applies to crypto (paper/live executions).
        if cfg.market_mode == MarketMode.CRYPTO:
            open_count = await self._count_open_trades(cfg.trading_mode)
            if open_count >= cfg.max_concurrent_positions:
                log.debug(
                    "scanner.max_positions_reached",
                    open_count=open_count,
                    cap=cfg.max_concurrent_positions,
                )
                return

        for symbol in watchlist:
            if self._stop.is_set():
                break
            try:
                await self._scan_one(symbol, cfg, strategy, source)
            except Exception as e:  # noqa: BLE001
                log.warning("scanner.symbol_error", symbol=symbol, error=str(e))

    # ----------------------------------------------------------------------
    # One-symbol pass
    # ----------------------------------------------------------------------

    async def _scan_one(
        self,
        symbol: str,
        cfg: UserConfig,
        strategy: MTFConfluenceStrategy,
        source: MarketDataSource,
    ) -> None:
        # Skip if there's already an open trade for this symbol (crypto only).
        if cfg.market_mode == MarketMode.CRYPTO and await self._has_open_trade(
            symbol, cfg.trading_mode
        ):
            return

        # Fetch klines for the 3 timeframes.
        bias_df, setup_df, entry_df = await asyncio.gather(
            source.get_klines(symbol, cfg.bias_tf, limit=300),
            source.get_klines(symbol, cfg.setup_tf, limit=200),
            source.get_klines(symbol, cfg.entry_tf, limit=200),
        )

        proposal, diag = strategy.evaluate(
            symbol,
            bias_df=bias_df,
            setup_df=setup_df,
            entry_df=entry_df,
            bias_tf=cfg.bias_tf,
            setup_tf=cfg.setup_tf,
            entry_tf=cfg.entry_tf,
        )
        diag["ts"] = datetime.now(tz=timezone.utc).isoformat()
        diag["market"] = cfg.market_mode.value
        self._diagnostics[symbol] = diag

        if proposal is None:
            log.info(
                "scanner.no_signal",
                symbol=symbol,
                stage=diag.get("stage"),
                reason=diag.get("reason"),
            )
            return

        # -------------- FOREX: signals-only path --------------
        if cfg.market_mode == MarketMode.FOREX:
            signal = await self._save_forex_signal(proposal, cfg)
            log.info(
                "scanner.signal.forex",
                symbol=symbol,
                side=proposal.side.value,
                confidence=proposal.confidence,
            )
            await event_bus.publish(
                {
                    "type": "signal.new",
                    "data": _signal_dict(signal),
                    "trade": None,
                    "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                }
            )
            return

        # -------------- CRYPTO: full paper/live execution --------------
        # Get symbol filters (cached)
        if symbol not in self._filter_cache:
            filters = await source.get_symbol_filters(symbol)
            if filters is None:
                log.warning("scanner.no_filters", symbol=symbol)
                return
            self._filter_cache[symbol] = filters
        filters = self._filter_cache[symbol]

        equity = await self._equity_for(cfg, source)

        try:
            sized = RiskManager(
                equity_usdt=equity,
                risk_per_trade_pct=cfg.risk_per_trade_pct,
                leverage=cfg.leverage,
                symbol_filters=filters,
            ).size(proposal)
        except RiskRejected as e:
            # Persist a CANCELLED signal so the user sees WHY a would-be
            # signal never became a trade. Without this, risk-rejected
            # setups vanish silently and the win-rate stats can look
            # unfairly bad or hide the fact that the strategy IS firing.
            log.info("scanner.risk_rejected", symbol=symbol, reason=str(e))
            cancelled = await self._save_cancelled_signal(
                proposal, cfg, reason=f"risk_rejected: {e}"
            )
            self._diagnostics[symbol]["stage"] = "risk_rejected"
            self._diagnostics[symbol]["reason"] = str(e)
            await event_bus.publish(
                {
                    "type": "signal.new",
                    "data": _signal_dict(cancelled),
                    "trade": None,
                    "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                }
            )
            return

        # Snapshot entry-TF ATR for trailing (last valid value).
        atr_snapshot = self._entry_atr_snapshot(entry_df, cfg.atr_period)
        trailing_cfg = TrailingConfig.from_user_config(cfg, atr_snapshot=atr_snapshot)

        # Persist signal + execute
        signal = await self._save_signal(proposal, sized, cfg)

        executor = self._executor_for(cfg)
        result = await executor.open_trade(
            sized,
            signal_id=signal.id,
            trailing=trailing_cfg,
        )

        if result.ok and result.trade is not None:
            async with session_scope() as session:
                s = await session.get(Signal, signal.id)
                if s is not None:
                    s.status = SignalStatus.OPEN
                    s.trade_id = result.trade.id
                    session.add(s)
            signal.status = SignalStatus.OPEN
            signal.trade_id = result.trade.id
            self._diagnostics[symbol]["stage"] = "executed"

            log.info(
                "scanner.signal.executed",
                symbol=symbol,
                side=proposal.side.value,
                mode=cfg.trading_mode.value,
                confidence=proposal.confidence,
            )
            await event_bus.publish(
                {
                    "type": "signal.new",
                    "data": _signal_dict(signal),
                    "trade": _trade_dict(result.trade),
                    "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                }
            )
        else:
            async with session_scope() as session:
                s = await session.get(Signal, signal.id)
                if s is not None:
                    s.status = SignalStatus.CANCELLED
                    s.reason = (s.reason or "") + f" | exec_error: {result.error}"
                    session.add(s)
            self._diagnostics[symbol]["stage"] = "exec_failed"
            self._diagnostics[symbol]["reason"] = str(result.error)
            log.warning(
                "scanner.execution_failed",
                symbol=symbol,
                error=result.error,
            )
            # Broadcast so the failed signal shows up in Recent Signals too.
            await event_bus.publish(
                {
                    "type": "signal.new",
                    "data": _signal_dict(signal),
                    "trade": None,
                    "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                }
            )

    # ----------------------------------------------------------------------
    # Helpers
    # ----------------------------------------------------------------------

    @staticmethod
    def _entry_atr_snapshot(entry_df, period: int) -> float:
        """Compute the latest ATR value from the entry-TF DataFrame.

        Used as a stable reference for ATR-based trailing so the trail
        distance doesn't drift as market volatility changes.
        """
        try:
            series = atr_series(entry_df, period=period).dropna()
            return float(series.iloc[-1]) if len(series) else 0.0
        except Exception:  # noqa: BLE001
            return 0.0

    async def _load_config(self) -> UserConfig:
        async with session_scope() as session:
            cfg = await session.get(UserConfig, 1)
            if cfg is None:
                cfg = UserConfig(id=1)
                session.add(cfg)
            return cfg

    @staticmethod
    def _ctx_from_config(cfg: UserConfig) -> StrategyContext:
        return StrategyContext(
            ema_fast=cfg.ema_fast,
            ema_slow=cfg.ema_slow,
            ema_trigger=cfg.ema_trigger,
            rsi_period=cfg.rsi_period,
            rsi_long_max=cfg.rsi_long_max,
            rsi_short_min=cfg.rsi_short_min,
            atr_period=cfg.atr_period,
            atr_sl_mult=cfg.atr_sl_mult,
            rr_tp1=cfg.rr_tp1,
            rr_tp2=cfg.rr_tp2,
        )

    def _executor_for(self, cfg: UserConfig) -> BaseExecutor:
        if cfg.trading_mode == TradingMode.LIVE:
            return LiveExecutor(rest=self._rest)
        return PaperExecutor()

    async def _equity_for(self, cfg: UserConfig, source: MarketDataSource) -> float:
        if cfg.trading_mode == TradingMode.PAPER:
            return float(cfg.paper_balance)
        # Live mode is crypto-only; use the underlying Binance REST client.
        if not isinstance(source, BinanceDataSource):
            return float(cfg.paper_balance)
        if not cfg.binance_api_key_enc:
            log.warning("scanner.live_no_credentials")
            return 0.0
        try:
            key = decrypt_secret(cfg.binance_api_key_enc)
            secret = decrypt_secret(cfg.binance_api_secret_enc)
            return await source._rest.get_balance_usdt(key, secret)  # noqa: SLF001
        except Exception as e:  # noqa: BLE001
            log.error("scanner.equity_fetch_failed", error=str(e))
            return 0.0

    def _all_relevant_symbols(self, cfg: UserConfig) -> list[str]:
        csv = (
            cfg.forex_watchlist_csv
            if cfg.market_mode == MarketMode.FOREX
            else cfg.watchlist_csv
        )
        watchlist = {s for s in csv.split(",") if s}
        return sorted(watchlist)

    async def _fetch_current_prices(
        self,
        source: MarketDataSource,
        symbols: list[str],
    ) -> dict[str, float]:
        if not symbols:
            return {}

        async def _one(sym: str) -> tuple[str, float | None]:
            try:
                return sym, await source.get_ticker_price(sym)
            except Exception as e:  # noqa: BLE001
                log.debug("scanner.price_fetch_failed", symbol=sym, error=str(e))
                return sym, None

        results = await asyncio.gather(*(_one(s) for s in symbols))
        return {s: p for s, p in results if p is not None}

    async def _count_open_trades(self, mode: TradingMode) -> int:
        async with session_scope() as session:
            rows = await session.execute(
                select(Trade).where(
                    Trade.mode == mode,
                    Trade.status.in_([SignalStatus.OPEN, SignalStatus.TP1_HIT]),
                )
            )
            return len(list(rows.scalars().all()))

    async def _has_open_trade(self, symbol: str, mode: TradingMode) -> bool:
        async with session_scope() as session:
            rows = await session.execute(
                select(Trade).where(
                    Trade.symbol == symbol,
                    Trade.mode == mode,
                    Trade.status.in_([SignalStatus.OPEN, SignalStatus.TP1_HIT]),
                )
            )
            return rows.first() is not None

    async def _save_cancelled_signal(
        self,
        proposal: SignalProposal,
        cfg: UserConfig,
        reason: str,
    ) -> Signal:
        """Save a signal that fired the strategy gates but got rejected
        downstream (risk manager, execution). Kept so the user can see the
        full history of *intended* signals, not just executed ones."""
        signal = Signal(
            symbol=proposal.symbol,
            side=proposal.side.value,  # type: ignore[arg-type]
            status=SignalStatus.CANCELLED,
            mode=cfg.trading_mode,
            bias_tf=proposal.bias_tf,
            setup_tf=proposal.setup_tf,
            entry_tf=proposal.entry_tf,
            entry_price=proposal.entry_price,
            stop_loss=proposal.stop_loss,
            take_profit_1=proposal.take_profit_1,
            take_profit_2=proposal.take_profit_2,
            leverage=cfg.leverage,
            quantity=0.0,
            risk_amount_usdt=0.0,
            confidence=proposal.confidence,
            reason=proposal.reason + f" | CANCELLED: {reason}",
            diagnostics=proposal.diagnostics,
        )
        async with session_scope() as session:
            session.add(signal)
            await session.flush()
        return signal

    async def _save_forex_signal(
        self,
        proposal: SignalProposal,
        cfg: UserConfig,
    ) -> Signal:
        """Persist a forex signal (no execution, quantity/risk are zero)."""
        signal = Signal(
            symbol=proposal.symbol,
            side=proposal.side.value,  # type: ignore[arg-type]
            status=SignalStatus.PENDING,  # forex signals stay PENDING (no trade)
            mode=cfg.trading_mode,
            bias_tf=proposal.bias_tf,
            setup_tf=proposal.setup_tf,
            entry_tf=proposal.entry_tf,
            entry_price=proposal.entry_price,
            stop_loss=proposal.stop_loss,
            take_profit_1=proposal.take_profit_1,
            take_profit_2=proposal.take_profit_2,
            leverage=cfg.leverage,
            quantity=0.0,
            risk_amount_usdt=0.0,
            confidence=proposal.confidence,
            reason=proposal.reason + " | forex signal (manual execution)",
            diagnostics=proposal.diagnostics,
        )
        async with session_scope() as session:
            session.add(signal)
            await session.flush()
        return signal

    async def _save_signal(
        self,
        proposal: SignalProposal,
        sized,
        cfg: UserConfig,
    ) -> Signal:
        signal = Signal(
            symbol=proposal.symbol,
            side=proposal.side.value,  # type: ignore[arg-type]
            status=SignalStatus.PENDING,
            mode=cfg.trading_mode,
            bias_tf=proposal.bias_tf,
            setup_tf=proposal.setup_tf,
            entry_tf=proposal.entry_tf,
            entry_price=sized.entry_price,
            stop_loss=sized.stop_loss,
            take_profit_1=sized.take_profit_1,
            take_profit_2=sized.take_profit_2,
            leverage=sized.leverage,
            quantity=sized.quantity,
            risk_amount_usdt=sized.risk_usdt,
            confidence=proposal.confidence,
            reason=proposal.reason,
            diagnostics={
                **proposal.diagnostics,
                "sized": asdict(sized) if hasattr(sized, "__dataclass_fields__") else {},
            },
        )
        async with session_scope() as session:
            session.add(signal)
            await session.flush()
        return signal


# --------------------------------------------------------------------------
# Serialization helpers for WS events (kept small + JSON-safe)
# --------------------------------------------------------------------------


def _signal_dict(s: Signal) -> dict:
    return {
        "id": s.id,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "symbol": s.symbol,
        "side": s.side.value if hasattr(s.side, "value") else str(s.side),
        "status": s.status.value if hasattr(s.status, "value") else str(s.status),
        "mode": s.mode.value if hasattr(s.mode, "value") else str(s.mode),
        "entry_tf": s.entry_tf,
        "entry_price": s.entry_price,
        "stop_loss": s.stop_loss,
        "take_profit_1": s.take_profit_1,
        "take_profit_2": s.take_profit_2,
        "leverage": s.leverage,
        "quantity": s.quantity,
        "risk_amount_usdt": s.risk_amount_usdt,
        "confidence": s.confidence,
        "reason": s.reason,
        "trade_id": s.trade_id,
    }


def _trade_dict(t: Trade) -> dict:
    return {
        "id": t.id,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "closed_at": t.closed_at.isoformat() if t.closed_at else None,
        "symbol": t.symbol,
        "side": t.side.value if hasattr(t.side, "value") else str(t.side),
        "status": t.status.value if hasattr(t.status, "value") else str(t.status),
        "mode": t.mode.value if hasattr(t.mode, "value") else str(t.mode),
        "leverage": t.leverage,
        "entry_price": t.entry_price,
        "quantity": t.quantity,
        "stop_loss": t.stop_loss,
        "take_profit_1": t.take_profit_1,
        "take_profit_2": t.take_profit_2,
        "exit_price": t.exit_price,
        "realized_pnl_usdt": t.realized_pnl_usdt,
        "realized_pnl_pct": t.realized_pnl_pct,
    }
