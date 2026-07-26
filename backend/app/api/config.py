"""Config endpoints — read/update the singleton UserConfig row.

Binance API keys are encrypted with Fernet before being stored. They are
NEVER returned to the frontend in plaintext.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.api.deps import SessionDep, get_or_create_config
from app.binance.rest import VALID_TIMEFRAMES, BinanceREST
from sqlalchemy import delete, update

from app.core.logging import get_logger
from app.core.security import decrypt_secret, encrypt_secret, mask_key
from app.db.models import MarketMode, Signal, Trade, TradingMode, TrailingMode, UserConfig

router = APIRouter()


# ---------- Schemas ----------


class ConfigOut(BaseModel):
    trading_mode: TradingMode
    scanner_enabled: bool
    market_mode: MarketMode

    binance_api_key_masked: str
    binance_api_configured: bool
    binance_testnet: bool

    twelvedata_configured: bool

    watchlist: list[str]
    forex_watchlist: list[str]

    bias_tf: str
    setup_tf: str
    entry_tf: str

    risk_per_trade_pct: float
    leverage: int
    max_concurrent_positions: int

    ema_fast: int
    ema_slow: int
    ema_trigger: int
    rsi_period: int
    rsi_long_max: float
    rsi_short_min: float
    atr_period: int
    atr_sl_mult: float
    rr_tp1: float
    rr_tp2: float

    # Regime + volume filters
    adx_period: int
    adx_min: float
    volume_mult: float

    # Trailing stop
    trailing_mode: TrailingMode
    trailing_activation_rr: float
    trailing_atr_mult: float
    trailing_percent: float

    paper_balance: float


class ConfigUpdate(BaseModel):
    trading_mode: TradingMode | None = None
    scanner_enabled: bool | None = None
    market_mode: MarketMode | None = None
    binance_testnet: bool | None = None
    watchlist: list[str] | None = None
    forex_watchlist: list[str] | None = None
    bias_tf: str | None = None
    setup_tf: str | None = None
    entry_tf: str | None = None
    risk_per_trade_pct: float | None = Field(default=None, ge=0.01, le=10.0)
    leverage: int | None = Field(default=None, ge=1, le=125)
    max_concurrent_positions: int | None = Field(default=None, ge=1, le=20)
    ema_fast: int | None = None
    ema_slow: int | None = None
    ema_trigger: int | None = None
    rsi_period: int | None = None
    rsi_long_max: float | None = None
    rsi_short_min: float | None = None
    atr_period: int | None = None
    atr_sl_mult: float | None = None
    rr_tp1: float | None = None
    rr_tp2: float | None = None
    adx_period: int | None = Field(default=None, ge=2, le=100)
    adx_min: float | None = Field(default=None, ge=0.0, le=60.0)
    volume_mult: float | None = Field(default=None, ge=0.0, le=5.0)

    # Trailing stop
    trailing_mode: TrailingMode | None = None
    trailing_activation_rr: float | None = Field(default=None, ge=0.0, le=20.0)
    trailing_atr_mult: float | None = Field(default=None, ge=0.1, le=10.0)
    trailing_percent: float | None = Field(default=None, ge=0.05, le=20.0)

    paper_balance: float | None = None


class BinanceKeyUpdate(BaseModel):
    api_key: str = Field(min_length=8)
    api_secret: str = Field(min_length=8)
    testnet: bool = True


class TwelveDataKeyUpdate(BaseModel):
    api_key: str = Field(min_length=8)


# ---------- Helpers ----------


def _to_out(cfg: UserConfig) -> ConfigOut:
    key = decrypt_secret(cfg.binance_api_key_enc) if cfg.binance_api_key_enc else ""
    return ConfigOut(
        trading_mode=cfg.trading_mode,
        scanner_enabled=cfg.scanner_enabled,
        market_mode=cfg.market_mode,
        binance_api_key_masked=mask_key(key),
        binance_api_configured=bool(cfg.binance_api_key_enc),
        binance_testnet=cfg.binance_testnet,
        twelvedata_configured=bool(cfg.twelvedata_api_key_enc),
        watchlist=[s for s in cfg.watchlist_csv.split(",") if s],
        forex_watchlist=[s for s in cfg.forex_watchlist_csv.split(",") if s],
        bias_tf=cfg.bias_tf,
        setup_tf=cfg.setup_tf,
        entry_tf=cfg.entry_tf,
        risk_per_trade_pct=cfg.risk_per_trade_pct,
        leverage=cfg.leverage,
        max_concurrent_positions=cfg.max_concurrent_positions,
        ema_fast=cfg.ema_fast,
        ema_slow=cfg.ema_slow,
        ema_trigger=cfg.ema_trigger,
        rsi_period=cfg.rsi_period,
        rsi_long_max=cfg.rsi_long_max,
        rsi_short_min=cfg.rsi_short_min,
        atr_period=cfg.atr_period,
        atr_sl_mult=cfg.atr_sl_mult,
        rr_tp1=cfg.rr_tp1,
        rr_tp2=cfg.rr_tp2,
        adx_period=cfg.adx_period,
        adx_min=cfg.adx_min,
        volume_mult=cfg.volume_mult,
        trailing_mode=cfg.trailing_mode,
        trailing_activation_rr=cfg.trailing_activation_rr,
        trailing_atr_mult=cfg.trailing_atr_mult,
        trailing_percent=cfg.trailing_percent,
        paper_balance=cfg.paper_balance,
    )


# ---------- Endpoints ----------


@router.get("", response_model=ConfigOut)
async def read_config(session: SessionDep) -> ConfigOut:
    cfg = await get_or_create_config(session)
    return _to_out(cfg)


@router.patch("", response_model=ConfigOut)
async def update_config(body: ConfigUpdate, session: SessionDep) -> ConfigOut:
    cfg = await get_or_create_config(session)

    updates = body.model_dump(exclude_none=True)
    if "watchlist" in updates:
        updates["watchlist_csv"] = ",".join(s.upper().strip() for s in updates.pop("watchlist"))
    if "forex_watchlist" in updates:
        updates["forex_watchlist_csv"] = ",".join(
            s.upper().strip() for s in updates.pop("forex_watchlist")
        )

    # Validate timeframes.
    for tf_field in ("bias_tf", "setup_tf", "entry_tf"):
        if tf_field in updates and updates[tf_field] not in VALID_TIMEFRAMES:
            raise HTTPException(400, f"invalid {tf_field}: {updates[tf_field]}")

    for k, v in updates.items():
        setattr(cfg, k, v)

    session.add(cfg)
    await session.commit()
    await session.refresh(cfg)
    return _to_out(cfg)


@router.post("/binance-keys", response_model=ConfigOut)
async def save_binance_keys(body: BinanceKeyUpdate, session: SessionDep) -> ConfigOut:
    cfg = await get_or_create_config(session)
    cfg.binance_api_key_enc = encrypt_secret(body.api_key.strip())
    cfg.binance_api_secret_enc = encrypt_secret(body.api_secret.strip())
    cfg.binance_testnet = body.testnet
    session.add(cfg)
    await session.commit()
    await session.refresh(cfg)
    return _to_out(cfg)


@router.delete("/binance-keys", response_model=ConfigOut)
async def delete_binance_keys(session: SessionDep) -> ConfigOut:
    cfg = await get_or_create_config(session)
    cfg.binance_api_key_enc = ""
    cfg.binance_api_secret_enc = ""
    session.add(cfg)
    await session.commit()
    await session.refresh(cfg)
    return _to_out(cfg)


@router.post("/twelvedata-key", response_model=ConfigOut)
async def save_twelvedata_key(
    body: TwelveDataKeyUpdate, session: SessionDep
) -> ConfigOut:
    """Store the TwelveData API key (encrypted). Used in FOREX market mode."""
    cfg = await get_or_create_config(session)
    cfg.twelvedata_api_key_enc = encrypt_secret(body.api_key.strip())
    session.add(cfg)
    await session.commit()
    await session.refresh(cfg)
    return _to_out(cfg)


@router.delete("/twelvedata-key", response_model=ConfigOut)
async def delete_twelvedata_key(session: SessionDep) -> ConfigOut:
    cfg = await get_or_create_config(session)
    cfg.twelvedata_api_key_enc = ""
    session.add(cfg)
    await session.commit()
    await session.refresh(cfg)
    return _to_out(cfg)


# --------------------------------------------------------------------------
# Paper-mode reset
# --------------------------------------------------------------------------


class PaperResetRequest(BaseModel):
    new_balance: float | None = Field(default=None, ge=0.01, le=10_000_000)


class PaperResetResponse(BaseModel):
    config: ConfigOut
    trades_deleted: int
    signals_deleted: int


@router.post("/paper/reset", response_model=PaperResetResponse)
async def reset_paper(body: PaperResetRequest, session: SessionDep) -> PaperResetResponse:
    """Nuke every paper trade and paper signal, and optionally set a new
    starting balance.

    LIVE trades and LIVE signals are never touched — the filter keys on
    ``mode == 'paper'``. Useful for wiping the ledger before running a
    fresh experiment (e.g. testing whether the strategy works at a tiny
    $10 modal).
    """
    log = get_logger(__name__)
    log.info("reset_paper.start", new_balance=body.new_balance)

    try:
        cfg = await get_or_create_config(session)

        # signals.trade_id → trades.id AND trades.signal_id → signals.id
        # form a circular FK, so a naive DELETE of either table hits a
        # ForeignKeyViolationError. NULL both directions first, then the
        # deletes are independent and can happen in either order.
        await session.execute(
            update(Signal)
            .where(Signal.mode == TradingMode.PAPER)
            .values(trade_id=None)
        )
        await session.execute(
            update(Trade)
            .where(Trade.mode == TradingMode.PAPER)
            .values(signal_id=None)
        )

        signals_result = await session.execute(
            delete(Signal).where(Signal.mode == TradingMode.PAPER)
        )
        signals_deleted = signals_result.rowcount or 0

        trades_result = await session.execute(
            delete(Trade).where(Trade.mode == TradingMode.PAPER)
        )
        trades_deleted = trades_result.rowcount or 0

        if body.new_balance is not None:
            cfg.paper_balance = float(body.new_balance)

        session.add(cfg)
        await session.commit()
        await session.refresh(cfg)

        log.info(
            "reset_paper.done",
            trades_deleted=trades_deleted,
            signals_deleted=signals_deleted,
            new_balance=cfg.paper_balance,
        )

        return PaperResetResponse(
            config=_to_out(cfg),
            trades_deleted=trades_deleted,
            signals_deleted=signals_deleted,
        )
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        # Log the full traceback for diagnosis but return a clean error
        # to the browser instead of dropping the connection (which would
        # surface as a generic 'Failed to fetch').
        log.exception("reset_paper.failed", error=str(e))
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001
            pass
        raise HTTPException(500, f"Reset failed: {type(e).__name__}: {e}") from e


@router.post("/binance-keys/test")
async def test_binance_keys(session: SessionDep) -> dict:
    """Try to read the account balance to verify the stored keys work."""
    cfg = await get_or_create_config(session)
    if not cfg.binance_api_key_enc:
        raise HTTPException(400, "No API keys configured")
    key = decrypt_secret(cfg.binance_api_key_enc)
    secret = decrypt_secret(cfg.binance_api_secret_enc)
    async with BinanceREST() as rest:
        try:
            bal = await rest.get_balance_usdt(key, secret)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"Binance rejected keys: {e}") from e
    return {"ok": True, "balance_usdt": bal, "testnet": cfg.binance_testnet}
