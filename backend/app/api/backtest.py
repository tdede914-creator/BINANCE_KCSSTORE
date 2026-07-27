"""Backtest endpoint.

POST /api/backtest/run
    Body: BacktestRequest — symbol, timeframes, days, sizing params.
    Returns: BacktestResponse — full trade list, equity curve, and
             pre-computed metrics.

The run is synchronous (streams progress via WS is a future
enhancement); a typical 60-day 5m backtest completes in 20-60 s.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.backtest.engine import BacktestConfig, BacktestEngine, SimTrade
from app.backtest.metrics import BacktestMetrics, compute_metrics
from app.binance.rest import VALID_TIMEFRAMES
from app.core.logging import get_logger
from app.strategy.types import StrategyContext

router = APIRouter()
log = get_logger(__name__)


# --------------------------------------------------------------------------
# Request / response schemas
# --------------------------------------------------------------------------


class BacktestStrategyParams(BaseModel):
    ema_fast: int = 50
    ema_slow: int = 200
    ema_trigger: int = 20
    rsi_period: int = 14
    rsi_long_max: float = 75.0
    rsi_short_min: float = 25.0
    atr_period: int = 14
    atr_sl_mult: float = 0.5
    rr_tp1: float = 2.0
    rr_tp2: float = 3.0
    rr_tp3: float = 4.0
    # Regime + volume filters. Match the live scanner defaults so A/B
    # comparisons between backtest and live are apples-to-apples.
    adx_period: int = 14
    adx_min: float = 20.0
    volume_mult: float = 1.2


class BacktestRequest(BaseModel):
    symbol: str = Field(..., min_length=3, max_length=20)
    bias_tf: str = "4h"
    setup_tf: str = "1h"
    entry_tf: str = "5m"
    days: int = Field(60, ge=7, le=365)
    initial_balance: float = Field(1000.0, ge=1.0, le=10_000_000)
    risk_per_trade_pct: float = Field(1.0, ge=0.1, le=20.0)
    leverage: int = Field(5, ge=1, le=125)
    strategy_params: BacktestStrategyParams = Field(
        default_factory=BacktestStrategyParams
    )
    # Which strategies to enable. Default = both (matches live scanner
    # after a fresh install). Pass e.g. ["mtf_confluence"] to backtest
    # only the trend-following logic, or ["range_breakout"] to isolate
    # the breakout logic.
    strategies: list[str] = Field(
        default_factory=lambda: ["mtf_confluence", "range_breakout"]
    )


class FillOut(BaseModel):
    time: datetime
    price: float
    qty: float
    reason: str
    gross_pnl: float
    fees: float
    net_pnl: float


class TradeOut(BaseModel):
    open_time: datetime
    close_time: datetime | None
    side: str
    entry_price: float
    quantity: float
    initial_sl: float
    take_profit_1: float
    take_profit_2: float
    realized_pnl: float
    total_fees: float
    status: str
    close_reason: str | None
    fills: list[FillOut]


class EquityPointOut(BaseModel):
    time: datetime
    equity: float


class BacktestResponse(BaseModel):
    symbol: str
    period_from: datetime
    period_to: datetime
    bias_tf: str
    setup_tf: str
    entry_tf: str
    total_bars: int
    trades: list[TradeOut]
    equity_curve: list[EquityPointOut]
    metrics: BacktestMetrics


# --------------------------------------------------------------------------
# Endpoint
# --------------------------------------------------------------------------


@router.post("/run", response_model=BacktestResponse)
async def run_backtest(body: BacktestRequest) -> BacktestResponse:
    for tf_name, tf_val in (
        ("bias_tf", body.bias_tf),
        ("setup_tf", body.setup_tf),
        ("entry_tf", body.entry_tf),
    ):
        if tf_val not in VALID_TIMEFRAMES:
            raise HTTPException(400, f"invalid {tf_name}: {tf_val}")

    ctx = StrategyContext(
        ema_fast=body.strategy_params.ema_fast,
        ema_slow=body.strategy_params.ema_slow,
        ema_trigger=body.strategy_params.ema_trigger,
        rsi_period=body.strategy_params.rsi_period,
        rsi_long_max=body.strategy_params.rsi_long_max,
        rsi_short_min=body.strategy_params.rsi_short_min,
        atr_period=body.strategy_params.atr_period,
        atr_sl_mult=body.strategy_params.atr_sl_mult,
        rr_tp1=body.strategy_params.rr_tp1,
        rr_tp2=body.strategy_params.rr_tp2,
        rr_tp3=body.strategy_params.rr_tp3,
        adx_period=body.strategy_params.adx_period,
        adx_min=body.strategy_params.adx_min,
        volume_mult=body.strategy_params.volume_mult,
    )
    cfg = BacktestConfig(
        symbol=body.symbol.upper(),
        bias_tf=body.bias_tf,
        setup_tf=body.setup_tf,
        entry_tf=body.entry_tf,
        days=body.days,
        initial_balance=body.initial_balance,
        risk_per_trade_pct=body.risk_per_trade_pct,
        leverage=body.leverage,
        strategy_ctx=ctx,
        strategies=tuple(body.strategies),
    )

    log.info(
        "backtest.request",
        symbol=cfg.symbol,
        days=cfg.days,
        entry_tf=cfg.entry_tf,
    )

    try:
        engine = BacktestEngine()
        result = await engine.run(cfg)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        log.exception("backtest.failed", symbol=cfg.symbol, error=str(e))
        raise HTTPException(500, f"Backtest failed: {type(e).__name__}: {e}") from e

    metrics = compute_metrics(result)

    trades_out = [_trade_out(t) for t in result.trades]
    equity_out = [
        EquityPointOut(time=p.time, equity=round(p.equity, 4))
        for p in result.equity_curve
    ]

    return BacktestResponse(
        symbol=result.config.symbol,
        period_from=result.period_from,
        period_to=result.period_to,
        bias_tf=result.config.bias_tf,
        setup_tf=result.config.setup_tf,
        entry_tf=result.config.entry_tf,
        total_bars=result.total_bars,
        trades=trades_out,
        equity_curve=equity_out,
        metrics=metrics,
    )


# --------------------------------------------------------------------------
# Batch endpoint — run the same backtest across multiple symbols in one
# HTTP call so users don't have to click Run 10 times.
# --------------------------------------------------------------------------


class BatchBacktestRequest(BaseModel):
    symbols: list[str] = Field(..., min_length=1, max_length=20)
    bias_tf: str = "4h"
    setup_tf: str = "1h"
    entry_tf: str = "5m"
    days: int = Field(60, ge=7, le=365)
    initial_balance: float = Field(1000.0, ge=1.0, le=10_000_000)
    risk_per_trade_pct: float = Field(1.0, ge=0.1, le=20.0)
    leverage: int = Field(5, ge=1, le=125)
    strategy_params: BacktestStrategyParams = Field(
        default_factory=BacktestStrategyParams
    )
    strategies: list[str] = Field(
        default_factory=lambda: ["mtf_confluence", "range_breakout"]
    )


class BatchBacktestSummary(BaseModel):
    symbol: str
    total_trades: int = 0
    win_rate_pct: float = 0.0
    total_return_usdt: float = 0.0
    total_return_pct: float = 0.0
    profit_factor: float = 0.0
    max_drawdown_pct: float = 0.0
    avg_rr: float = 0.0
    total_fees_usdt: float = 0.0
    final_balance: float = 0.0
    error: str | None = None


class BatchBacktestResponse(BaseModel):
    period_from: datetime | None
    period_to: datetime | None
    bias_tf: str
    setup_tf: str
    entry_tf: str
    days: int
    summaries: list[BatchBacktestSummary]


@router.post("/batch", response_model=BatchBacktestResponse)
async def run_batch(body: BatchBacktestRequest) -> BatchBacktestResponse:
    """Run the same backtest config against several symbols sequentially.

    Sequential (not parallel) on purpose: parallel fetches would swamp
    Binance's per-IP rate limit and the engine is CPU-bound during the
    replay loop anyway, so we'd end up starving each other. Users see
    aggregated results only — no per-symbol trade lists — to keep the
    response payload small.
    """
    for tf_name, tf_val in (
        ("bias_tf", body.bias_tf),
        ("setup_tf", body.setup_tf),
        ("entry_tf", body.entry_tf),
    ):
        if tf_val not in VALID_TIMEFRAMES:
            raise HTTPException(400, f"invalid {tf_name}: {tf_val}")

    ctx = StrategyContext(
        ema_fast=body.strategy_params.ema_fast,
        ema_slow=body.strategy_params.ema_slow,
        ema_trigger=body.strategy_params.ema_trigger,
        rsi_period=body.strategy_params.rsi_period,
        rsi_long_max=body.strategy_params.rsi_long_max,
        rsi_short_min=body.strategy_params.rsi_short_min,
        atr_period=body.strategy_params.atr_period,
        atr_sl_mult=body.strategy_params.atr_sl_mult,
        rr_tp1=body.strategy_params.rr_tp1,
        rr_tp2=body.strategy_params.rr_tp2,
        rr_tp3=body.strategy_params.rr_tp3,
        adx_period=body.strategy_params.adx_period,
        adx_min=body.strategy_params.adx_min,
        volume_mult=body.strategy_params.volume_mult,
    )

    engine = BacktestEngine()
    summaries: list[BatchBacktestSummary] = []
    period_from: datetime | None = None
    period_to: datetime | None = None

    # Dedup & clean the symbol list up front.
    clean_symbols: list[str] = []
    for s in body.symbols:
        s2 = s.strip().upper()
        if s2 and s2 not in clean_symbols:
            clean_symbols.append(s2)

    log.info(
        "backtest.batch.start",
        n_symbols=len(clean_symbols),
        days=body.days,
        entry_tf=body.entry_tf,
    )

    for idx, sym in enumerate(clean_symbols):
        log.info("backtest.batch.symbol", i=idx + 1, total=len(clean_symbols), symbol=sym)
        cfg = BacktestConfig(
            symbol=sym,
            bias_tf=body.bias_tf,
            setup_tf=body.setup_tf,
            entry_tf=body.entry_tf,
            days=body.days,
            initial_balance=body.initial_balance,
            risk_per_trade_pct=body.risk_per_trade_pct,
            leverage=body.leverage,
            strategy_ctx=ctx,
            strategies=tuple(body.strategies),
        )
        try:
            result = await engine.run(cfg)
            metrics = compute_metrics(result)
            if period_from is None:
                period_from = result.period_from
                period_to = result.period_to
            summaries.append(
                BatchBacktestSummary(
                    symbol=sym,
                    total_trades=metrics.total_trades,
                    win_rate_pct=metrics.win_rate_pct,
                    total_return_usdt=metrics.total_return_usdt,
                    total_return_pct=metrics.total_return_pct,
                    profit_factor=metrics.profit_factor,
                    max_drawdown_pct=metrics.max_drawdown_pct,
                    avg_rr=metrics.avg_rr,
                    total_fees_usdt=metrics.total_fees_usdt,
                    final_balance=metrics.final_balance,
                )
            )
        except Exception as e:  # noqa: BLE001
            log.exception(
                "backtest.batch.symbol_failed", symbol=sym, error=str(e)
            )
            summaries.append(
                BatchBacktestSummary(symbol=sym, error=f"{type(e).__name__}: {e}")
            )

    return BatchBacktestResponse(
        period_from=period_from,
        period_to=period_to,
        bias_tf=body.bias_tf,
        setup_tf=body.setup_tf,
        entry_tf=body.entry_tf,
        days=body.days,
        summaries=summaries,
    )


def _trade_out(t: SimTrade) -> TradeOut:
    return TradeOut(
        open_time=t.open_time,
        close_time=t.close_time,
        side=t.side,
        entry_price=round(t.entry_price, 8),
        quantity=round(t.quantity, 8),
        initial_sl=round(t.initial_sl, 8),
        take_profit_1=round(t.take_profit_1, 8),
        take_profit_2=round(t.take_profit_2, 8),
        realized_pnl=round(t.realized_pnl, 4),
        total_fees=round(t.total_fees, 4),
        status=t.status,
        close_reason=t.close_reason,
        fills=[
            FillOut(
                time=f.time,
                price=round(f.price, 8),
                qty=round(f.qty, 8),
                reason=f.reason,
                gross_pnl=round(f.gross_pnl, 4),
                fees=round(f.fees, 4),
                net_pnl=round(f.net_pnl, 4),
            )
            for f in t.fills
        ],
    )
