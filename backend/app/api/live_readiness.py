"""LIVE-mode readiness pre-flight.

Answers the question: *"If I flip trading_mode to LIVE right now, will
the executor actually be able to send an order?"*

We test the three most common failure modes in order:

1. API key stored / non-empty?
2. Binance accepts the key at all (any authenticated call)?
3. Balance endpoint responds (needs the specific "futures" permission)?

Users hit this via the dashboard so they can catch a broken setup
BEFORE they toggle LIVE — much better UX than staring at an empty
scanner panel because a 401 killed the tick loop.
"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.binance.rest import BinanceREST
from app.core.config import settings
from app.core.logging import get_logger
from app.core.security import decrypt_secret
from app.db.database import session_scope
from app.db.models import UserConfig
from sqlmodel import select

router = APIRouter()
log = get_logger(__name__)


class Check(BaseModel):
    name: str
    ok: bool
    detail: str


class LiveReadiness(BaseModel):
    ready: bool                 # overall — True only if all critical checks pass
    checks: list[Check]         # one row per sub-check
    balance_usdt: float | None  # convenience: current wallet if we could fetch it


@router.get("/live-readiness", response_model=LiveReadiness)
async def live_readiness() -> LiveReadiness:
    checks: list[Check] = []

    async with session_scope() as session:
        result = await session.execute(select(UserConfig).limit(1))
        cfg = result.scalars().first()

    if cfg is None:
        checks.append(Check(name="config", ok=False, detail="no user_config row"))
        return LiveReadiness(ready=False, checks=checks, balance_usdt=None)

    # ------------------------------------------------------------------
    # 1) Keys stored?
    # ------------------------------------------------------------------
    if not cfg.binance_api_key_enc or not cfg.binance_api_secret_enc:
        checks.append(
            Check(
                name="api_keys",
                ok=False,
                detail=(
                    "Binance API key/secret not saved. Go to Settings → "
                    "Binance API Keys and paste them (Futures permission "
                    "required, withdrawals NOT required)."
                ),
            )
        )
        return LiveReadiness(ready=False, checks=checks, balance_usdt=None)

    checks.append(
        Check(name="api_keys", ok=True, detail="key + secret present (encrypted)")
    )

    # ------------------------------------------------------------------
    # 2) Can we authenticate & fetch balance?
    # ------------------------------------------------------------------
    try:
        key = decrypt_secret(cfg.binance_api_key_enc)
        secret = decrypt_secret(cfg.binance_api_secret_enc)
    except Exception as e:  # noqa: BLE001
        checks.append(
            Check(
                name="decrypt",
                ok=False,
                detail=f"Cannot decrypt stored key ({e}). Re-save the key.",
            )
        )
        return LiveReadiness(ready=False, checks=checks, balance_usdt=None)

    base_url = (
        settings.BINANCE_FUTURES_REST_TESTNET
        if cfg.binance_testnet
        else settings.BINANCE_FUTURES_REST_MAINNET
    )

    balance: float | None = None
    try:
        async with BinanceREST(base_url=base_url) as rest:
            balance = await rest.get_balance_usdt(key, secret)
        checks.append(
            Check(
                name="binance_auth",
                ok=True,
                detail=(
                    f"authenticated on {'TESTNET' if cfg.binance_testnet else 'MAINNET'}"
                ),
            )
        )
        checks.append(
            Check(
                name="futures_balance",
                ok=True,
                detail=f"USDT available = {balance:.4f}",
            )
        )
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {e}"
        # Try to classify common failure reasons for actionable messages.
        hint = err
        low = err.lower()
        if "-2015" in err or "invalid api-key" in low or "ip" in low and "whitelist" in low:
            hint = (
                "Binance rejected the key. Most common: your VPS IP is "
                "not on the API key whitelist. Add the VPS public IP to "
                "the key's IP restriction list in Binance API Management, "
                "or disable IP restriction (less safe)."
            )
        elif "-2014" in err or "signature" in low:
            hint = (
                "Signature invalid — the stored secret is corrupted or "
                "the wrong secret was paired with the key. Re-save the "
                "keys from Settings."
            )
        elif "permission" in low or "-2015" in err:
            hint = (
                "Key exists but the required permission (Enable Futures) "
                "is not granted. Edit the key in Binance and enable it."
            )
        elif "timestamp" in low or "recvwindow" in low:
            hint = (
                "Server clock skew. Sync the VPS clock: "
                "'sudo timedatectl set-ntp on' then reboot the backend."
            )
        checks.append(Check(name="binance_auth", ok=False, detail=hint))
        return LiveReadiness(ready=False, checks=checks, balance_usdt=None)

    # ------------------------------------------------------------------
    # 3) Warn if balance is zero (auth ok but wallet empty).
    # ------------------------------------------------------------------
    if balance is not None and balance <= 0:
        checks.append(
            Check(
                name="funds",
                ok=False,
                detail=(
                    "USDT wallet balance is 0. Transfer USDT from Spot to "
                    "Futures wallet in Binance before enabling LIVE."
                ),
            )
        )
        return LiveReadiness(ready=False, checks=checks, balance_usdt=balance)

    return LiveReadiness(ready=True, checks=checks, balance_usdt=balance)
