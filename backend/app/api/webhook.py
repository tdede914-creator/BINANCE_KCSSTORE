"""External signal ingestion — TradingView (and generic) webhook.

Rationale
---------
TwelveData has quota limits on the free tier that make scanning 10+
forex/index/commodity pairs impractical (800 requests/day cap). The
cleanest bypass is to let TradingView do the signal generation on its
side — it has real-time data for every instrument Exness / MT5 quotes
— and just POST the alert here.

Setup on the TradingView side
-----------------------------
1. Free-tier TradingView doesn't support webhooks. You need at least
   the **Pro** plan ($14.95/month) to add a webhook URL to an alert.
2. Alert message body must be JSON in this shape (all fields are
   TradingView's alert placeholders):

       {
         "secret":     "<your token from Settings>",
         "symbol":     "{{ticker}}",
         "side":       "{{strategy.order.action}}",   // BUY / SELL
         "entry":      {{close}},
         "sl":         {{plot("Stop")}},              // optional
         "tp1":        {{plot("TP1")}},               // optional
         "tp2":        {{plot("TP2")}},               // optional
         "entry_tf":   "{{interval}}",
         "confidence": 0.75,                          // optional
         "reason":     "MTF confluence bullish"       // optional
       }

3. Webhook URL: ``http://<your-vps-ip>:8000/api/webhook/tradingview``
4. Once TradingView fires the alert, the signal appears on the
   Dashboard / Signals page tagged with ``strategy="tradingview"``.

Security
--------
The endpoint is open to the internet. Auth is a shared secret carried
in the JSON body — generated in ``GET /api/config`` (auto-created if
empty) and displayed in Settings. Requests with the wrong secret are
silently dropped (return 401 to prevent enumeration).
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlmodel import select

from app.core.logging import get_logger
from app.db.database import session_scope
from app.db.models import (
    Signal,
    SignalSide,
    SignalStatus,
    TradingMode,
    UserConfig,
)
from app.api.ws import event_bus

router = APIRouter()
log = get_logger(__name__)


# --------------------------------------------------------------------------
# Request schema — permissive: TradingView alert templates vary.
# --------------------------------------------------------------------------


class TVWebhookPayload(BaseModel):
    secret: str
    symbol: str = Field(..., min_length=1, max_length=32)
    # TradingView's built-in placeholders emit BUY / SELL / LONG / SHORT
    # depending on whether it's a strategy or an indicator alert. We
    # normalise below.
    side: str

    entry: float | None = None
    entry_price: float | None = None
    sl: float | None = None
    stop_loss: float | None = None
    tp1: float | None = None
    take_profit_1: float | None = None
    tp2: float | None = None
    take_profit_2: float | None = None
    entry_tf: str | None = None
    confidence: float | None = None
    reason: str | None = None
    leverage: int | None = None


def _normalise_side(raw: str) -> SignalSide:
    r = raw.strip().upper()
    if r in ("BUY", "LONG", "L"):
        return SignalSide.LONG
    if r in ("SELL", "SHORT", "S"):
        return SignalSide.SHORT
    raise HTTPException(400, f"invalid side: {raw!r}")


# --------------------------------------------------------------------------
# Endpoint
# --------------------------------------------------------------------------


@router.post("/tradingview")
async def receive_tradingview(request: Request) -> dict:
    """Accept a TradingView alert JSON body and create a Signal record.

    We parse the body ourselves rather than relying on Pydantic's
    automatic binding because TradingView sends ``Content-Type:
    text/plain`` even when the body is JSON, which trips FastAPI's
    JSON auto-decoding.
    """
    raw = await request.body()
    try:
        import json
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        log.warning("webhook.tradingview.bad_json", error=str(e))
        raise HTTPException(400, "body must be JSON") from e

    try:
        payload = TVWebhookPayload(**data)
    except Exception as e:  # pydantic ValidationError etc.
        log.warning("webhook.tradingview.bad_payload", error=str(e), data=data)
        raise HTTPException(400, f"invalid payload: {e}") from e

    async with session_scope() as session:
        cfg_result = await session.execute(select(UserConfig).limit(1))
        cfg = cfg_result.scalars().first()
        if cfg is None:
            raise HTTPException(500, "no user_config row")

        # Feature-gate + secret check. We deliberately return the same
        # 401 status for both "disabled" and "wrong secret" so external
        # scanners can't enumerate valid endpoints.
        if not cfg.tradingview_webhook_enabled:
            log.warning("webhook.tradingview.disabled")
            raise HTTPException(401, "webhook disabled")
        if not cfg.tradingview_webhook_secret or payload.secret != cfg.tradingview_webhook_secret:
            log.warning("webhook.tradingview.bad_secret")
            raise HTTPException(401, "bad secret")

        # Merge alt field names — Pine users write either "entry" or
        # "entry_price"; we accept both to reduce alert-template friction.
        entry = payload.entry_price or payload.entry or 0.0
        sl = payload.stop_loss or payload.sl or 0.0
        tp1 = payload.take_profit_1 or payload.tp1 or 0.0
        tp2 = payload.take_profit_2 or payload.tp2 or 0.0
        side = _normalise_side(payload.side)
        entry_tf = (payload.entry_tf or cfg.entry_tf or "15m").strip()
        leverage = payload.leverage or cfg.default_leverage
        confidence = float(payload.confidence) if payload.confidence is not None else 0.7
        confidence = max(0.0, min(confidence, 1.0))
        reason = payload.reason or "TradingView webhook alert"

        signal = Signal(
            symbol=payload.symbol.upper(),
            side=side,
            status=SignalStatus.OPEN,  # TV alerts are already "live" — no risk check needed
            mode=cfg.trading_mode,
            bias_tf=cfg.bias_tf,
            setup_tf=cfg.setup_tf,
            entry_tf=entry_tf,
            entry_price=entry,
            stop_loss=sl,
            take_profit_1=tp1,
            take_profit_2=tp2,
            leverage=leverage,
            quantity=0.0,          # not sized — user executes manually
            risk_amount_usdt=0.0,  # ditto
            confidence=confidence,
            reason=reason,
            diagnostics={
                "source": "tradingview_webhook",
                "raw": data,
                "received_at": datetime.now(tz=timezone.utc).isoformat(),
            },
            strategy="tradingview",
        )
        session.add(signal)
        await session.flush()

        signal_dict = _signal_dict(signal)

    await event_bus.publish(
        {"type": "signal.new", "data": signal_dict, "trade": None}
    )
    log.info(
        "webhook.tradingview.accepted",
        symbol=signal.symbol,
        side=side.value,
        entry=entry,
    )
    return {"ok": True, "signal_id": signal.id, "symbol": signal.symbol}


def _signal_dict(s: Signal) -> dict:
    """Trimmed Signal → dict for WS broadcast. Matches scanner.engine's shape."""
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
        "strategy": getattr(s, "strategy", "tradingview"),
    }
