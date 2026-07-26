"""Trade endpoints — list open + historical trades and their aggregated P&L."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import desc, func, select

from datetime import datetime, timezone

from app.api.deps import SessionDep
from app.core.logging import get_logger
from app.datasource.factory import get_data_source
from app.db.models import SignalStatus, Trade, TradingMode
from app.executor.live import LiveExecutor

log = get_logger(__name__)

# Same fee used by PaperExecutor.close_trade so the manual-close P&L
# matches auto-close P&L bookkeeping-wise.
_PAPER_FEE_RATE = 0.0005

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
    """Manually close an open trade at the current market price.

    Kept intentionally simple: we perform the paper close inline against
    the request's session instead of delegating to PaperExecutor. The
    executor opens its own ``session_scope()``, which conflicts with the
    request's session and causes SQLAlchemy to fail silently — the
    endpoint would then hang or crash mid-response, showing up in the
    browser as a generic 'TypeError: Failed to fetch'.
    """
    trade = await session.get(Trade, trade_id)
    if trade is None:
        raise HTTPException(404, "Trade not found")

    # SignalStatus may be an enum or a plain string coming out of SQLite.
    status_val = (
        trade.status.value if hasattr(trade.status, "value") else str(trade.status)
    )
    if status_val not in ("OPEN", "TP1_HIT"):
        raise HTTPException(400, f"Trade is {status_val}, cannot close")

    # Fetch current price from whichever market this trade was opened on.
    try:
        source = await get_data_source()
    except RuntimeError as e:
        raise HTTPException(400, str(e)) from e

    try:
        async with source:
            price = await source.get_ticker_price(trade.symbol)
    except Exception as e:  # noqa: BLE001
        log.warning("close.price_fetch_failed", trade_id=trade_id, error=str(e))
        raise HTTPException(502, f"Failed to fetch current price: {e}") from e

    mode_val = (
        trade.mode.value if hasattr(trade.mode, "value") else str(trade.mode)
    )
    try:
        if mode_val == "paper":
            _paper_close_inline(trade, price)
        else:
            result = await LiveExecutor().close_trade(
                trade, current_price=price, reason="manual"
            )
            if not result.ok:
                raise HTTPException(502, f"Failed to close: {result.error}")
            # LiveExecutor committed via its own session; we can't reuse the
            # object as-is because SQLAlchemy may consider it stale. Detach
            # it and reload from DB before returning.
            session.expunge(trade)
            trade = await session.get(Trade, trade_id)
            if trade is None:
                raise HTTPException(500, "Trade vanished after live close")
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        log.error("close.failed", trade_id=trade_id, error=str(e))
        raise HTTPException(500, f"Close failed: {e}") from e

    session.add(trade)
    await session.commit()
    await session.refresh(trade)
    return _to_out(trade)


def _paper_close_inline(trade: Trade, exit_price: float) -> None:
    """Mutate ``trade`` in-place to reflect a manual paper close.

    Mirrors ``PaperExecutor.close_trade`` fee math so histories stay
    consistent:
      - remaining qty depends on status (half if TP1_HIT, else full)
      - net leg P&L = gross_pnl − entry_fee_share − exit_fee
      - realised P&L accumulates (adds to any TP1 partial already booked)
      - realised P&L % = final realised P&L / initial margin
    """
    remaining_qty = (
        trade.quantity / 2.0
        if trade.status == SignalStatus.TP1_HIT
        else trade.quantity
    )
    side_val = trade.side.value if hasattr(trade.side, "value") else str(trade.side)
    if side_val == "LONG":
        move = exit_price - trade.entry_price
    else:
        move = trade.entry_price - exit_price

    gross_leg = move * remaining_qty
    entry_fee_share = trade.entry_price * remaining_qty * _PAPER_FEE_RATE
    exit_fee = exit_price * remaining_qty * _PAPER_FEE_RATE
    net_leg = gross_leg - entry_fee_share - exit_fee

    prev_realized = trade.realized_pnl_usdt or 0.0
    trade.exit_price = exit_price
    trade.realized_pnl_usdt = round(prev_realized + net_leg, 4)
    trade.fee_usdt = round((trade.fee_usdt or 0.0) + exit_fee, 4)

    margin = (trade.entry_price * trade.quantity) / max(trade.leverage, 1)
    trade.realized_pnl_pct = round(
        (trade.realized_pnl_usdt / max(margin, 1e-9)) * 100.0, 4
    )
    trade.closed_at = datetime.now(tz=timezone.utc)
    trade.notes = "manual close via API"
    trade.status = SignalStatus.CLOSED_MANUAL
