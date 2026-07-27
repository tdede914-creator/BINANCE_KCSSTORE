"""Wallet / balance endpoint — mode-aware.

The dashboard needs a single source of truth for equity + free margin
that behaves correctly depending on ``UserConfig.trading_mode``:

- ``paper``: derived from ``paper_balance`` in the DB plus realised
  P&L from closed paper trades, minus margin locked in open positions.
- ``live``: fetched from Binance Futures ``GET /fapi/v2/account`` for
  USDT — reflecting the REAL wallet balance the user sees in the app.

Without this endpoint the frontend was showing the paper balance even
after switching to LIVE, which is confusing and dangerous (users
thought their live wallet was $10.83 when in reality it might be much
more or much less).
"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel
from sqlmodel import select

from app.binance.rest import BinanceREST
from app.core.logging import get_logger
from app.core.security import decrypt_secret
from app.db.database import session_scope
from app.db.models import SignalStatus, Trade, TradingMode, UserConfig

router = APIRouter()
log = get_logger(__name__)


class WalletBalance(BaseModel):
    mode: TradingMode                # "paper" or "live"
    source: str                       # "paper" | "binance" | "paper_fallback"
    wallet_balance: float             # realised balance (paper) or walletBalance (live)
    available_balance: float          # free margin the user can risk on new trades
    unrealized_pnl: float             # 0 for paper, actual for live
    locked_margin: float              # margin currently in open positions
    equity: float                     # wallet_balance + unrealized_pnl
    error: str | None = None          # populated if the live fetch failed


@router.get("/balance", response_model=WalletBalance)
async def get_wallet_balance() -> WalletBalance:
    """Return the current wallet snapshot for whichever trading mode is active."""
    async with session_scope() as session:
        cfg_result = await session.execute(select(UserConfig).limit(1))
        cfg = cfg_result.scalars().first()
        if cfg is None:
            # Should be impossible after init_db, but guard anyway.
            return WalletBalance(
                mode=TradingMode.PAPER,
                source="paper",
                wallet_balance=0.0,
                available_balance=0.0,
                unrealized_pnl=0.0,
                locked_margin=0.0,
                equity=0.0,
                error="no user_config row",
            )

        if cfg.trading_mode == TradingMode.LIVE:
            return await _live_balance(cfg)
        return await _paper_balance(session, cfg)


async def _live_balance(cfg: UserConfig) -> WalletBalance:
    """Fetch the real futures balance from Binance."""
    if not cfg.binance_api_key_enc or not cfg.binance_api_secret_enc:
        return WalletBalance(
            mode=TradingMode.LIVE,
            source="paper_fallback",
            wallet_balance=float(cfg.paper_balance),
            available_balance=float(cfg.paper_balance),
            unrealized_pnl=0.0,
            locked_margin=0.0,
            equity=float(cfg.paper_balance),
            error="Live mode is active but Binance API keys are not configured.",
        )
    try:
        key = decrypt_secret(cfg.binance_api_key_enc)
        secret = decrypt_secret(cfg.binance_api_secret_enc)
        async with BinanceREST(testnet=cfg.binance_testnet) as rest:
            info = await rest.get_balance_info(key, secret)
    except Exception as e:  # noqa: BLE001
        log.error("wallet.live_fetch_failed", error=str(e))
        return WalletBalance(
            mode=TradingMode.LIVE,
            source="paper_fallback",
            wallet_balance=float(cfg.paper_balance),
            available_balance=float(cfg.paper_balance),
            unrealized_pnl=0.0,
            locked_margin=0.0,
            equity=float(cfg.paper_balance),
            error=f"Binance balance fetch failed: {e}",
        )

    return WalletBalance(
        mode=TradingMode.LIVE,
        source="binance",
        wallet_balance=info["wallet_balance"],
        available_balance=info["available_balance"],
        unrealized_pnl=info["unrealized_pnl"],
        locked_margin=info["initial_margin"],
        equity=info["margin_balance"],
    )


async def _paper_balance(session, cfg: UserConfig) -> WalletBalance:
    """Compute the paper wallet snapshot from the DB.

    We mirror scanner.engine._paper_equity so both places use the same
    numbers: realised P&L from closed trades + starting balance,
    minus margin still locked in TP1_HIT / PENDING trades.
    """
    # Sum realised P&L across ALL paper trades (partial fills booked
    # incrementally on TP1 already count via the paper executor's
    # realized_pnl_usdt updates).
    result = await session.execute(
        select(Trade).where(Trade.mode == TradingMode.PAPER)
    )
    all_trades = result.scalars().all()
    realised = sum(float(t.realized_pnl_usdt or 0.0) for t in all_trades)

    open_trades = [
        t for t in all_trades
        if t.status in (SignalStatus.OPEN, SignalStatus.TP1_HIT, SignalStatus.PENDING)
    ]

    locked_margin = 0.0
    unrealised = 0.0
    for t in open_trades:
        status_val = (
            t.status.value if hasattr(t.status, "value") else str(t.status)
        )
        # After TP1 half the position is closed; only half of the qty
        # is still on the books.
        remaining_qty = (
            t.quantity / 2.0 if status_val == "TP1_HIT" else t.quantity
        )
        locked_margin += (t.entry_price * remaining_qty) / max(t.leverage, 1)
        # Paper mode doesn't track live mark price here — leave unrealised
        # at 0. (Dashboard shows unrealised via WS price ticks anyway.)

    wallet = float(cfg.paper_balance) + realised
    available = max(wallet - locked_margin, 0.0)

    return WalletBalance(
        mode=TradingMode.PAPER,
        source="paper",
        wallet_balance=round(wallet, 6),
        available_balance=round(available, 6),
        unrealized_pnl=round(unrealised, 6),
        locked_margin=round(locked_margin, 6),
        equity=round(wallet + unrealised, 6),
    )
