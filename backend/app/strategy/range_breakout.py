"""Range Breakout strategy — post-consolidation directional break.

Complements :class:`MTFConfluenceStrategy` (which needs a clear trend).
Range Breakout fires when the market has been *sideways* for a while
and then breaks the box, in either direction:

1. **Consolidation detection** — the last ``rb_lookback`` bars on the
   entry TF form a tight box (range height ≤ ``rb_max_range_pct`` of
   current price).
2. **Volatility squeeze** — current ATR is meaningfully lower than the
   recent 50-bar average (``atr / atr_ma ≤ rb_atr_squeeze_ratio``).
   A squeeze means the range is compressed and priming for expansion.
3. **Breakout candle** — the current candle CLOSES above the range top
   (LONG) or below the range bottom (SHORT), by at least
   ``rb_breakout_buffer × ATR`` to avoid wick fills.
4. **Volume confirmation** — same as MTF confluence: entry candle
   volume ≥ ``volume_mult × MA(volume, 20)``.

Structural difference vs MTF Confluence:
- MTF cares about EMA alignment across timeframes ("is the trend up?").
- Range Breakout doesn't; it fires in EITHER direction and lets the
  breakout itself define the direction. This is what most crypto
  Telegram "Andi Hakim"-style signals actually do — they draw a box,
  wait for a break, then post entry/SL/TP.

SL / TP model
-------------
- **SL**: opposite side of the range, offset by the breakout buffer.
- **TP1**: measured move — height of the range projected. This is a
  classic technical target: if a $10 box breaks up at $100, TP1 = $110.
- **TP2**: 1.5× the range height, for the runner.
- SL distance is naturally small (roughly range_height + buffer), so
  R:R on this strategy is typically favourable when the breakout is
  real (avg 2-3R at TP2).

Confidence score components
---------------------------
- baseline 0.40 for passing all gates
- +up to 0.20 for the strength of the squeeze (tighter = higher)
- +up to 0.20 for how far the breakout candle closed beyond the level
- +up to 0.20 for volume spike magnitude
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.core.logging import get_logger
from app.strategy.indicators import enrich
from app.strategy.types import Side, SignalProposal, StrategyContext

log = get_logger(__name__)


class RangeBreakoutStrategy:
    """Post-consolidation breakout signal engine."""

    STRATEGY_NAME = "range_breakout"

    def __init__(self, ctx: StrategyContext | None = None) -> None:
        self.ctx = ctx or StrategyContext()

    # ------------------------------------------------------------------
    # Public API — mirror MTFConfluenceStrategy.evaluate() so the
    # scanner can call either one interchangeably.
    # ------------------------------------------------------------------
    def evaluate(
        self,
        symbol: str,
        bias_df: pd.DataFrame,
        setup_df: pd.DataFrame,
        entry_df: pd.DataFrame,
        *,
        bias_tf: str,
        setup_tf: str,
        entry_tf: str,
    ) -> tuple[SignalProposal | None, dict]:
        diag: dict = {"stage": "range_breakout"}

        lookback = self.ctx.rb_lookback
        if len(entry_df) < lookback + 55:  # 50 for atr_ma + 5 buffer
            diag["reason"] = f"not enough entry bars ({len(entry_df)} < {lookback + 55})"
            return None, diag

        # Enrich once (fast-path skips recompute if already enriched).
        df = enrich(
            entry_df,
            ema_fast=self.ctx.ema_fast,
            ema_slow=self.ctx.ema_slow,
            ema_trigger=self.ctx.ema_trigger,
            rsi_period=self.ctx.rsi_period,
            atr_period=self.ctx.atr_period,
            adx_period=self.ctx.adx_period,
        )

        # 1. --- Define the range from the last N bars, excluding current.
        # We exclude the current bar because the breakout candle would
        # otherwise inflate the "range top/bottom" and make it impossible
        # to detect its own break.
        window = df.iloc[-lookback - 1 : -1]
        current = df.iloc[-1]
        prev = df.iloc[-2]

        range_top = float(window["high"].max())
        range_bot = float(window["low"].min())
        range_height = range_top - range_bot
        close = float(current["close"])
        prev_close = float(prev["close"])

        if close <= 0 or range_height <= 0:
            diag["reason"] = "invalid range/close"
            return None, diag

        range_pct = 100.0 * range_height / close
        diag.update(
            {
                "range_top": range_top,
                "range_bot": range_bot,
                "range_height": range_height,
                "range_pct": range_pct,
                "close": close,
            }
        )

        # 2. --- Range must be tight enough.
        if range_pct > self.ctx.rb_max_range_pct:
            diag["reason"] = (
                f"range too wide ({range_pct:.2f}% > "
                f"{self.ctx.rb_max_range_pct:.2f}%)"
            )
            return None, diag

        # 3. --- Volatility squeeze — current ATR must be well below its
        # recent mean.
        atr_now = float(current["atr"])
        if np.isnan(atr_now) or atr_now <= 0:
            diag["reason"] = "atr invalid"
            return None, diag

        atr_ma = float(df["atr"].tail(50).mean())
        if atr_ma <= 0 or np.isnan(atr_ma):
            diag["reason"] = "atr_ma invalid"
            return None, diag

        squeeze_ratio = atr_now / atr_ma
        diag["atr_now"] = atr_now
        diag["atr_ma"] = atr_ma
        diag["squeeze_ratio"] = squeeze_ratio

        if squeeze_ratio > self.ctx.rb_atr_squeeze_ratio:
            diag["reason"] = (
                f"no squeeze (atr_now/atr_ma = {squeeze_ratio:.2f} > "
                f"{self.ctx.rb_atr_squeeze_ratio:.2f})"
            )
            return None, diag

        # 4. --- Breakout check.
        buffer = self.ctx.rb_breakout_buffer * atr_now

        long_breakout = close > range_top + buffer and prev_close <= range_top + buffer
        short_breakout = close < range_bot - buffer and prev_close >= range_bot - buffer

        diag["long_breakout"] = long_breakout
        diag["short_breakout"] = short_breakout

        if not (long_breakout or short_breakout):
            diag["reason"] = (
                f"no breakout (close {close:.6f}, top {range_top:.6f}, "
                f"bot {range_bot:.6f})"
            )
            return None, diag

        # 5. --- Volume confirmation (same rule as MTF Confluence).
        vol = float(current["volume"])
        vol_ma = float(current["vol_ma"])
        vol_ok = True
        if self.ctx.volume_mult > 0 and vol_ma > 0:
            vol_ok = vol >= vol_ma * self.ctx.volume_mult

        diag["volume"] = vol
        diag["vol_ma"] = vol_ma
        diag["volume_ok"] = vol_ok

        if not vol_ok:
            diag["reason"] = (
                f"volume too low ({vol:.0f} < {vol_ma:.0f} × "
                f"{self.ctx.volume_mult:.2f})"
            )
            return None, diag

        # 6. --- Build the signal.
        # Range Breakout uses measured moves for TP1/TP2, and 2× TP2 as a
        # display-only TP3 runner so the Telegram signal card matches the
        # 3-tier target style users are used to from Telegram signal channels.
        if long_breakout:
            side = Side.LONG
            entry = close
            sl = range_bot - buffer
            risk = entry - sl
            tp1 = entry + range_height * self.ctx.rb_measured_move_tp1
            tp2 = entry + range_height * self.ctx.rb_measured_move_tp2
            tp3 = entry + range_height * self.ctx.rb_measured_move_tp2 * 1.5
        else:
            side = Side.SHORT
            entry = close
            sl = range_top + buffer
            risk = sl - entry
            tp1 = entry - range_height * self.ctx.rb_measured_move_tp1
            tp2 = entry - range_height * self.ctx.rb_measured_move_tp2
            tp3 = entry - range_height * self.ctx.rb_measured_move_tp2 * 1.5

        if risk <= 0:
            diag["reason"] = "computed risk <= 0"
            return None, diag

        rr_tp1 = (tp1 - entry) / risk if side == Side.LONG else (entry - tp1) / risk

        # 7. --- Confidence score.
        # Baseline for passing all gates + magnitude bonuses.
        conf = 0.40
        # Tighter squeeze → higher conviction.
        conf += 0.20 * (1.0 - min(squeeze_ratio / self.ctx.rb_atr_squeeze_ratio, 1.0))
        # How far the break went (in ATR multiples).
        break_dist = (close - range_top) / atr_now if long_breakout else (range_bot - close) / atr_now
        conf += 0.20 * min(break_dist, 1.0)
        # Volume spike magnitude.
        spike = (vol / vol_ma) if vol_ma > 0 else 1.0
        if spike >= 2.0:
            conf += 0.20
        elif spike >= 1.5:
            conf += 0.12
        elif spike >= 1.2:
            conf += 0.06
        conf = round(min(max(conf, 0.0), 1.0), 3)

        proposal = SignalProposal(
            symbol=symbol,
            side=side,
            entry_price=entry,
            stop_loss=sl,
            take_profit_1=tp1,
            take_profit_2=tp2,
            take_profit_3=tp3,
            bias_tf=bias_tf,
            setup_tf=setup_tf,
            entry_tf=entry_tf,
            confidence=conf,
            reason=(
                f"Range breakout {side.value}: {lookback}-bar box "
                f"[{range_bot:.6f} — {range_top:.6f}] height {range_pct:.2f}%, "
                f"squeeze {squeeze_ratio:.2f}, RR@TP1 {rr_tp1:.2f}"
            ),
            diagnostics=dict(diag),
            strategy=self.STRATEGY_NAME,
        )
        diag["reason"] = "signal fired"
        diag["side"] = side.value
        diag["rr_tp1"] = rr_tp1
        log.info(
            "range_breakout.fire",
            symbol=symbol,
            side=side.value,
            entry=entry,
            sl=sl,
            tp1=tp1,
            tp2=tp2,
            range_pct=range_pct,
            squeeze_ratio=squeeze_ratio,
            confidence=conf,
        )
        return proposal, diag
