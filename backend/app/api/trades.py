"""Trade endpoints — list open + historical trades and their aggregated P&L."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import desc, func, select

from app.api.deps import SessionDep
from app.binance.rest import BinanceREST
from app.db.models import SignalStatus, Trade, TradingMode
from app.executor.live import LiveExecutor
from app.executor.paper import PaperExecutor

router = APIRouter()


class TradeOut(BaseModel):
    id: int
    created_at: datetime
    closed_at: datetime | None
    signal_id: int | None
    mode: str
    symbol: str
    side: str
    leverage: int
    entry_price: float
    quantity: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    exit_price: float | None
    realized_pnl_usdt: float | None
    realized_pnl_pct: float | None
    fee_usdt: float
    entry_order_id: str | None
    sl_order_id: str | None
    tp1_order_id: str | None
    tp2_order_id: str | None
    status: str
    notes: str


class Stats(BaseModel):
    total_trades: int
    open_trades: int
    wins: int
    losses: int
    win_rate_pct: float
    total_pnl_usdt: float


def _to_out(t: Trade) -> TradeOut:
    return TradeOut(
        id=t.id or 0,
        created_at=t.created_at,
        closed_at=t.closed_at,
        signal_id=t.signal_id,
        mode=t.mode.value if hasattr(t.mode, "value") else str(t.mode),
        symbol=t.symbol,
        side=t.side.value if hasattr(t.side, "value") else str(t.side),
        leverage=t.leverage,
        entry_price=t.entry_price,
        quantity=t.quantity,
        stop_loss=t.stop_loss,
        take_profit_1=t.take_profit_1,
        take_profit_2=t.take_profit_2,
        exit_price=t.exit_price,
        realized_pnl_usdt=t.realized_pnl_usdt,
        realized_pnl_pct=t.realized_pnl_pct,
        fee_usdt=t.fee_usdt,
        entry_order_id=t.entry_order_id,
        sl_order_id=t.sl_order_id,
        tp1_order_id=t.tp1_order_id,
        tp2_order_id=t.tp2_order_id,
        status=t.status.value if hasattr(t.status, "value") else str(t.status),
        notes=t.notes,
    )


@router.get("", response_model=list[TradeOut])
async def list_trades(
    session: SessionDep,
    limit: int = Query(100, ge=1, le=1000),
    mode: TradingMode | None = None,
    status: SignalStatus | None = None,
    symbol: str | None = None,
    open_only: bool = False,
) -> list[TradeOut]:
    stmt = select(Trade).order_by(desc(Trade.created_at)).limit(limit)
    if mode:
        stmt = stmt.where(Trade.mode == mode)
    if status:
        stmt = stmt.where(Trade.status == status)
    if symbol:
        stmt = stmt.where(Trade.symbol == symbol.upper())
    if open_only:
        stmt = stmt.where(Trade.status.in_([SignalStatus.OPEN, SignalStatus.TP1_HIT]))
    rows = (await session.execute(stmt)).scalars().all()
    return [_to_out(t) for t in rows]


@router.get("/stats/summary", response_model=Stats)
async def trade_stats(session: SessionDep, mode: TradingMode | None = None) -> Stats:
    where = []
    if mode:
        where.append(Trade.mode == mode)

    total = (
        await session.execute(select(func.count()).select_from(Trade).where(*where))
    ).scalar_one()
    open_count = (
        await session.execute(
            select(func.count())
            .select_from(Trade)
            .where(Trade.status.in_([SignalStatus.OPEN, SignalStatus.TP1_HIT]), *where)
        )
    ).scalar_one()

    wins = (
        await session.execute(
            select(func.count())
            .select_from(Trade)
            .where(Trade.realized_pnl_usdt > 0, *where)
        )
    ).scalar_one()
    losses = (
        await session.execute(
            select(func.count())
            .select_from(Trade)
            .where(Trade.realized_pnl_usdt < 0, *where)
        )
    ).scalar_one()
    total_pnl = (
        await session.execute(
            select(func.coalesce(func.sum(Trade.realized_pnl_usdt), 0.0)).where(*where)
        )
    ).scalar_one()

    closed = wins + losses
    win_rate = (wins / closed * 100.0) if closed > 0 else 0.0
    return Stats(
        total_trades=total,
        open_trades=open_count,
        wins=wins,
        losses=losses,
        win_rate_pct=round(win_rate, 2),
        total_pnl_usdt=round(float(total_pnl), 4),
    )


@router.post("/{trade_id}/close", response_model=TradeOut)
async def close_trade(trade_id: int, session: SessionDep) -> TradeOut:
    trade = await session.get(Trade, trade_id)
    if trade is None:
        raise HTTPException(404, "Trade not found")
    if trade.status not in (SignalStatus.OPEN, SignalStatus.TP1_HIT):
        raise HTTPException(400, f"Trade is {trade.status}, cannot close")

    # Fetch current price
    async with BinanceREST() as rest:
        price = await rest.get_ticker_price(trade.symbol)

    if trade.mode == TradingMode.PAPER:
        await PaperExecutor().close_trade(trade, current_price=price, reason="manual")
        trade.status = SignalStatus.CLOSED_MANUAL
    else:
        await LiveExecutor().close_trade(trade, current_price=price, reason="manual")

    session.add(trade)
    await session.commit()
    await session.refresh(trade)
    return _to_out(trade)
