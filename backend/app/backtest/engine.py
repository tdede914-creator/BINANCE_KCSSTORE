"""Backtest engine — replay MTFConfluenceStrategy on historical klines.

By reusing the exact same :class:`MTFConfluenceStrategy` the live scanner
uses, results from a backtest transfer directly to what the live bot
would have done (subject to the usual caveats about slippage, funding,
and regime changes discussed in the docs).

Simulation model
----------------
- Each trade opens at the CLOSE of the candle where the signal fired.
- Subsequent candles are checked bar-by-bar against SL / TP1 / TP2:
  we use the candle's high and low to decide fills, which mirrors how
  a real market can wick to a level even if it closes elsewhere.
- SL is checked FIRST when both SL and a TP are in range in the same
  candle (whipsaw safety — matches most professional backtest engines).
- TP1 closes 50% of the position and moves SL to break-even, exactly
  like ``PaperExecutor.check_open_trades``.
- Fees: 0.05% taker per side (Binance default), booked proportionally
  on every partial close so net P&L reflects real costs.
- Equity compounds: each new signal is sized against the running
  equity, not the starting balance.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pandas as pd

from app.binance.rest import BinanceREST
from app.core.logging import get_logger
from app.strategy.indicators import enrich as _enrich_indicators
from app.strategy.mtf_confluence import MTFConfluenceStrategy
from app.strategy.range_breakout import RangeBreakoutStrategy
from app.strategy.types import Side, SignalProposal, StrategyContext

log = get_logger(__name__)

FEE_RATE = 0.0005  # 0.05% taker per side, matches PaperExecutor


# ==========================================================================
# Data classes
# ==========================================================================


@dataclass(slots=True)
class BacktestConfig:
    symbol: str
    bias_tf: str = "4h"
    setup_tf: str = "1h"
    entry_tf: str = "5m"
    days: int = 60
    initial_balance: float = 1000.0
    risk_per_trade_pct: float = 1.0
    leverage: int = 5
    strategy_ctx: StrategyContext = field(default_factory=StrategyContext)
    # Which strategies to run: any subset of
    # ("mtf_confluence", "range_breakout"). If both are listed we try
    # them in order and take whichever fires first — same rule as the
    # live scanner.
    strategies: tuple[str, ...] = ("mtf_confluence", "range_breakout")


@dataclass(slots=True)
class SimFill:
    time: datetime
    price: float
    qty: float
    reason: str          # "TP1" | "TP2" | "SL" | "EOP" (end of period)
    gross_pnl: float
    fees: float
    net_pnl: float


@dataclass(slots=True)
class SimTrade:
    open_time: datetime
    side: str            # "LONG" | "SHORT"
    entry_price: float
    quantity: float      # original quantity opened
    stop_loss: float     # current SL (may be moved to BE after TP1)
    initial_sl: float    # SL at open (for RR computation)
    take_profit_1: float
    take_profit_2: float

    remaining_qty: float = 0.0
    realized_pnl: float = 0.0
    total_fees: float = 0.0
    status: str = "OPEN"           # OPEN | TP1_HIT | CLOSED
    close_time: datetime | None = None
    close_reason: str | None = None
    fills: list[SimFill] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.remaining_qty:
            self.remaining_qty = self.quantity

    # ------------------------------------------------------------------
    # Fill helpers
    # ------------------------------------------------------------------

    def _apply_fill(
        self,
        price: float,
        qty: float,
        reason: str,
        timestamp: datetime,
    ) -> SimFill:
        entry_fee_share = self.entry_price * qty * FEE_RATE
        exit_fee = price * qty * FEE_RATE
        fees = entry_fee_share + exit_fee
        move = (price - self.entry_price) if self.side == "LONG" else (
            self.entry_price - price
        )
        gross = move * qty
        net = gross - fees

        self.realized_pnl += net
        self.total_fees += fees
        self.remaining_qty -= qty
        fill = SimFill(
            time=timestamp,
            price=price,
            qty=qty,
            reason=reason,
            gross_pnl=gross,
            fees=fees,
            net_pnl=net,
        )
        self.fills.append(fill)

        if self.remaining_qty < 1e-9:
            self.status = "CLOSED"
            self.close_time = timestamp
            self.close_reason = reason
        return fill

    # ------------------------------------------------------------------
    # Bar-level exit check
    # ------------------------------------------------------------------

    def check_bar(self, high: float, low: float, timestamp: datetime) -> None:
        """Apply SL / TP fills for a single candle's price range.

        Follows the whipsaw-safe convention: if SL and a TP are both in
        range, assume SL was hit first (worst case).
        """
        if self.status == "CLOSED":
            return

        if self.side == "LONG":
            if low <= self.stop_loss:
                self._apply_fill(
                    self.stop_loss, self.remaining_qty, "SL", timestamp
                )
                return
            if self.status == "TP1_HIT" and high >= self.take_profit_2:
                self._apply_fill(
                    self.take_profit_2, self.remaining_qty, "TP2", timestamp
                )
                return
            if self.status == "OPEN" and high >= self.take_profit_1:
                half = self.quantity / 2.0
                self._apply_fill(self.take_profit_1, half, "TP1", timestamp)
                self.status = "TP1_HIT"
                # Move SL to entry (break-even) — same as paper executor.
                self.stop_loss = self.entry_price
                return
        else:  # SHORT
            if high >= self.stop_loss:
                self._apply_fill(
                    self.stop_loss, self.remaining_qty, "SL", timestamp
                )
                return
            if self.status == "TP1_HIT" and low <= self.take_profit_2:
                self._apply_fill(
                    self.take_profit_2, self.remaining_qty, "TP2", timestamp
                )
                return
            if self.status == "OPEN" and low <= self.take_profit_1:
                half = self.quantity / 2.0
                self._apply_fill(self.take_profit_1, half, "TP1", timestamp)
                self.status = "TP1_HIT"
                self.stop_loss = self.entry_price
                return

    def force_close(self, price: float, timestamp: datetime) -> None:
        """Close the remaining position at the given price (end of period)."""
        if self.status == "CLOSED":
            return
        self._apply_fill(price, self.remaining_qty, "EOP", timestamp)


@dataclass(slots=True)
class EquityPoint:
    time: datetime
    equity: float


@dataclass(slots=True)
class BacktestResult:
    config: BacktestConfig
    trades: list[SimTrade]
    equity_curve: list[EquityPoint]
    period_from: datetime
    period_to: datetime
    total_bars: int


# ==========================================================================
# Engine
# ==========================================================================


class BacktestEngine:
    """Run a backtest against Binance historical klines.

    Kept crypto-only for now (Binance data source). Forex backtest via
    TwelveData is a straightforward extension but its historical range
    is more limited on the free tier, so we ship crypto-first.
    """

    async def run(self, cfg: BacktestConfig) -> BacktestResult:
        end_time = datetime.now(tz=timezone.utc)
        start_time = end_time - timedelta(days=cfg.days)
        # Pad the start with enough history for the strategy's slowest
        # indicator to be "warm" by the time the reporting window starts.
        #
        # The old heuristic `max(days // 4, 7)` was WAY too small when the
        # bias TF is 4h: EMA200 on 4h needs 200 × 4h = ~33 days of bars,
        # so a 30-day backtest with only 7 days of warmup couldn't fire
        # anything until day ~26 — leaving effectively 3-4 days of live
        # signal window. That produced misleadingly small trade counts.
        _bias_hours = _tf_to_hours(cfg.bias_tf)
        _warmup_bars_needed = cfg.strategy_ctx.ema_slow + 20
        _warmup_days_needed = int(_warmup_bars_needed * _bias_hours / 24) + 2
        pad_days = max(cfg.days // 4, 7, _warmup_days_needed)
        fetch_start = start_time - timedelta(days=pad_days)
        log.info(
            "backtest.warmup",
            symbol=cfg.symbol,
            bias_tf=cfg.bias_tf,
            warmup_days=pad_days,
            requested_days=cfg.days,
        )

        start_ms = int(fetch_start.timestamp() * 1000)
        end_ms = int(end_time.timestamp() * 1000)

        async with BinanceREST() as rest:
            bias_df, setup_df, entry_df = await asyncio.gather(
                _fetch_klines_range(rest, cfg.symbol, cfg.bias_tf, start_ms, end_ms),
                _fetch_klines_range(rest, cfg.symbol, cfg.setup_tf, start_ms, end_ms),
                _fetch_klines_range(rest, cfg.symbol, cfg.entry_tf, start_ms, end_ms),
            )

        log.info(
            "backtest.data_loaded",
            symbol=cfg.symbol,
            bias_bars=len(bias_df),
            setup_bars=len(setup_df),
            entry_bars=len(entry_df),
        )

        # HUGE speed-up: pre-compute EMAs / RSI / ATR / vol MA on the full
        # DataFrames just once. The strategy's enrich() sees these columns
        # already present and returns the df as-is, saving 3 ewm() +
        # ATR + RSI recomputations per iteration.
        s = cfg.strategy_ctx
        bias_df = _enrich_indicators(
            bias_df,
            ema_fast=s.ema_fast, ema_slow=s.ema_slow, ema_trigger=s.ema_trigger,
            rsi_period=s.rsi_period, atr_period=s.atr_period, adx_period=s.adx_period,
        )
        setup_df = _enrich_indicators(
            setup_df,
            ema_fast=s.ema_fast, ema_slow=s.ema_slow, ema_trigger=s.ema_trigger,
            rsi_period=s.rsi_period, atr_period=s.atr_period, adx_period=s.adx_period,
        )
        entry_df = _enrich_indicators(
            entry_df,
            ema_fast=s.ema_fast, ema_slow=s.ema_slow, ema_trigger=s.ema_trigger,
            rsi_period=s.rsi_period, atr_period=s.atr_period, adx_period=s.adx_period,
        )
        log.info("backtest.indicators_precomputed", symbol=cfg.symbol)

        # Instantiate the selected strategies once and reuse across
        # every candle. Order in cfg.strategies determines priority
        # when multiple are enabled: MTF Confluence first (more
        # selective), then Range Breakout, matching the live scanner.
        registry = {
            "mtf_confluence": MTFConfluenceStrategy(cfg.strategy_ctx),
            "range_breakout": RangeBreakoutStrategy(cfg.strategy_ctx),
        }
        strategies_list = [registry[name] for name in cfg.strategies if name in registry]
        if not strategies_list:
            # Fallback so old callers that don't set cfg.strategies still work.
            strategies_list = [registry["mtf_confluence"]]
        trades: list[SimTrade] = []
        open_trade: SimTrade | None = None
        equity = cfg.initial_balance
        equity_curve: list[EquityPoint] = []

        # Warmup gate — need enough bias history before evaluating.
        min_bias_bars = cfg.strategy_ctx.ema_slow + 5

        # Precompute datetime index once for fast lookups.
        entry_index = entry_df.index
        total_bars = len(entry_df)

        for i in range(total_bars):
            t = entry_index[i]
            # Progress log every ~500 bars so long backtests are observable
            # in 'docker compose logs backend | grep backtest.progress'
            # instead of feeling like the request hung.
            if i and i % 500 == 0:
                log.info(
                    "backtest.progress",
                    i=i,
                    total=total_bars,
                    trades_so_far=len(trades),
                    equity=round(equity, 4),
                )
            # Skip until we're inside the requested period.
            if t < pd.Timestamp(start_time):
                continue
            candle = entry_df.iloc[i]

            # 1) First: bar-check the open trade for SL/TP fills.
            if open_trade is not None:
                open_trade.check_bar(
                    high=float(candle["high"]),
                    low=float(candle["low"]),
                    timestamp=t.to_pydatetime(),
                )
                if open_trade.status == "CLOSED":
                    trades.append(open_trade)
                    equity += open_trade.realized_pnl
                    equity_curve.append(
                        EquityPoint(time=open_trade.close_time or t.to_pydatetime(),
                                    equity=equity)
                    )
                    open_trade = None

            # 2) If nothing open, ask the strategy whether to open a new one.
            if open_trade is None:
                bias_slice = bias_df.loc[:t]
                if len(bias_slice) < min_bias_bars:
                    continue
                setup_slice = setup_df.loc[:t]
                if len(setup_slice) < 60:
                    continue
                entry_slice = entry_df.iloc[: i + 1]
                if len(entry_slice) < 60:
                    continue

                proposal = None
                for strat in strategies_list:
                    p, _diag = strat.evaluate(
                        cfg.symbol,
                        bias_df=bias_slice,
                        setup_df=setup_slice,
                        entry_df=entry_slice,
                        bias_tf=cfg.bias_tf,
                        setup_tf=cfg.setup_tf,
                        entry_tf=cfg.entry_tf,
                    )
                    if p is not None:
                        proposal = p
                        break
                if proposal is None:
                    continue

                new_trade = self._try_open(
                    proposal, cfg, equity, t.to_pydatetime()
                )
                if new_trade is not None:
                    open_trade = new_trade

        # Force-close whatever's still open at the end of the period.
        if open_trade is not None and open_trade.status != "CLOSED":
            last_t = entry_index[-1].to_pydatetime()
            last_price = float(entry_df.iloc[-1]["close"])
            open_trade.force_close(last_price, last_t)
            trades.append(open_trade)
            equity += open_trade.realized_pnl
            equity_curve.append(EquityPoint(time=last_t, equity=equity))

        # Ensure the equity curve always has at least one point.
        if not equity_curve:
            equity_curve.append(
                EquityPoint(time=start_time, equity=cfg.initial_balance)
            )

        log.info(
            "backtest.done",
            symbol=cfg.symbol,
            total_bars=total_bars,
            trades=len(trades),
            final_equity=round(equity, 4),
        )

        return BacktestResult(
            config=cfg,
            trades=trades,
            equity_curve=equity_curve,
            period_from=start_time,
            period_to=end_time,
            total_bars=len(entry_df),
        )

    # ------------------------------------------------------------------
    # Sizing
    # ------------------------------------------------------------------

    @staticmethod
    def _try_open(
        proposal: SignalProposal,
        cfg: BacktestConfig,
        equity: float,
        timestamp: datetime,
    ) -> SimTrade | None:
        """Size a new SimTrade from a strategy proposal.

        Uses a *simplified* risk manager: rejects trades where the SL is
        too tight or too wide, sizes quantity = risk_usdt / price_risk,
        caps notional so margin never exceeds equity, and never returns
        an over-sized trade for a small backtest starting balance.
        """
        entry = proposal.entry_price
        sl = proposal.stop_loss
        if entry <= 0 or sl <= 0:
            return None

        price_risk_pct = abs(entry - sl) / entry
        if not (0.001 <= price_risk_pct <= 0.05):
            return None  # too tight (<0.1%) or too wide (>5%)

        risk_usdt = equity * (cfg.risk_per_trade_pct / 100.0)
        notional = risk_usdt / price_risk_pct
        # Margin cap: never deploy more than 95% of equity as margin.
        max_notional = equity * cfg.leverage * 0.95
        notional = min(notional, max_notional)
        qty = notional / entry
        if qty <= 0:
            return None

        return SimTrade(
            open_time=timestamp,
            side="LONG" if proposal.side == Side.LONG else "SHORT",
            entry_price=entry,
            quantity=qty,
            stop_loss=sl,
            initial_sl=sl,
            take_profit_1=proposal.take_profit_1,
            take_profit_2=proposal.take_profit_2,
        )


# ==========================================================================
# Paginated klines fetch
# ==========================================================================


def _tf_to_hours(tf: str) -> float:
    """Convert a Binance timeframe string ("1m", "4h", "1d") to hours."""
    tf = tf.strip().lower()
    if tf.endswith("m"):
        return int(tf[:-1]) / 60.0
    if tf.endswith("h"):
        return float(int(tf[:-1]))
    if tf.endswith("d"):
        return float(int(tf[:-1]) * 24)
    if tf.endswith("w"):
        return float(int(tf[:-1]) * 24 * 7)
    return 1.0


_KLINE_COLS = [
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_asset_volume",
    "num_trades",
    "taker_buy_base_vol",
    "taker_buy_quote_vol",
    "ignore",
]


async def _fetch_klines_range(
    rest: BinanceREST,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
) -> pd.DataFrame:
    """Fetch klines covering [start_ms, end_ms) by paginating the API.

    Binance caps a single response at 1500 klines, so we loop with a
    moving startTime cursor until we've covered the requested range or
    the exchange returns fewer rows than the limit (indicating no more
    data).
    """
    all_rows: list[list] = []
    cursor = start_ms
    while cursor < end_ms:
        r = await rest._client.get(  # noqa: SLF001 — using raw client for speed
            "/fapi/v1/klines",
            params={
                "symbol": symbol.upper(),
                "interval": interval,
                "startTime": cursor,
                "endTime": end_ms,
                "limit": 1500,
            },
        )
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        all_rows.extend(batch)
        if len(batch) < 1500:
            break
        cursor = int(batch[-1][0]) + 1

    if not all_rows:
        empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        empty.index = pd.DatetimeIndex([], tz="UTC", name="open_time")
        return empty

    df = pd.DataFrame(all_rows, columns=_KLINE_COLS)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = df[col].astype(float)
    df = df.set_index("open_time").sort_index()
    # Deduplicate — pagination can return overlapping candles at boundaries.
    df = df[~df.index.duplicated(keep="first")]
    return df[["open", "high", "low", "close", "volume"]]
