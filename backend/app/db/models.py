"""SQLModel ORM models."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

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


# ==========================================================================
# Models
# ==========================================================================


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


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

    # Binance API (encrypted at rest)
    binance_api_key_enc: str = Field(default="")
    binance_api_secret_enc: str = Field(default="")
    binance_testnet: bool = Field(default=True)

    # Watchlist
    watchlist_csv: str = Field(default="BTCUSDT,ETHUSDT,SOLUSDT")

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

    # Paper trading equity
    paper_balance: float = Field(default=1000.0)

    updated_at: datetime = Field(
        default_factory=_utc_now,
        sa_column_kwargs={"onupdate": _utc_now},
    )


class Signal(SQLModel, table=True):
    """A generated trading signal from the strategy engine."""

    __tablename__ = "signals"

    id: int | None = Field(default=None, primary_key=True)
    created_at: datetime = Field(default_factory=_utc_now, index=True)

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

    # Correlated trade
    trade_id: Optional[int] = Field(default=None, foreign_key="trades.id", index=True)


class Trade(SQLModel, table=True):
    """A trade (either paper or real) resulting from a signal."""

    __tablename__ = "trades"

    id: int | None = Field(default=None, primary_key=True)
    created_at: datetime = Field(default_factory=_utc_now, index=True)
    closed_at: datetime | None = Field(default=None)

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

    status: SignalStatus = Field(default=SignalStatus.PENDING, index=True)
    notes: str = Field(default="")
