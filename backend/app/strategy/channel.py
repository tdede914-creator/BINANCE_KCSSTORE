"""Regression-based parallel channel detection.

Algorithm
---------
Given a klines DataFrame, we fit a linear regression ``y = m*x + b``
through recent closes and derive three parallel lines:

- **Midline**  = the regression itself.
- **Upper**    = midline shifted up by ``max_positive_residual``.
- **Lower**    = midline shifted down by ``max_negative_residual``.

Because the shifts equal the biggest deviations, the upper and lower
lines are guaranteed to *touch* the most extreme candle wicks in the
lookback window — matching what a human trader draws when they connect
two swing highs / two swing lows.

The result is a "tight" parallel channel (bands touch the extremes).
For a "loose" statistical channel, use N × stddev instead of max
residuals; both are returned so the caller can pick.

Returns None when there are fewer than 10 candles.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


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

    start_i = 0
    end_i = n - 1
    start_time = int(recent.index[start_i].timestamp())
    end_time = int(recent.index[end_i].timestamp())

    def _line(offset: float) -> ChannelLine:
        return ChannelLine(
            start=ChannelPoint(time=start_time, price=float(fit[start_i] + offset)),
            end=ChannelPoint(time=end_time, price=float(fit[end_i] + offset)),
        )

    upper = _line(max_up)
    midline = _line(0.0)
    lower = _line(max_down)

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
