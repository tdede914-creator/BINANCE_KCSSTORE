"""SQLModel ORM models."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlalchemy import DateTime
from sqlmodel import JSON, Column, Field, SQLModel


# ==========================================================================
# Enums (stored as strings)
# ==========================================================================


class SignalSide(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class SignalStatus(str, Enum):
    """State machine for a signal → trade."""

    PENDING = "PENDING"          # signal fired, waiting entry fill
    OPEN = "OPEN"                # entry filled, SL/TP live
    TP1_HIT = "TP1_HIT"          # partial close taken, SL moved to BE
    CLOSED_TP = "CLOSED_TP"      # TP2 hit
    CLOSED_SL = "CLOSED_SL"      # SL hit
    CLOSED_MANUAL = "CLOSED_MANUAL"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class TradingMode(str, Enum):
    PAPER = "paper"
    LIVE = "live"


class MarketMode(str, Enum):
    """Which market universe the bot is analysing right now.

    - CRYPTO: Binance USDT-M Futures (execution enabled).
    - FOREX:  TwelveData feed for MT5-style pairs (signals-only; user
              executes manually in Exness or another MT5 broker).
    """

    CRYPTO = "crypto"
    FOREX = "forex"


class TrailingMode(str, Enum):
    """Trailing-stop algorithm mode.

    - OFF:     no trailing, SL is fixed (may still move to BE after TP1 hit)
    - ATR:     SL = extreme_price ± N × ATR(entry TF, snapshot at signal time)
    - PERCENT: SL = extreme_price × (1 ± X / 100)
    """

    OFF = "off"
    ATR = "atr"
    PERCENT = "percent"


# ==========================================================================
# Models
# ==========================================================================


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _dt_column(
    *,
    index: bool = False,
    nullable: bool = False,
    onupdate: bool = False,
) -> Column:
    """Return a timezone-aware TIMESTAMP column definition.

    Using ``DateTime(timezone=True)`` makes Postgres store ``TIMESTAMPTZ``,
    which is required because our Python code produces tz-aware datetimes
    (``datetime.now(tz=timezone.utc)``). Without ``timezone=True`` asyncpg
    rejects the insert with:
        'can't subtract offset-naive and offset-aware datetimes'.
    SQLite silently accepts both, which is why dev ran fine but Postgres
    prod did not.
    """
    kwargs: dict = {"nullable": nullable, "index": index}
    if onupdate:
        kwargs["onupdate"] = _utc_now
    return Column(DateTime(timezone=True), **kwargs)


class UserConfig(SQLModel, table=True):
    """Singleton row (id=1) — the bot's configuration.

    We keep this simple (single-user local install). Multi-user support
    would extend this to a per-user row.
    """

    __tablename__ = "user_config"

    id: int | None = Field(default=1, primary_key=True)

    # Trading mode
    trading_mode: TradingMode = Field(default=TradingMode.PAPER)
    scanner_enabled: bool = Field(default=False)

    # Market mode (crypto → Binance; forex → TwelveData, signals-only)
    market_mode: MarketMode = Field(default=MarketMode.CRYPTO)

    # Binance API (encrypted at rest) — used in CRYPTO mode only.
    binance_api_key_enc: str = Field(default="")
    binance_api_secret_enc: str = Field(default="")
    binance_testnet: bool = Field(default=True)

    # TwelveData API key (encrypted) — used in FOREX mode.
    twelvedata_api_key_enc: str = Field(default="")

    # Watchlist per market mode. The scanner reads the appropriate one
    # depending on ``market_mode``.
    watchlist_csv: str = Field(
        default=(
            "ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,DOGEUSDT,"
            "ADAUSDT,AVAXUSDT,LINKUSDT,SUIUSDT,1000PEPEUSDT"
        )
    )
    forex_watchlist_csv: str = Field(
        default="XAUUSD,EURUSD,GBPUSD,GBPJPY,AUDUSD,USDJPY,USDCAD,NZDUSD"
    )

    # Multi-timeframe
    bias_tf: str = Field(default="4h")
    setup_tf: str = Field(default="1h")
    entry_tf: str = Field(default="5m")

    # Risk
    risk_per_trade_pct: float = Field(default=1.0)
    leverage: int = Field(default=5)
    max_concurrent_positions: int = Field(default=3)

    # Strategy parameters
    ema_fast: int = Field(default=50)
    ema_slow: int = Field(default=200)
    ema_trigger: int = Field(default=20)
    rsi_period: int = Field(default=14)
    rsi_long_max: float = Field(default=75.0)
    rsi_short_min: float = Field(default=25.0)
    atr_period: int = Field(default=14)
    atr_sl_mult: float = Field(default=0.5)
    rr_tp1: float = Field(default=2.0)
    rr_tp2: float = Field(default=3.0)

    # ADX-based regime filter (skip trend signals in ranging markets).
    # Default: reject entries when ADX < 20 (Wilder-typical threshold).
    # Set adx_min = 0 to disable.
    adx_period: int = Field(default=14)
    adx_min: float = Field(default=20.0)

    # Volume confirmation: entry candle volume must be >= volume_mult
    # times its 20-bar moving average. 0 disables the check.
    volume_mult: float = Field(default=1.2)

    # ------------------------------------------------------------
    # Which strategies the scanner runs. Both default ON so the bot
    # can catch both trending pullbacks (MTF Confluence) and
    # post-consolidation breakouts (Range Breakout).
    # ------------------------------------------------------------
    mtf_confluence_enabled: bool = Field(default=True)
    range_breakout_enabled: bool = Field(default=True)

    # Range Breakout specific params (see StrategyContext for docs).
    rb_lookback: int = Field(default=30)
    rb_max_range_pct: float = Field(default=3.0)
    rb_atr_squeeze_ratio: float = Field(default=0.7)
    rb_breakout_buffer: float = Field(default=0.1)
    rb_measured_move_tp1: float = Field(default=1.0)
    rb_measured_move_tp2: float = Field(default=1.5)

    # Trailing stop
    trailing_mode: TrailingMode = Field(default=TrailingMode.OFF)
    # RR (in units of initial risk) that price must move in favor before
    # trailing "activates". 0 = trail from entry, 1 = trail after TP1, etc.
    trailing_activation_rr: float = Field(default=1.0)
    trailing_atr_mult: float = Field(default=1.5)
    trailing_percent: float = Field(default=1.0)  # 1.0 = 1%

    # Paper trading equity
    paper_balance: float = Field(default=1000.0)

    updated_at: datetime = Field(
        default_factory=_utc_now,
        sa_column=_dt_column(onupdate=True),
    )


class Signal(SQLModel, table=True):
    """A generated trading signal from the strategy engine."""

    __tablename__ = "signals"

    id: int | None = Field(default=None, primary_key=True)
    created_at: datetime = Field(
        default_factory=_utc_now,
        sa_column=_dt_column(index=True),
    )

    symbol: str = Field(index=True)
    side: SignalSide
    status: SignalStatus = Field(default=SignalStatus.PENDING, index=True)
    mode: TradingMode = Field(default=TradingMode.PAPER)

    # Timeframes used
    bias_tf: str
    setup_tf: str
    entry_tf: str

    # Prices at signal time
    entry_price: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float

    # Sizing
    leverage: int = Field(default=5)
    quantity: float = Field(default=0.0)
    risk_amount_usdt: float = Field(default=0.0)

    # Confidence / diagnostics (0..1)
    confidence: float = Field(default=0.0)
    reason: str = Field(default="")  # human-readable diagnostic text

    # Diagnostics dict (indicator snapshots)
    diagnostics: dict = Field(default_factory=dict, sa_column=Column(JSON))

    # Which strategy fired this signal — free-form string so we can add
    # new strategies without an enum migration. Legacy rows without
    # this column default to "mtf_confluence" (via the additive
    # migration in database.py).
    strategy: str = Field(default="mtf_confluence", index=True)

    # Correlated trade
    trade_id: Optional[int] = Field(default=None, foreign_key="trades.id", index=True)


class Trade(SQLModel, table=True):
    """A trade (either paper or real) resulting from a signal."""

    __tablename__ = "trades"

    id: int | None = Field(default=None, primary_key=True)
    created_at: datetime = Field(
        default_factory=_utc_now,
        sa_column=_dt_column(index=True),
    )
    closed_at: datetime | None = Field(
        default=None,
        sa_column=_dt_column(nullable=True),
    )

    signal_id: Optional[int] = Field(default=None, foreign_key="signals.id", index=True)

    mode: TradingMode
    symbol: str = Field(index=True)
    side: SignalSide
    leverage: int

    # Entry
    entry_price: float
    quantity: float

    # Risk plan
    stop_loss: float
    take_profit_1: float
    take_profit_2: float

    # Realized
    exit_price: float | None = Field(default=None)
    realized_pnl_usdt: float | None = Field(default=None)
    realized_pnl_pct: float | None = Field(default=None)  # of margin used
    fee_usdt: float = Field(default=0.0)

    # Binance order ids (live mode only)
    entry_order_id: str | None = Field(default=None)
    sl_order_id: str | None = Field(default=None)
    tp1_order_id: str | None = Field(default=None)
    tp2_order_id: str | None = Field(default=None)

    # Trailing stop state — populated at trade open, updated by reconciler
    trailing_mode: TrailingMode = Field(default=TrailingMode.OFF)
    trailing_activation_rr: float = Field(default=1.0)
    trailing_atr_mult: float = Field(default=1.5)
    trailing_percent: float = Field(default=1.0)
    trailing_atr_snapshot: float = Field(default=0.0)  # ATR at signal time, cached
    trailing_active: bool = Field(default=False)       # activation threshold crossed?
    highest_price: float | None = Field(default=None)  # for LONG
    lowest_price: float | None = Field(default=None)   # for SHORT
    initial_sl: float | None = Field(default=None)     # remember original SL for RR calc

    status: SignalStatus = Field(default=SignalStatus.PENDING, index=True)
    notes: str = Field(default="")
