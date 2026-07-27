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

from sqlalchemy import func, select

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
import numpy as np

from app.executor.base import BaseExecutor
from app.executor.live import LiveExecutor
from app.executor.paper import PaperExecutor
from app.risk.manager import RiskManager, RiskRejected
from app.risk.trailing import TrailingConfig
from app.strategy.indicators import atr as atr_series
from app.strategy.mtf_confluence import MTFConfluenceStrategy
from app.strategy.range_breakout import RangeBreakoutStrategy
from app.strategy.types import SignalProposal, StrategyContext
from app.telegram import notifier as tg_notifier

log = get_logger(__name__)


def _json_safe(obj):
    """Recursively convert numpy / pandas scalar types to plain Python.

    The Signal.diagnostics column is stored as JSON. SQLAlchemy's JSON
    encoder rejects numpy scalars (bool_, int64, float64) with 'Object of
    type X is not JSON serializable', which was silently failing every
    fired signal insert. This walker converts everything to primitives
    before we hand the dict to the ORM.
    """
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    return obj


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
        # Populated when reconcile / executor bookkeeping fails in LIVE
        # mode (bad API key, IP not whitelisted, futures not enabled,
        # transient 5xx). Surfaced via /api/scanner/diagnostics so the
        # frontend can show a clear banner instead of silently
        # producing an empty panel.
        self._last_reconcile_error: str | None = None
        # Per-symbol cool-down after an exec-failed signal, so a bug (or
        # a transient exchange error, or a broken precision map) doesn't
        # cause the same signal to re-fire every tick. Stores a
        # ``time.monotonic()`` expiry timestamp; if a symbol has an
        # entry in this dict that's still in the future, ``_scan_one``
        # skips it. Cleared automatically when the deadline passes.
        self._symbol_cooldown_until: dict[str, float] = {}
        # Circuit breaker: track recent exec_failed events across ALL
        # symbols. If we exceed the threshold within the window we
        # auto-disable the scanner (equivalent to the user clicking
        # "Scanner OFF") and send a Telegram alert so they know why.
        # This is the last line of defence against fee bleed when a
        # new Binance policy or a bug breaks every execution.
        self._recent_exec_failures: list[float] = []  # unix-epoch seconds

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
        #
        # We wrap this in its OWN try/except so a failure here (e.g. Binance
        # API key wrong / IP not whitelisted in LIVE mode, or transient
        # HTTP timeout) never blocks the scanning half of the tick.
        # Before this guard, a single 401 from get_open_orders would
        # kill the whole tick and leave the diagnostics panel empty —
        # symptom the user hit when switching PAPER → LIVE.
        if cfg.market_mode == MarketMode.CRYPTO:
            try:
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
                    # Telegram notify — TP1/TP2/SL/manual close etc. Fire and
                    # forget: never let Telegram outages block executor logic.
                    await _notify_trade(trade, cfg)
                # Clear any previous reconciliation error now that a
                # tick succeeded.
                self._last_reconcile_error = None
            except Exception as e:  # noqa: BLE001
                # Log + remember but DON'T re-raise. Scanning must proceed.
                err_msg = f"{type(e).__name__}: {e}"
                log.warning(
                    "scanner.reconcile_failed",
                    mode=cfg.trading_mode.value,
                    error=err_msg,
                )
                self._last_reconcile_error = err_msg

        # 2) If scanner disabled, we stop here.
        if not cfg.scanner_enabled:
            return

        # Build the list of strategies to try this tick. Each is a
        # separate class implementing evaluate(); the scanner tries
        # them in order and uses whichever fires first. MTF Confluence
        # is checked first because it's more selective; Range Breakout
        # picks up post-consolidation setups that MTF misses.
        ctx = self._ctx_from_config(cfg)
        strategies: list = []
        if cfg.mtf_confluence_enabled:
            strategies.append(MTFConfluenceStrategy(ctx))
        if cfg.range_breakout_enabled:
            strategies.append(RangeBreakoutStrategy(ctx))
        if not strategies:
            log.debug("scanner.no_strategies_enabled")
            return

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

        import time as _time
        for symbol in watchlist:
            if self._stop.is_set():
                break
            # Skip symbols that recently exec-failed to prevent the same
            # broken signal firing every tick. Cool-down is 5 minutes;
            # a config edit / bug fix is short enough for the user to
            # just wait it out.
            expires = self._symbol_cooldown_until.get(symbol)
            if expires is not None:
                if _time.monotonic() < expires:
                    diag = self._diagnostics.get(symbol) or {}
                    diag["stage"] = "cooldown"
                    diag["reason"] = (
                        f"skipping — cooldown "
                        f"{int(expires - _time.monotonic())}s "
                        "after previous exec_failed"
                    )
                    self._diagnostics[symbol] = diag
                    continue
                del self._symbol_cooldown_until[symbol]

            try:
                await self._scan_one(symbol, cfg, strategies, source)
            except Exception as e:  # noqa: BLE001
                log.warning("scanner.symbol_error", symbol=symbol, error=str(e))
                # If the strategy already advanced diag to 'fired' but the
                # sizing / executor path blew up, reflect that in the panel
                # so we don't leave a misleading FIRED state without a
                # matching signal in the DB.
                cur = self._diagnostics.get(symbol)
                if cur and cur.get("stage") == "fired":
                    cur["stage"] = "exec_failed"
                    cur["reason"] = f"exception: {e}"
                # Extend cool-down to 15 minutes so we don't spam even
                # after the underlying bug is fixed — plenty of time
                # for the user to notice and react.
                self._symbol_cooldown_until[symbol] = _time.monotonic() + 900
                await self._maybe_trip_breaker(cfg, symbol, str(e))

    # ----------------------------------------------------------------------
    # One-symbol pass
    # ----------------------------------------------------------------------

    async def _scan_one(
        self,
        symbol: str,
        cfg: UserConfig,
        strategies: list,
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

        # Try each enabled strategy; first hit wins. Diagnostics per
        # strategy are aggregated so the UI can show why each one
        # didn't fire this tick.
        proposal: SignalProposal | None = None
        diag: dict = {}
        per_strategy_diag: list[dict] = []
        for strat in strategies:
            strat_name = getattr(strat, "STRATEGY_NAME", type(strat).__name__)
            p, d = strat.evaluate(
                symbol,
                bias_df=bias_df,
                setup_df=setup_df,
                entry_df=entry_df,
                bias_tf=cfg.bias_tf,
                setup_tf=cfg.setup_tf,
                entry_tf=cfg.entry_tf,
            )
            per_strategy_diag.append(
                {
                    "strategy": strat_name,
                    "stage": d.get("stage"),
                    "reason": d.get("reason"),
                }
            )
            if p is not None:
                proposal = p
                # Merge the winning strategy's diagnostics as the
                # top-level view + tag it.
                diag = {**d}
                diag["fired_by"] = strat_name
                break

        # If no strategy fired, keep the last one's diag as the panel
        # summary (so users see the closest-to-firing reasoning) plus
        # the full per-strategy breakdown.
        if proposal is None and per_strategy_diag:
            diag = per_strategy_diag[-1].copy()
        diag["ts"] = datetime.now(tz=timezone.utc).isoformat()
        diag["market"] = cfg.market_mode.value
        diag["strategies"] = per_strategy_diag
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
            await _notify_signal(signal, cfg)
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

        # Push the SIGNAL notification immediately, BEFORE we try to
        # execute. Notifying only on execution-success (the old
        # behaviour) hid every signal whose SL placement failed —
        # exactly the case that just burned fees in DOGE. Users expect
        # to see the setup regardless of what the executor manages to
        # do about it.
        await _notify_signal(signal, cfg)

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
            # Signal notification was already sent right after _save_signal
            # above. Trade-opened notification comes from _notify_trade
            # via the reconcile loop.
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
            rr_tp3=cfg.rr_tp3,
            adx_period=cfg.adx_period,
            adx_min=cfg.adx_min,
            volume_mult=cfg.volume_mult,
            rb_lookback=cfg.rb_lookback,
            rb_max_range_pct=cfg.rb_max_range_pct,
            rb_atr_squeeze_ratio=cfg.rb_atr_squeeze_ratio,
            rb_breakout_buffer=cfg.rb_breakout_buffer,
            rb_measured_move_tp1=cfg.rb_measured_move_tp1,
            rb_measured_move_tp2=cfg.rb_measured_move_tp2,
        )

    async def _maybe_trip_breaker(
        self, cfg: UserConfig, symbol: str, error_text: str
    ) -> None:
        """Track exec failures and auto-disable the scanner if too many pile up.

        Threshold: 3 exec_failed events across ANY symbols within a
        10-minute window trips the breaker. When it trips we:

        - Flip cfg.scanner_enabled to False in the DB so the next tick
          returns early and no new signals fire.
        - Send a Telegram alert (if configured) with the last error so
          the user knows something went wrong and can investigate.

        This is the SAFETY NET after the per-symbol cool-down. Losing
        \$0.06 in fees per broken signal adds up; killing the scanner
        after 3 in a row limits the damage to ~\$0.18.
        """
        import time as _time
        now = _time.time()
        window = 600.0  # 10 minutes
        limit = 3
        self._recent_exec_failures = [
            t for t in self._recent_exec_failures if now - t < window
        ]
        self._recent_exec_failures.append(now)
        if len(self._recent_exec_failures) < limit:
            return

        log.error(
            "scanner.circuit_breaker_tripped",
            failures_in_window=len(self._recent_exec_failures),
            last_symbol=symbol,
            last_error=error_text,
        )

        # Persist scanner_enabled=False so a bot restart doesn't
        # silently resume the bleed.
        async with session_scope() as session:
            row = (await session.execute(select(UserConfig).limit(1))).scalars().first()
            if row is not None and row.scanner_enabled:
                row.scanner_enabled = False
                session.add(row)

        # Telegram alarm.
        try:
            await tg_notifier.send_message(
                cfg,
                "🚨 *Scanner auto-disabled* 🚨\n\n"
                f"Detected {limit} execution failures within "
                f"{int(window / 60)} minutes. To prevent fee bleed, "
                "the scanner has been turned off. Latest error:\n\n"
                f"`{error_text[:300]}`\n\n"
                "Investigate the error, then re-enable the scanner "
                "from the dashboard when ready.",
            )
        except Exception as e:  # noqa: BLE001
            log.warning("scanner.breaker_notify_failed", error=str(e))

        # Reset the failure count so we don't fire another alarm the
        # instant the user re-enables the scanner.
        self._recent_exec_failures.clear()

    def _executor_for(self, cfg: UserConfig) -> BaseExecutor:
        if cfg.trading_mode == TradingMode.LIVE:
            # Reuse the crypto data source's REST client when possible so
            # we don't spin up an extra httpx.AsyncClient per tick.
            # Fall back to a fresh one if the current source isn't Binance
            # (e.g. forex mode was active earlier this tick).
            rest = None
            src = self._source
            if isinstance(src, BinanceDataSource):
                rest = src._rest  # noqa: SLF001
            return LiveExecutor(rest=rest)
        return PaperExecutor()

    async def _equity_for(self, cfg: UserConfig, source: MarketDataSource) -> float:
        if cfg.trading_mode == TradingMode.PAPER:
            return await self._paper_equity(cfg)
        # Live mode is crypto-only; use the underlying Binance REST client.
        if not isinstance(source, BinanceDataSource):
            return await self._paper_equity(cfg)
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

    @staticmethod
    async def _paper_equity(cfg: UserConfig) -> float:
        """Return AVAILABLE paper equity for opening a NEW trade.

            available = starting_balance
                      + realized P&L across all paper trades
                      - margin locked in currently OPEN paper positions

        This matches how Binance actually works: money already committed
        as margin on open positions is not usable to size a new position.
        Without this correction, three concurrent trades with a $10 wallet
        could each request $3 margin each from the "full" $10 → total $9
        deployed, but each sizing decision saw the whole $10 as free, so
        the total could easily exceed the wallet and produce unrealistic
        results.

        Never returns a negative number — clamped at 0 so the risk manager
        rejects new trades cleanly once the pot is exhausted.
        """
        async with session_scope() as session:
            # 1) Realized P&L (wins + losses already booked, incl. partial TP1)
            pnl_stmt = select(
                func.coalesce(func.sum(Trade.realized_pnl_usdt), 0.0)
            ).where(Trade.mode == TradingMode.PAPER)
            realized = float(
                (await session.execute(pnl_stmt)).scalar_one() or 0.0
            )

            # 2) Margin currently locked in open positions
            open_stmt = select(Trade).where(
                Trade.mode == TradingMode.PAPER,
                Trade.status.in_(
                    [SignalStatus.OPEN, SignalStatus.TP1_HIT]
                ),
            )
            open_trades = list(
                (await session.execute(open_stmt)).scalars().all()
            )

        locked_margin = 0.0
        for t in open_trades:
            status_val = (
                t.status.value if hasattr(t.status, "value") else str(t.status)
            )
            remaining_qty = (
                t.quantity / 2.0 if status_val == "TP1_HIT" else t.quantity
            )
            locked_margin += (t.entry_price * remaining_qty) / max(t.leverage, 1)

        wallet = float(cfg.paper_balance) + realized
        available = wallet - locked_margin
        return max(available, 0.0)

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
            take_profit_3=proposal.take_profit_3 or None,
            leverage=cfg.leverage,
            quantity=0.0,
            risk_amount_usdt=0.0,
            confidence=proposal.confidence,
            reason=proposal.reason + f" | CANCELLED: {reason}",
            diagnostics=_json_safe(proposal.diagnostics),
            strategy=proposal.strategy,
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
            take_profit_3=proposal.take_profit_3 or None,
            leverage=cfg.leverage,
            quantity=0.0,
            risk_amount_usdt=0.0,
            confidence=proposal.confidence,
            reason=proposal.reason + " | forex signal (manual execution)",
            diagnostics=_json_safe(proposal.diagnostics),
            strategy=proposal.strategy,
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
            take_profit_3=proposal.take_profit_3 or None,
            leverage=sized.leverage,
            quantity=sized.quantity,
            risk_amount_usdt=sized.risk_usdt,
            confidence=proposal.confidence,
            reason=proposal.reason,
            diagnostics=_json_safe(
                {
                    **proposal.diagnostics,
                    "sized": asdict(sized)
                    if hasattr(sized, "__dataclass_fields__")
                    else {},
                }
            ),
            strategy=proposal.strategy,
        )
        async with session_scope() as session:
            session.add(signal)
            await session.flush()
        return signal


# --------------------------------------------------------------------------
# Serialization helpers for WS events (kept small + JSON-safe)
# --------------------------------------------------------------------------


# ==========================================================================
# Circuit breaker — bolted onto ScannerEngine below via a mixin-style method.
# ==========================================================================


async def _notify_signal(signal: Signal, cfg: UserConfig) -> None:
    """Send a Telegram alert for a newly fired signal. Silent on failure."""
    if not cfg.telegram_enabled or not cfg.telegram_notify_signals:
        return
    try:
        await tg_notifier.send_message(cfg, tg_notifier.render_signal(signal))
    except Exception as e:  # noqa: BLE001 — never crash caller
        log.warning("notify.signal_failed", error=str(e))


async def _notify_trade(trade: Trade, cfg: UserConfig) -> None:
    """Send a Telegram alert for a trade transition (TP/SL/manual/trail)."""
    if not cfg.telegram_enabled or not cfg.telegram_notify_trades:
        return
    # Map SignalStatus → event label. TP1_HIT fires while still open;
    # CLOSED_* is a terminal transition.
    status_val = trade.status.value if hasattr(trade.status, "value") else str(trade.status)
    event = {
        "OPEN": "OPEN",
        "TP1_HIT": "TP1_HIT",
        "CLOSED_TP": "TP2",
        "CLOSED_SL": "SL",
        "CLOSED_MANUAL": "MANUAL",
    }.get(status_val, status_val)
    try:
        await tg_notifier.send_message(cfg, tg_notifier.render_trade_update(trade, event))
    except Exception as e:  # noqa: BLE001
        log.warning("notify.trade_failed", error=str(e))


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
        "take_profit_3": getattr(s, "take_profit_3", None),
        "leverage": s.leverage,
        "quantity": s.quantity,
        "risk_amount_usdt": s.risk_amount_usdt,
        "confidence": s.confidence,
        "reason": s.reason,
        "trade_id": s.trade_id,
        "strategy": getattr(s, "strategy", "mtf_confluence"),
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
