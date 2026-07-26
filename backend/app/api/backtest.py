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
