"""Signal endpoints — list + acknowledge signals produced by the scanner."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import desc, select

from app.api.deps import SessionDep
from app.db.models import Signal, SignalStatus, TradingMode

router = APIRouter()


class SignalOut(BaseModel):
    id: int
    created_at: datetime
    symbol: str
    side: str
    status: str
    mode: str

    bias_tf: str
    setup_tf: str
    entry_tf: str

    entry_price: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    take_profit_3: float | None = None

    leverage: int
    quantity: float
    risk_amount_usdt: float

    confidence: float
    reason: str
    diagnostics: dict = {}
    trade_id: int | None
    strategy: str | None = None


def _to_out(s: Signal) -> SignalOut:
    return SignalOut(
        id=s.id or 0,
        created_at=s.created_at,
        symbol=s.symbol,
        side=s.side.value if hasattr(s.side, "value") else str(s.side),
        status=s.status.value if hasattr(s.status, "value") else str(s.status),
        mode=s.mode.value if hasattr(s.mode, "value") else str(s.mode),
        bias_tf=s.bias_tf,
        setup_tf=s.setup_tf,
        entry_tf=s.entry_tf,
        entry_price=s.entry_price,
        stop_loss=s.stop_loss,
        take_profit_1=s.take_profit_1,
        take_profit_2=s.take_profit_2,
        take_profit_3=getattr(s, "take_profit_3", None),
        leverage=s.leverage,
        quantity=s.quantity,
        risk_amount_usdt=s.risk_amount_usdt,
        confidence=s.confidence,
        reason=s.reason,
        diagnostics=s.diagnostics or {},
        trade_id=s.trade_id,
        strategy=getattr(s, "strategy", None),
    )


@router.get("", response_model=list[SignalOut])
async def list_signals(
    session: SessionDep,
    limit: int = Query(50, ge=1, le=500),
    status: SignalStatus | None = None,
    mode: TradingMode | None = None,
    symbol: str | None = None,
) -> list[SignalOut]:
    stmt = select(Signal).order_by(desc(Signal.created_at)).limit(limit)
    if status:
        stmt = stmt.where(Signal.status == status)
    if mode:
        stmt = stmt.where(Signal.mode == mode)
    if symbol:
        stmt = stmt.where(Signal.symbol == symbol.upper())
    rows = (await session.execute(stmt)).scalars().all()
    return [_to_out(s) for s in rows]


@router.get("/{signal_id}", response_model=SignalOut)
async def get_signal(signal_id: int, session: SessionDep) -> SignalOut:
    s = await session.get(Signal, signal_id)
    if s is None:
        from fastapi import HTTPException

        raise HTTPException(404, "Signal not found")
    return _to_out(s)


@router.post("/{signal_id}/cancel", response_model=SignalOut)
async def cancel_pending_signal(signal_id: int, session: SessionDep) -> SignalOut:
    """Cancel a PENDING signal so its delayed auto-execute is skipped.

    Only signals still in PENDING (i.e. within the
    ``signal_execute_delay_seconds`` head-start window before the
    executor runs) can be cancelled. Once a trade is OPEN or the
    signal has already terminated the request is a no-op with a 400
    response.
    """
    from fastapi import HTTPException

    s = await session.get(Signal, signal_id)
    if s is None:
        raise HTTPException(404, "Signal not found")
    if s.status != SignalStatus.PENDING:
        raise HTTPException(
            400,
            f"Signal is {s.status.value}, only PENDING signals can be cancelled",
        )
    s.status = SignalStatus.CANCELLED
    s.reason = (s.reason or "") + " | cancelled_by_user"
    session.add(s)
    await session.commit()
    await session.refresh(s)
    return _to_out(s)
