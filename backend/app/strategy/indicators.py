"""Pure-function technical indicators.

All functions take a pandas DataFrame with columns
``open, high, low, close, volume`` and return a Series or a dataclass of
computed values. We intentionally implement the indicators from scratch
(instead of relying on `pandas-ta`) so the code has no hidden state and
is easy to unit test.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


# ==========================================================================
# Trend / momentum
# ==========================================================================


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential moving average."""
    return series.ewm(span=period, adjust=False).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period, min_periods=period).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI."""
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)

    # Wilder smoothing = EMA with alpha = 1/period
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100.0 - (100.0 / (1.0 + rs))
    return out.fillna(50.0)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range (Wilder)."""
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)

    tr = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average Directional Index (Wilder, 1978).

    ADX measures *trend strength* independent of direction. A common
    heuristic among trend-following systems:

        ADX < 20   → market is ranging / choppy → skip trend trades
        ADX 20-40  → trend present, tradeable
        ADX > 40   → very strong trend (but often already exhausted)

    Implementation follows Wilder's original: TR + directional movement
    are smoothed via RMA (equivalent to EMA with alpha=1/period), then
    DI+/DI- computed, then DX, then ADX = smoothed DX.
    """
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)

    # True range (same as atr() but we need the series here)
    tr = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    # Directional movement
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0),
        index=df.index,
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0),
        index=df.index,
    )

    alpha = 1.0 / period
    atr_s = tr.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    plus_di = 100.0 * plus_dm.ewm(alpha=alpha, adjust=False, min_periods=period).mean() / atr_s
    minus_di = 100.0 * minus_dm.ewm(alpha=alpha, adjust=False, min_periods=period).mean() / atr_s

    dx = (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan) * 100.0
    adx_s = dx.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    return adx_s.fillna(0.0)


# ==========================================================================
# Structure detection
# ==========================================================================


@dataclass(slots=True)
class SwingPoint:
    index: int          # position in the DataFrame
    price: float
    is_high: bool

    def __repr__(self) -> str:
        kind = "H" if self.is_high else "L"
        return f"Swing{kind}(i={self.index}, p={self.price:.4f})"


def find_swings(df: pd.DataFrame, left: int = 3, right: int = 3) -> list[SwingPoint]:
    """Locate swing highs / lows using a fractal-style pivot detection.

    A candle at index i is a swing high if its high is >= all highs in
    the ``left`` candles before and ``right`` candles after it. Analogous
    for swing lows.
    """
    highs = df["high"].values
    lows = df["low"].values
    n = len(df)
    swings: list[SwingPoint] = []

    for i in range(left, n - right):
        window_high = highs[i - left : i + right + 1]
        window_low = lows[i - left : i + right + 1]

        if highs[i] == window_high.max() and (window_high == highs[i]).sum() == 1:
            swings.append(SwingPoint(index=i, price=float(highs[i]), is_high=True))
        if lows[i] == window_low.min() and (window_low == lows[i]).sum() == 1:
            swings.append(SwingPoint(index=i, price=float(lows[i]), is_high=False))

    return swings


def last_swing_high(swings: list[SwingPoint], before_index: int | None = None) -> SwingPoint | None:
    cand = [s for s in swings if s.is_high and (before_index is None or s.index < before_index)]
    return cand[-1] if cand else None


def last_swing_low(swings: list[SwingPoint], before_index: int | None = None) -> SwingPoint | None:
    cand = [s for s in swings if not s.is_high and (before_index is None or s.index < before_index)]
    return cand[-1] if cand else None


# ==========================================================================
# Support / Resistance zones (from clustered swings)
# ==========================================================================


@dataclass(slots=True)
class SRZone:
    price: float
    kind: str           # "support" | "resistance"
    touches: int        # how many swings clustered here
    last_touch_index: int


def sr_zones(
    df: pd.DataFrame,
    swings: list[SwingPoint],
    cluster_pct: float = 0.003,  # 0.3% price radius
    min_touches: int = 2,
) -> list[SRZone]:
    """Cluster swings into support/resistance zones by proximity."""
    zones: list[SRZone] = []

    def _cluster(points: list[SwingPoint], kind: str) -> list[SRZone]:
        result: list[SRZone] = []
        for pt in points:
            merged = False
            for z in result:
                if abs(z.price - pt.price) / max(z.price, 1e-9) <= cluster_pct:
                    # merge — running mean
                    z.price = (z.price * z.touches + pt.price) / (z.touches + 1)
                    z.touches += 1
                    z.last_touch_index = max(z.last_touch_index, pt.index)
                    merged = True
                    break
            if not merged:
                result.append(
                    SRZone(
                        price=pt.price,
                        kind=kind,
                        touches=1,
                        last_touch_index=pt.index,
                    )
                )
        return [z for z in result if z.touches >= min_touches]

    zones.extend(_cluster([s for s in swings if s.is_high], "resistance"))
    zones.extend(_cluster([s for s in swings if not s.is_high], "support"))
    return zones


# ==========================================================================
# Order Block detection (simplified SMC)
# ==========================================================================


@dataclass(slots=True)
class OrderBlock:
    index: int
    high: float
    low: float
    kind: str  # "bull" | "bear"


def find_order_blocks(
    df: pd.DataFrame,
    lookback: int = 100,
    displacement_atr_mult: float = 1.5,
) -> list[OrderBlock]:
    """Detect basic bullish/bearish order blocks.

    A **bullish OB** = last down-close candle immediately before a strong
    up-move (displacement candle with body >= displacement_atr_mult * ATR).
    Analogous for bearish OB.
    """
    if len(df) < lookback + 5:
        return []

    tail = df.iloc[-lookback:]
    atr_series = atr(tail, period=14).fillna(method="ffill")
    blocks: list[OrderBlock] = []

    for i in range(1, len(tail) - 1):
        curr = tail.iloc[i]
        prev = tail.iloc[i - 1]
        body = abs(curr["close"] - curr["open"])
        curr_atr = atr_series.iloc[i]
        if pd.isna(curr_atr) or curr_atr == 0:
            continue
        if body < displacement_atr_mult * curr_atr:
            continue

        if curr["close"] > curr["open"] and prev["close"] < prev["open"]:
            # bullish displacement after a down candle → prev = bullish OB
            blocks.append(
                OrderBlock(
                    index=len(df) - len(tail) + (i - 1),
                    high=float(prev["high"]),
                    low=float(prev["low"]),
                    kind="bull",
                )
            )
        elif curr["close"] < curr["open"] and prev["close"] > prev["open"]:
            blocks.append(
                OrderBlock(
                    index=len(df) - len(tail) + (i - 1),
                    high=float(prev["high"]),
                    low=float(prev["low"]),
                    kind="bear",
                )
            )

    return blocks


# ==========================================================================
# Volume confirmation
# ==========================================================================


def volume_ma(series: pd.Series, period: int = 20) -> pd.Series:
    return series.rolling(window=period, min_periods=1).mean()


# ==========================================================================
# Composite convenience: enrich a klines DataFrame with all indicators.
# ==========================================================================


_ENRICH_COLUMNS = ("ema_fast", "ema_slow", "ema_trigger", "rsi", "atr", "adx", "vol_ma")


def enrich(
    df: pd.DataFrame,
    *,
    ema_fast: int = 50,
    ema_slow: int = 200,
    ema_trigger: int = 20,
    rsi_period: int = 14,
    atr_period: int = 14,
    adx_period: int = 14,
) -> pd.DataFrame:
    """Return a copy of ``df`` with indicator columns added.

    Columns added:
        ema_fast, ema_slow, ema_trigger, rsi, atr, adx, vol_ma

    Fast path: if ``df`` already has ALL indicator columns (e.g. because
    the backtest engine pre-enriched it once and then sliced), we
    return it as-is instead of recomputing. Slicing pandas Series and
    calling ewm() 17k+ times was the main bottleneck of the naive
    backtest (multi-minute runs at 5m/60d).
    """
    if all(col in df.columns for col in _ENRICH_COLUMNS):
        return df
    out = df.copy()
    out["ema_fast"] = ema(out["close"], ema_fast)
    out["ema_slow"] = ema(out["close"], ema_slow)
    out["ema_trigger"] = ema(out["close"], ema_trigger)
    out["rsi"] = rsi(out["close"], rsi_period)
    out["atr"] = atr(out, atr_period)
    out["adx"] = adx(out, adx_period)
    out["vol_ma"] = volume_ma(out["volume"], 20)
    return out
