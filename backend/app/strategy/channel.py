"""Parallel channel detection.

Two algorithms are supported and returned in the same shape so callers
(FastAPI, frontend chart) don't care which one produced the channel:

**Pivot channel** (default)
    Draws the channel through actual swing pivots — the classical
    manual-trader technique. For an uptrend, it connects the two most
    recent higher-lows and parallels through the highest high. For a
    downtrend, it connects the two most recent lower-highs and parallels
    through the lowest low. Result: the channel edges pass *exactly*
    through pivot candles, matching how a trader would draw it by hand.

**Regression channel** (fallback)
    Least-squares fit through recent closes, offset by max residuals on
    both sides. Always produces a channel even in ranging markets, but
    the edges may not touch actual pivots. Used when pivot detection
    fails (not enough swings or no clear trend).

Both return the same RegressionChannel dataclass so the API response
shape is identical.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from app.strategy.indicators import find_swings


@dataclass(slots=True)
class ChannelPoint:
    time: int          # unix seconds
    price: float


@dataclass(slots=True)
class ChannelLine:
    start: ChannelPoint
    end: ChannelPoint


@dataclass(slots=True)
class RegressionChannel:
    upper: ChannelLine
    midline: ChannelLine
    lower: ChannelLine

    slope_per_bar: float        # raw slope of the fit (price units / bar)
    slope_pct_total: float      # % change from start to end of midline
    stddev: float               # std deviation of residuals
    width_pct: float            # (upper - lower) at end, as % of midline
    lookback: int
    algorithm: str = "regression"  # "pivot" | "regression"


def compute_pivot_channel(
    df: pd.DataFrame,
    lookback: int = 100,
    left: int = 3,
    right: int = 3,
) -> RegressionChannel | None:
    """Draw a parallel channel through actual swing pivots.

    Rules:
    - Uptrend if the two most recent swing lows are ascending:
        - Lower channel line = trendline through those two higher lows.
        - Upper channel line = same slope, shifted to pass through the
          highest swing high in the lookback window.
    - Downtrend if the two most recent swing highs are descending:
        - Upper channel line = trendline through those two lower highs.
        - Lower channel line = same slope, shifted to pass through the
          lowest swing low.
    - Otherwise returns None so the caller can fall back to regression.

    Returns None if there are not enough swings.
    """
    if len(df) < 20:
        return None

    recent = df.tail(lookback).reset_index()  # keep original datetime column
    # After reset_index the datetime column is called by its original name
    # (e.g. "open_time" or "index"). Grab the first column name generically.
    time_col = recent.columns[0]
    swings = find_swings(recent.set_index(time_col), left=left, right=right)
    # find_swings uses positional indices; those match `recent`'s row order
    # because we reset_index() before calling it.

    highs = [s for s in swings if s.is_high]
    lows = [s for s in swings if not s.is_high]

    if len(highs) < 2 and len(lows) < 2:
        return None

    trend: str | None = None
    if len(lows) >= 2 and lows[-1].price > lows[-2].price:
        trend = "up"
    elif len(highs) >= 2 and highs[-1].price < highs[-2].price:
        trend = "down"

    if trend is None:
        return None

    n = len(recent)
    start_i = 0
    end_i = n - 1

    if trend == "up":
        p1, p2 = lows[-2], lows[-1]
        opposing = max(highs, key=lambda h: h.price) if highs else None
    else:
        p1, p2 = highs[-2], highs[-1]
        opposing = min(lows, key=lambda lo: lo.price) if lows else None

    if opposing is None or p2.index == p1.index:
        return None

    slope = (p2.price - p1.price) / (p2.index - p1.index)

    def _price_at(index: int, anchor_idx: int, anchor_price: float) -> float:
        return anchor_price + slope * (index - anchor_idx)

    trend_start = _price_at(start_i, p1.index, p1.price)
    trend_end = _price_at(end_i, p1.index, p1.price)
    parallel_start = _price_at(start_i, opposing.index, opposing.price)
    parallel_end = _price_at(end_i, opposing.index, opposing.price)

    start_time = int(pd.Timestamp(recent[time_col].iloc[start_i]).timestamp())
    end_time = int(pd.Timestamp(recent[time_col].iloc[end_i]).timestamp())

    if trend == "up":
        upper = ChannelLine(
            start=ChannelPoint(time=start_time, price=float(parallel_start)),
            end=ChannelPoint(time=end_time, price=float(parallel_end)),
        )
        lower = ChannelLine(
            start=ChannelPoint(time=start_time, price=float(trend_start)),
            end=ChannelPoint(time=end_time, price=float(trend_end)),
        )
    else:
        upper = ChannelLine(
            start=ChannelPoint(time=start_time, price=float(trend_start)),
            end=ChannelPoint(time=end_time, price=float(trend_end)),
        )
        lower = ChannelLine(
            start=ChannelPoint(time=start_time, price=float(parallel_start)),
            end=ChannelPoint(time=end_time, price=float(parallel_end)),
        )

    mid_start = (upper.start.price + lower.start.price) / 2.0
    mid_end = (upper.end.price + lower.end.price) / 2.0
    midline = ChannelLine(
        start=ChannelPoint(time=start_time, price=mid_start),
        end=ChannelPoint(time=end_time, price=mid_end),
    )

    slope_pct_total = ((mid_end - mid_start) / mid_start) * 100.0 if mid_start else 0.0
    width_pct = (
        (upper.end.price - lower.end.price) / midline.end.price * 100.0
        if midline.end.price
        else 0.0
    )

    return RegressionChannel(
        upper=upper,
        midline=midline,
        lower=lower,
        slope_per_bar=float(slope),
        slope_pct_total=float(slope_pct_total),
        stddev=0.0,
        width_pct=float(width_pct),
        lookback=n,
        algorithm="pivot",
    )


def compute_channel(
    df: pd.DataFrame,
    lookback: int = 100,
    prefer: str = "pivot",
) -> RegressionChannel | None:
    """Dispatch to the requested algorithm with automatic fallback.

    ``prefer='pivot'`` (default) tries the pivot algorithm first and falls
    back to regression if there are not enough swings. ``prefer='regression'``
    only runs regression.
    """
    if prefer == "pivot":
        pivot = compute_pivot_channel(df, lookback=lookback)
        if pivot is not None:
            return pivot
    return compute_regression_channel(df, lookback=lookback)


def compute_regression_channel(
    df: pd.DataFrame,
    lookback: int = 100,
    source: str = "close",
) -> RegressionChannel | None:
    """Fit a regression channel over the last ``lookback`` candles.

    :param df: Klines DataFrame with a datetime index.
    :param lookback: Number of most-recent candles to include.
    :param source: Column to regress ('close' is typical; 'high' / 'low'
        can be used to fit against wicks only).
    """
    if len(df) < 10:
        return None

    recent = df.tail(lookback)
    n = len(recent)
    if n < 10:
        return None

    x = np.arange(n, dtype=float)
    y = recent[source].to_numpy(dtype=float)

    # np.polyfit fits highest-degree first. deg=1 returns [slope, intercept].
    slope, intercept = np.polyfit(x, y, 1)
    fit = slope * x + intercept
    residuals = y - fit

    stddev = float(np.std(residuals))
    max_up = float(residuals.max())          # >= 0
    max_down = float(residuals.min())        # <= 0

    # Use the LARGER of the two extremes as the offset for both sides,
    # so the midline is exactly centered between upper and lower. This
    # matches user intuition ("garis tengah persis di tengah") — the
    # asymmetric variant was more academically correct but visually
    # confusing.
    max_dev = max(abs(max_up), abs(max_down))

    start_i = 0
    end_i = n - 1
    start_time = int(recent.index[start_i].timestamp())
    end_time = int(recent.index[end_i].timestamp())

    def _line(offset: float) -> ChannelLine:
        return ChannelLine(
            start=ChannelPoint(time=start_time, price=float(fit[start_i] + offset)),
            end=ChannelPoint(time=end_time, price=float(fit[end_i] + offset)),
        )

    upper = _line(+max_dev)
    midline = _line(0.0)
    lower = _line(-max_dev)

    slope_pct_total = float(((fit[-1] - fit[0]) / fit[0]) * 100.0) if fit[0] else 0.0
    width_pct = (
        float((upper.end.price - lower.end.price) / midline.end.price * 100.0)
        if midline.end.price
        else 0.0
    )

    return RegressionChannel(
        upper=upper,
        midline=midline,
        lower=lower,
        slope_per_bar=float(slope),
        slope_pct_total=slope_pct_total,
        stddev=stddev,
        width_pct=width_pct,
        lookback=n,
    )
