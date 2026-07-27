"""Thin async wrapper around Telegram's Bot API for outbound notifications.

The bot only *sends* messages — we don't listen for commands from the
user's side (that would need a long-poll or webhook and adds moving
parts). All the notifier does is HTTP POST to::

    https://api.telegram.org/bot<TOKEN>/sendMessage

with a rendered payload. Callers pass a UserConfig row so they don't
have to know how to derive credentials themselves; the notifier
short-circuits if:

- ``cfg.telegram_enabled`` is False
- token or chat_id is empty (misconfigured)

so callers can call ``notify_signal(...)`` unconditionally without
checking anything themselves — this keeps hot paths (scanner tick,
executor update) clean.

Failures are logged at INFO level and swallowed. A Telegram outage
should NEVER cause a signal to be lost or an executor to crash.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from app.core.logging import get_logger
from app.core.security import decrypt_secret
from app.db.models import Signal, Trade, UserConfig

log = get_logger(__name__)


_API_BASE = "https://api.telegram.org"
_HTTP_TIMEOUT = httpx.Timeout(10.0)


def _decode_token(cfg: UserConfig) -> str | None:
    """Pull the bot token from the config. Handles both plain and encrypted."""
    raw = (cfg.telegram_bot_token_enc or "").strip()
    if not raw:
        return None
    # Tokens are stored encrypted (same helper as Binance keys). If the
    # value looks like a raw Telegram token (12345:ABC...) we accept it
    # too for local-dev convenience.
    if ":" in raw and raw.split(":", 1)[0].isdigit():
        return raw
    try:
        return decrypt_secret(raw)
    except Exception as e:  # noqa: BLE001
        log.warning("telegram.token_decrypt_failed", error=str(e))
        return None


async def send_message(cfg: UserConfig, text: str) -> bool:
    """Send a Markdown-formatted message. Returns True on success."""
    if not cfg.telegram_enabled:
        return False
    token = _decode_token(cfg)
    chat_id = (cfg.telegram_chat_id or "").strip()
    if not token or not chat_id:
        return False

    url = f"{_API_BASE}/bot{token}/sendMessage"
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code != 200:
                log.warning(
                    "telegram.send_failed",
                    status=resp.status_code,
                    body=resp.text[:500],
                )
                return False
            return True
    except Exception as e:  # noqa: BLE001 — never crash caller
        log.warning("telegram.send_exception", error=str(e))
        return False


# ---------------------------------------------------------------------------
# Message templates
# ---------------------------------------------------------------------------


def _fmt_price(p: float) -> str:
    """Compact formatting that keeps precision for sub-dollar assets."""
    if p == 0:
        return "0"
    ap = abs(p)
    if ap >= 100:
        return f"{p:,.2f}"
    if ap >= 1:
        return f"{p:,.4f}"
    if ap >= 0.01:
        return f"{p:,.6f}"
    return f"{p:,.8f}"


def render_signal(signal: Signal) -> str:
    """Format a Signal → Markdown message body (Telegram trader-signal style)."""
    side = signal.side.value if hasattr(signal.side, "value") else str(signal.side)
    emoji = "🟢" if side == "LONG" else "🔴"
    strat = getattr(signal, "strategy", "mtf_confluence")
    strat_pretty = {
        "mtf_confluence": "MTF Confluence",
        "range_breakout": "Range Breakout",
        "tradingview": "TradingView",
    }.get(strat, strat)

    lines = [
        f"{emoji} *{side} {signal.symbol}* · `{signal.entry_tf}`",
        "",
        f"📍 Entry: `{_fmt_price(signal.entry_price)}`",
        f"❌ SL: `{_fmt_price(signal.stop_loss)}`",
        f"🎯 TP1: `{_fmt_price(signal.take_profit_1)}`",
        f"🎯 TP2: `{_fmt_price(signal.take_profit_2)}`",
    ]
    tp3 = getattr(signal, "take_profit_3", None)
    if tp3 is not None and tp3 > 0:
        lines.append(f"🎯 TP3: `{_fmt_price(float(tp3))}`")
    lines += [
        "",
        f"Leverage: {signal.leverage}x · Conf {signal.confidence * 100:.0f}%",
        f"Strategy: _{strat_pretty}_",
    ]
    if signal.reason:
        # Truncate long reasons — some strategies emit a paragraph.
        reason = signal.reason
        if len(reason) > 160:
            reason = reason[:157] + "..."
        lines.append(f"\n_{reason}_")
    return "\n".join(lines)


def render_trade_update(trade: Trade, event: str) -> str:
    """Format a trade state transition → message.

    event ∈ {"OPEN", "TP1_HIT", "TP2", "SL", "MANUAL", "TRAIL"}.
    """
    icons = {
        "OPEN": "🚀",
        "TP1_HIT": "🎯",
        "TP2": "✅",
        "SL": "🛑",
        "MANUAL": "🖐",
        "TRAIL": "🔄",
    }
    icon = icons.get(event, "ℹ️")
    side = trade.side.value if hasattr(trade.side, "value") else str(trade.side)
    lines = [
        f"{icon} *{event}* — {side} {trade.symbol}",
        f"Entry: `{_fmt_price(trade.entry_price)}` · Qty {trade.quantity}",
    ]
    if trade.exit_price:
        lines.append(f"Exit: `{_fmt_price(trade.exit_price)}`")
    if trade.realized_pnl_usdt is not None:
        pnl = float(trade.realized_pnl_usdt)
        sign = "+" if pnl >= 0 else ""
        lines.append(f"Realized P&L: *{sign}{pnl:.4f} USDT*")
    return "\n".join(lines)


def render_balance(
    mode: str,
    wallet: float,
    available: float,
    locked: float,
    unrealized: float,
    source: str,
    error: str | None = None,
) -> str:
    """Format the hourly wallet snapshot → message."""
    now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    mode_up = mode.upper()
    header = f"💰 *Wallet update* — `{now}`"
    body = [
        header,
        "",
        f"Mode: *{mode_up}* (`{source}`)",
        f"Wallet: `{_fmt_price(wallet)}` USDT",
        f"Available: `{_fmt_price(available)}`",
    ]
    if locked > 0:
        body.append(f"Locked in trades: `{_fmt_price(locked)}`")
    if unrealized:
        sign = "+" if unrealized >= 0 else ""
        body.append(f"Unrealized P&L: *{sign}{unrealized:.4f}*")
    if error:
        body.append(f"\n⚠️ _{error}_")
    return "\n".join(body)
