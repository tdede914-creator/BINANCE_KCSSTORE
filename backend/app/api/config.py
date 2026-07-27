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

    # Strategy toggles
    mtf_confluence_enabled: bool
    range_breakout_enabled: bool

    # Range Breakout params
    rb_lookback: int
    rb_max_range_pct: float
    rb_atr_squeeze_ratio: float
    rb_breakout_buffer: float
    rb_measured_move_tp1: float
    rb_measured_move_tp2: float

    # Trailing stop
    trailing_mode: TrailingMode
    trailing_activation_rr: float
    trailing_atr_mult: float
    trailing_percent: float

    paper_balance: float

    # Telegram — token intentionally NOT exposed (write-only via
    # dedicated PATCH); we surface configured=True so the UI can
    # show a "clear key" affordance without leaking the token itself.
    telegram_enabled: bool
    telegram_configured: bool
    telegram_chat_id: str
    telegram_notify_signals: bool
    telegram_notify_trades: bool
    telegram_notify_hourly_balance: bool
    telegram_balance_interval_min: int


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

    # Strategy toggles + Range Breakout params
    mtf_confluence_enabled: bool | None = None
    range_breakout_enabled: bool | None = None
    rb_lookback: int | None = Field(default=None, ge=5, le=200)
    rb_max_range_pct: float | None = Field(default=None, ge=0.1, le=20.0)
    rb_atr_squeeze_ratio: float | None = Field(default=None, ge=0.1, le=2.0)
    rb_breakout_buffer: float | None = Field(default=None, ge=0.0, le=2.0)
    rb_measured_move_tp1: float | None = Field(default=None, ge=0.1, le=10.0)
    rb_measured_move_tp2: float | None = Field(default=None, ge=0.1, le=20.0)

    # Trailing stop
    trailing_mode: TrailingMode | None = None
    trailing_activation_rr: float | None = Field(default=None, ge=0.0, le=20.0)
    trailing_atr_mult: float | None = Field(default=None, ge=0.1, le=10.0)
    trailing_percent: float | None = Field(default=None, ge=0.05, le=20.0)

    paper_balance: float | None = None

    # Telegram notification toggles (token is set via dedicated endpoint)
    telegram_enabled: bool | None = None
    telegram_chat_id: str | None = None
    telegram_notify_signals: bool | None = None
    telegram_notify_trades: bool | None = None
    telegram_notify_hourly_balance: bool | None = None
    telegram_balance_interval_min: int | None = Field(default=None, ge=5, le=1440)


class TelegramTokenUpdate(BaseModel):
    bot_token: str = Field(..., min_length=20, max_length=200)


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
        mtf_confluence_enabled=cfg.mtf_confluence_enabled,
        range_breakout_enabled=cfg.range_breakout_enabled,
        rb_lookback=cfg.rb_lookback,
        rb_max_range_pct=cfg.rb_max_range_pct,
        rb_atr_squeeze_ratio=cfg.rb_atr_squeeze_ratio,
        rb_breakout_buffer=cfg.rb_breakout_buffer,
        rb_measured_move_tp1=cfg.rb_measured_move_tp1,
        rb_measured_move_tp2=cfg.rb_measured_move_tp2,
        telegram_enabled=cfg.telegram_enabled,
        telegram_configured=bool(cfg.telegram_bot_token_enc),
        telegram_chat_id=cfg.telegram_chat_id,
        telegram_notify_signals=cfg.telegram_notify_signals,
        telegram_notify_trades=cfg.telegram_notify_trades,
        telegram_notify_hourly_balance=cfg.telegram_notify_hourly_balance,
        telegram_balance_interval_min=cfg.telegram_balance_interval_min,
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


# ---------- Telegram bot token + test message ----------


@router.post("/telegram-token", response_model=ConfigOut)
async def set_telegram_token(body: TelegramTokenUpdate, session: SessionDep) -> ConfigOut:
    """Store the bot token (Fernet-encrypted). Doesn't validate the token —
    users can hit POST /telegram/test afterwards to send a live probe."""
    cfg = await get_or_create_config(session)
    cfg.telegram_bot_token_enc = encrypt_secret(body.bot_token.strip())
    session.add(cfg)
    await session.commit()
    await session.refresh(cfg)
    return _to_out(cfg)


@router.delete("/telegram-token", response_model=ConfigOut)
async def delete_telegram_token(session: SessionDep) -> ConfigOut:
    """Clear the stored bot token and turn Telegram notifications off."""
    cfg = await get_or_create_config(session)
    cfg.telegram_bot_token_enc = ""
    cfg.telegram_enabled = False
    session.add(cfg)
    await session.commit()
    await session.refresh(cfg)
    return _to_out(cfg)


@router.post("/telegram/detect-chat-id")
async def detect_telegram_chat_id(session: SessionDep) -> dict:
    """Call getUpdates on the stored bot token and pull the newest chat ID.

    User workflow:
      1. Save bot token in Settings.
      2. Open Telegram, send /start (or anything) to the bot.
      3. Click 'Auto-detect chat ID' — this endpoint runs.

    We look at the most recent update and extract chat.id. If nothing
    comes back, the user probably hasn't messaged the bot yet, or
    another poller consumed the queue. In either case we return a
    helpful error rather than silently saving 0.
    """
    import httpx
    cfg = await get_or_create_config(session)
    if not cfg.telegram_bot_token_enc:
        return {"ok": False, "error": "Bot token not set. Save it first."}
    try:
        token = decrypt_secret(cfg.telegram_bot_token_enc)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"Cannot decrypt token: {e}"}

    url = f"https://api.telegram.org/bot{token}/getUpdates"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"HTTP error: {e}"}

    if resp.status_code != 200:
        return {
            "ok": False,
            "error": f"Telegram API returned {resp.status_code}: {resp.text[:200]}",
        }

    data = resp.json()
    updates = data.get("result", [])
    if not updates:
        return {
            "ok": False,
            "error": (
                "No messages found. Send any message to your bot on "
                "Telegram (e.g. /start), then click this button again. "
                "If it still fails, another process may have polled the "
                "queue empty — talk to @userinfobot for a manual lookup."
            ),
        }

    # Walk updates newest-first, pick the first one that has a private chat id.
    chat_id: int | None = None
    for update in reversed(updates):
        for key in ("message", "edited_message", "channel_post", "callback_query"):
            payload = update.get(key)
            if not payload:
                continue
            chat = payload.get("chat") or payload.get("from")
            if chat and "id" in chat:
                chat_id = chat["id"]
                break
        if chat_id is not None:
            break

    if chat_id is None:
        return {
            "ok": False,
            "error": "Updates contain no chat/from IDs. Send a normal message (not a system event) to the bot and try again.",
        }

    cfg.telegram_chat_id = str(chat_id)
    session.add(cfg)
    await session.commit()
    await session.refresh(cfg)
    return {"ok": True, "chat_id": str(chat_id), "error": None}


@router.post("/telegram/test")
async def send_test_telegram(session: SessionDep) -> dict:
    """Try to send a hello-world message with the CURRENTLY stored token.

    Returns ``{ok: bool, error: str | null}``. This is the fastest way for
    users to confirm bot token + chat_id are correct before they wait for
    a real signal.
    """
    from app.telegram import notifier as tg_notifier
    cfg = await get_or_create_config(session)
    if not cfg.telegram_bot_token_enc:
        return {"ok": False, "error": "Bot token not set. Save it first."}
    if not cfg.telegram_chat_id:
        return {"ok": False, "error": "Chat ID is empty. Fill it in and Save."}
    # We temporarily flip 'telegram_enabled' so notifier.send_message
    # doesn't short-circuit — the user is explicitly asking for a probe.
    original_enabled = cfg.telegram_enabled
    cfg.telegram_enabled = True
    try:
        ok = await tg_notifier.send_message(
            cfg,
            "✅ *Test message* from your signal bot.\n\n"
            "Notifications are wired up correctly. You can now enable "
            "signal / trade / hourly-balance alerts in Settings.",
        )
    finally:
        cfg.telegram_enabled = original_enabled
    if not ok:
        return {
            "ok": False,
            "error": (
                "Telegram API did not accept the message. Double-check "
                "the bot token, that the chat ID is your user (not @username), "
                "and that you've sent /start to the bot first."
            ),
        }
    return {"ok": True, "error": None}


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
