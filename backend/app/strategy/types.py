"""Common types for the strategy layer."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Bias(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    NEUTRAL = "NEUTRAL"


class Side(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


@dataclass(slots=True)
class SignalProposal:
    """Output of the strategy engine before risk/sizing is applied."""

    symbol: str
    side: Side
    entry_price: float

    # Suggested SL/TP from strategy (raw, before risk manager may adjust)
    stop_loss: float
    take_profit_1: float
    take_profit_2: float

    # Timeframes used
    bias_tf: str
    setup_tf: str
    entry_tf: str

    confidence: float  # 0..1
    reason: str = ""
    diagnostics: dict = field(default_factory=dict)


@dataclass(slots=True)
class StrategyContext:
    """User-tunable strategy parameters."""

    ema_fast: int = 50
    ema_slow: int = 200
    ema_trigger: int = 20
    rsi_period: int = 14
    rsi_long_max: float = 75.0
    rsi_short_min: float = 25.0
    atr_period: int = 14
    atr_sl_mult: float = 0.5
    rr_tp1: float = 2.0
    rr_tp2: float = 3.0
    setup_max_atr_distance: float = 1.5  # price must be within N × ATR of setup zone

    # Regime filter — reject signals when the market is ranging.
    # ADX below ``adx_min`` (Wilder default 20) means no trend, and
    # trend-following signals fail there. Set adx_min=0 to disable.
    adx_period: int = 14
    adx_min: float = 20.0

    # Volume confirmation — require the entry candle's volume to exceed
    # ``volume_mult`` × its 20-period average. 1.0 = must beat average,
    # 1.5 = spike required, 0 = disable.
    volume_mult: float = 1.2
