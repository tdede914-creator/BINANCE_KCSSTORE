"""MT5 bridge API.

The Windows-side MetaTrader 5 bridge script talks to the Linux backend
through these endpoints. Communication is a simple polling loop:

  1. Bridge  →  GET /api/mt5/pending
       - Returns forex signals that haven't been executed yet.
  2. Bridge attempts the order in MT5.
  3. Bridge  →  POST /api/mt5/report
       - Reports fill / error back so the signal row is updated.
  4. Bridge polls its own MT5 positions and reports closures via
       POST /api/mt5/close-report.

Auth is a shared secret stored in ``UserConfig.mt5_bridge_secret``,
auto-generated on first ``GET /api/config`` (same bootstrap logic we
use elsewhere). The bridge passes it as ``X-Bridge-Secret`` header.
Wrong / missing secret → 401.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from sqlmodel import select

from app.core.logging import get_logger
from app.db.database import session_scope
from app.db.models import (
    Signal,
    SignalStatus,
    Trade,
    TradingMode,
    TrailingMode,
    UserConfig,
)


router = APIRouter()
log = get_logger(__name__)


class PendingSignal(BaseModel):
    id: int
    created_at: datetime
    symbol: str
    side: str
    entry_price: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    take_profit_3: float | None
    leverage: int
    reason: str


class PendingResponse(BaseModel):
    signals: list[PendingSignal]
    server_time: datetime


class ReportRequest(BaseModel):
    signal_id: int
    ok: bool
    ticket: str | None = None
    fill_price: float | None = None
    lot: float | None = None
    mt5_symbol: str | None = None
    error: str | None = None


class CloseReportRequest(BaseModel):
    ticket: str
    exit_price: float
    reason: str          # "SL", "TP", "MANUAL", "TIMEOUT"
    realized_pnl: float | None = None


async def _authed(secret_header: str | None) -> UserConfig:
    if not secret_header:
        raise HTTPException(401, "missing X-Bridge-Secret")
    async with session_scope() as session:
        cfg_result = await session.execute(select(UserConfig).limit(1))
        cfg = cfg_result.scalars().first()
        if cfg is None:
            raise HTTPException(500, "no user_config row")
        if not cfg.mt5_bridge_secret or cfg.mt5_bridge_secret != secret_header:
            raise HTTPException(401, "bad secret")
        return cfg


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------


@router.get("/pending", response_model=PendingResponse)
async def get_pending_signals(
    x_bridge_secret: str | None = Header(default=None),
) -> PendingResponse:
    """Bridge polls this — returns forex signals not yet handed to MT5.

    Filter: FOREX market mode + mt5_ticket IS NULL + status = OPEN
    (i.e. fired via _save_forex_signal but not yet executed).
    """
    await _authed(x_bridge_secret)

    async with session_scope() as session:
        # Update heartbeat so the dashboard can show 'bridge alive'.
        cfg_result = await session.execute(select(UserConfig).limit(1))
        cfg = cfg_result.scalars().first()
        if cfg is not None:
            cfg.mt5_bridge_last_heartbeat = datetime.now(tz=timezone.utc)
            session.add(cfg)

        rows = await session.execute(
            select(Signal)
            .where(
                Signal.mt5_ticket.is_(None),
                Signal.mt5_error.is_(None),
                Signal.status == SignalStatus.OPEN,
            )
            .order_by(Signal.created_at)
            .limit(20)
        )
        signals = rows.scalars().all()

    out: list[PendingSignal] = []
    for s in signals:
        # Only forex signals — save_forex_signal is the only path that
        # leaves mt5_ticket NULL + status=OPEN with quantity=0.
        # Extra defensive check on quantity so we don't accidentally
        # send a crypto signal through the MT5 bridge.
        if s.quantity != 0:
            continue
        out.append(
            PendingSignal(
                id=s.id or 0,
                created_at=s.created_at,
                symbol=s.symbol,
                side=s.side.value if hasattr(s.side, "value") else str(s.side),
                entry_price=s.entry_price,
                stop_loss=s.stop_loss,
                take_profit_1=s.take_profit_1,
                take_profit_2=s.take_profit_2,
                take_profit_3=getattr(s, "take_profit_3", None),
                leverage=s.leverage,
                reason=s.reason or "",
            )
        )
    return PendingResponse(signals=out, server_time=datetime.now(tz=timezone.utc))


@router.post("/report")
async def report_execution(
    body: ReportRequest,
    x_bridge_secret: str | None = Header(default=None),
) -> dict:
    """Bridge reports MT5 order outcome.

    - ok=True: mt5_ticket + fill_price get saved. A Trade row is created
      so the position appears on the History / Signals pages.
    - ok=False: the signal is marked CANCELLED with mt5_error populated
      so users can see why (broker rejected, symbol unknown, no funds).
    """
    await _authed(x_bridge_secret)

    async with session_scope() as session:
        s = await session.get(Signal, body.signal_id)
        if s is None:
            raise HTTPException(404, "signal not found")

        if body.ok:
            s.mt5_ticket = body.ticket or "unknown"
            s.mt5_fill_price = body.fill_price
            s.mt5_lot = body.lot
            # Overwrite entry_price with the actual fill so downstream
            # display + Telegram trade-update messages match MT5.
            if body.fill_price and body.fill_price > 0:
                s.entry_price = body.fill_price
            # Create a Trade row for the signal so it shows up under
            # 'Open positions' on the dashboard.
            trade = Trade(
                signal_id=s.id,
                mode=TradingMode.LIVE,
                symbol=body.mt5_symbol or s.symbol,
                side=s.side.value if hasattr(s.side, "value") else str(s.side),
                leverage=s.leverage,
                entry_price=body.fill_price or s.entry_price,
                quantity=body.lot or 0.0,
                quantity_precision=2,
                stop_loss=s.stop_loss,
                take_profit_1=s.take_profit_1,
                take_profit_2=s.take_profit_2,
                initial_sl=s.stop_loss,
                status=SignalStatus.OPEN,
                trailing_mode=TrailingMode.OFF,
                notes=f"MT5 ticket #{body.ticket}",
                sl_order_id=body.ticket or "",
                tp1_order_id="",
                tp2_order_id="",
            )
            session.add(trade)
            await session.flush()
            s.trade_id = trade.id
            session.add(s)
            log.info(
                "mt5.report.filled",
                signal_id=body.signal_id,
                ticket=body.ticket,
                fill_price=body.fill_price,
            )
            return {"ok": True, "trade_id": trade.id}
        else:
            s.mt5_error = body.error or "unknown"
            s.status = SignalStatus.CANCELLED
            s.reason = (s.reason or "") + f" | mt5_failed: {body.error}"
            session.add(s)
            log.warning(
                "mt5.report.failed",
                signal_id=body.signal_id,
                error=body.error,
            )
            return {"ok": True, "cancelled": True}


@router.post("/close-report")
async def report_close(
    body: CloseReportRequest,
    x_bridge_secret: str | None = Header(default=None),
) -> dict:
    """Bridge reports a position was closed on MT5 (SL/TP/manual).

    We look up the Trade by mt5 ticket (stored in sl_order_id when we
    created it) and mark it closed with the appropriate reason.
    """
    await _authed(x_bridge_secret)

    async with session_scope() as session:
        # sl_order_id holds the MT5 ticket in our tiny convention.
        rows = await session.execute(
            select(Trade).where(Trade.sl_order_id == body.ticket)
        )
        trade = rows.scalars().first()
        if trade is None:
            log.warning("mt5.close_report.no_trade", ticket=body.ticket)
            return {"ok": False, "error": "trade not found"}

        status_map = {
            "SL": SignalStatus.CLOSED_SL,
            "TP": SignalStatus.CLOSED_TP,
            "MANUAL": SignalStatus.CLOSED_MANUAL,
        }
        trade.status = status_map.get(body.reason.upper(), SignalStatus.CLOSED_MANUAL)
        trade.exit_price = body.exit_price
        trade.closed_at = datetime.now(tz=timezone.utc)
        if body.realized_pnl is not None:
            trade.realized_pnl_usdt = body.realized_pnl
        session.add(trade)
        log.info(
            "mt5.close_report",
            ticket=body.ticket,
            reason=body.reason,
            exit_price=body.exit_price,
            realized_pnl=body.realized_pnl,
        )
    return {"ok": True}


@router.post("/heartbeat")
async def heartbeat(
    x_bridge_secret: str | None = Header(default=None),
) -> dict:
    """Bridge pings to say 'I'm alive'. Just refreshes the timestamp."""
    await _authed(x_bridge_secret)
    async with session_scope() as session:
        cfg_result = await session.execute(select(UserConfig).limit(1))
        cfg = cfg_result.scalars().first()
        if cfg is not None:
            cfg.mt5_bridge_last_heartbeat = datetime.now(tz=timezone.utc)
            session.add(cfg)
    return {"ok": True, "server_time": datetime.now(tz=timezone.utc).isoformat()}
