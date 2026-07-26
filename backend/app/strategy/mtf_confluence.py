"""Multi-Timeframe Confluence signal engine.

Logic
-----
1. **Bias (HTF)** — EMA(fast) vs EMA(slow) on the higher timeframe.
   - fast > slow  → LONG bias
   - fast < slow  → SHORT bias
   - within noise → NEUTRAL (skip)

2. **Setup (MTF)** — the mid timeframe must show that price is near a
   valid S/R zone or order block *in the direction of the bias*.

3. **Trigger (LTF, configurable)** — the entry timeframe must produce a
   confirmation candle:
   - Break of Structure (BOS) in the bias direction, or
   - Retest of EMA(trigger) with a bullish/bearish reaction candle
   - RSI is not at an extreme
   - Volume of trigger candle > MA(volume)

4. **Suggested SL** — last swing (opposite direction) ± ``atr_sl_mult × ATR``.
5. **Suggested TP** — RR ratios based on that SL.
"""
from __future__ import annotations

from dataclasses import asdict

import numpy as np
import pandas as pd

from app.core.logging import get_logger
from app.strategy.indicators import (
    OrderBlock,
    SRZone,
    enrich,
    find_order_blocks,
    find_swings,
    last_swing_high,
    last_swing_low,
    sr_zones,
)
from app.strategy.types import Bias, Side, SignalProposal, StrategyContext

log = get_logger(__name__)


class MTFConfluenceStrategy:
    """Stateless, receives DataFrames + context, returns a SignalProposal or None."""

    def __init__(self, ctx: StrategyContext | None = None) -> None:
        self.ctx = ctx or StrategyContext()

    # ------------------------------------------------------------------
    # Public API
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
        """Run all three checks; return ``(proposal_or_None, diagnostics)``.

        The diagnostics dict is always populated so the scanner can surface
        WHY a signal was skipped even when the return value is None. The
        top-level ``stage`` key tells you the last gate reached, and
        ``reason`` gives a short human-readable explanation.
        """
        diag: dict = {"symbol": symbol, "stage": "warmup"}

        if len(bias_df) < self.ctx.ema_slow + 5:
            diag["reason"] = f"not enough bias data ({len(bias_df)} bars)"
            return None, diag
        if len(setup_df) < 60:
            diag["reason"] = f"not enough setup data ({len(setup_df)} bars)"
            return None, diag
        if len(entry_df) < 60:
            diag["reason"] = f"not enough entry data ({len(entry_df)} bars)"
            return None, diag

        # ---- 1. Bias -----------------------------------------------------
        diag["stage"] = "bias"
        bias, bias_diag = self._compute_bias(bias_df)
        diag["bias"] = bias_diag
        if bias == Bias.NEUTRAL:
            diag["reason"] = bias_diag.get("reason", "neutral bias")
            return None, diag
        diag["bias_side"] = bias.value

        # ---- 2. Setup ----------------------------------------------------
        diag["stage"] = "setup"
        setup_ok, setup_diag = self._compute_setup(setup_df, bias)
        diag["setup"] = setup_diag
        if not setup_ok:
            diag["reason"] = setup_diag.get("reason", "no setup zone")
            return None, diag

        # ---- 3. Trigger --------------------------------------------------
        diag["stage"] = "trigger"
        trigger_ok, entry_price, sl, tp1, tp2, trig_diag = self._compute_trigger(
            entry_df, bias
        )
        diag["trigger"] = trig_diag
        if not trigger_ok:
            diag["reason"] = trig_diag.get("reason", "no trigger")
            return None, diag

        # ---- Confidence score --------------------------------------------
        diag["stage"] = "fired"
        diag["reason"] = "all gates passed"
        confidence = self._score_confidence(bias_diag, setup_diag, trig_diag)

        reason_parts = [
            f"Bias {bias.value} on {bias_tf}",
            f"Setup: {setup_diag.get('reason', 'ok')}",
            f"Trigger: {trig_diag.get('reason', 'ok')} on {entry_tf}",
        ]

        proposal = SignalProposal(
            symbol=symbol,
            side=Side.LONG if bias == Bias.LONG else Side.SHORT,
            entry_price=entry_price,
            stop_loss=sl,
            take_profit_1=tp1,
            take_profit_2=tp2,
            bias_tf=bias_tf,
            setup_tf=setup_tf,
            entry_tf=entry_tf,
            confidence=confidence,
            reason=" | ".join(reason_parts),
            diagnostics={
                "bias": bias_diag,
                "setup": setup_diag,
                "trigger": trig_diag,
                "ctx": asdict(self.ctx),
            },
        )
        return proposal, diag

    # ------------------------------------------------------------------
    # 1. Bias
    # ------------------------------------------------------------------

    def _compute_bias(self, df: pd.DataFrame) -> tuple[Bias, dict]:
        enriched = enrich(
            df,
            ema_fast=self.ctx.ema_fast,
            ema_slow=self.ctx.ema_slow,
            ema_trigger=self.ctx.ema_trigger,
            rsi_period=self.ctx.rsi_period,
            atr_period=self.ctx.atr_period,
        )
        last = enriched.iloc[-1]
        ema_f = last["ema_fast"]
        ema_s = last["ema_slow"]
        close = last["close"]

        if pd.isna(ema_f) or pd.isna(ema_s):
            return Bias.NEUTRAL, {"reason": "not enough data"}

        # Require some separation between EMAs to filter chop.
        sep_pct = abs(ema_f - ema_s) / max(ema_s, 1e-9)
        if sep_pct < 0.0015:  # 0.15% separation minimum
            return Bias.NEUTRAL, {
                "reason": f"EMAs too close ({sep_pct*100:.3f}%)",
                "ema_fast": float(ema_f),
                "ema_slow": float(ema_s),
            }

        diag = {
            "ema_fast": float(ema_f),
            "ema_slow": float(ema_s),
            "close": float(close),
            "separation_pct": float(sep_pct * 100),
        }

        if ema_f > ema_s and close > ema_s:
            return Bias.LONG, diag
        if ema_f < ema_s and close < ema_s:
            return Bias.SHORT, diag
        return Bias.NEUTRAL, diag

    # ------------------------------------------------------------------
    # 2. Setup zone
    # ------------------------------------------------------------------

    def _compute_setup(self, df: pd.DataFrame, bias: Bias) -> tuple[bool, dict]:
        enriched = enrich(
            df,
            ema_fast=self.ctx.ema_fast,
            ema_slow=self.ctx.ema_slow,
            ema_trigger=self.ctx.ema_trigger,
            rsi_period=self.ctx.rsi_period,
            atr_period=self.ctx.atr_period,
        )
        last = enriched.iloc[-1]
        atr_val = last["atr"]
        close = last["close"]

        if pd.isna(atr_val) or atr_val <= 0:
            return False, {"reason": "atr invalid"}

        swings = find_swings(df, left=3, right=3)
        zones = sr_zones(df, swings)
        obs = find_order_blocks(df, lookback=min(len(df) - 1, 100))

        max_dist = self.ctx.setup_max_atr_distance * atr_val

        nearest_zone: SRZone | None = None
        nearest_ob: OrderBlock | None = None

        if bias == Bias.LONG:
            # Look for support below or an OB below (bullish OB)
            support_zones = [z for z in zones if z.kind == "support" and z.price <= close]
            if support_zones:
                nearest_zone = min(support_zones, key=lambda z: abs(close - z.price))
            bull_obs = [ob for ob in obs if ob.kind == "bull" and ob.high <= close]
            if bull_obs:
                nearest_ob = min(bull_obs, key=lambda ob: abs(close - (ob.high + ob.low) / 2))
        else:  # SHORT
            resist_zones = [z for z in zones if z.kind == "resistance" and z.price >= close]
            if resist_zones:
                nearest_zone = min(resist_zones, key=lambda z: abs(close - z.price))
            bear_obs = [ob for ob in obs if ob.kind == "bear" and ob.low >= close]
            if bear_obs:
                nearest_ob = min(bear_obs, key=lambda ob: abs(close - (ob.high + ob.low) / 2))

        ok_zone = nearest_zone is not None and abs(close - nearest_zone.price) <= max_dist
        ok_ob = nearest_ob is not None and (
            (bias == Bias.LONG and (close - nearest_ob.high) <= max_dist)
            or (bias == Bias.SHORT and (nearest_ob.low - close) <= max_dist)
        )

        diag = {
            "close": float(close),
            "atr": float(atr_val),
            "max_distance": float(max_dist),
            "nearest_zone": (
                {"price": nearest_zone.price, "touches": nearest_zone.touches}
                if nearest_zone
                else None
            ),
            "nearest_ob": (
                {"high": nearest_ob.high, "low": nearest_ob.low, "kind": nearest_ob.kind}
                if nearest_ob
                else None
            ),
        }

        if ok_zone or ok_ob:
            diag["reason"] = (
                f"near {'S/R zone' if ok_zone else ''}"
                f"{' + ' if ok_zone and ok_ob else ''}"
                f"{'order block' if ok_ob else ''}"
            )
            return True, diag

        diag["reason"] = "no setup zone within range"
        return False, diag

    # ------------------------------------------------------------------
    # 3. Trigger
    # ------------------------------------------------------------------

    def _compute_trigger(
        self,
        df: pd.DataFrame,
        bias: Bias,
    ) -> tuple[bool, float, float, float, float, dict]:
        enriched = enrich(
            df,
            ema_fast=self.ctx.ema_fast,
            ema_slow=self.ctx.ema_slow,
            ema_trigger=self.ctx.ema_trigger,
            rsi_period=self.ctx.rsi_period,
            atr_period=self.ctx.atr_period,
        )
        last = enriched.iloc[-1]
        prev = enriched.iloc[-2]

        close = float(last["close"])
        atr_val = float(last["atr"])
        rsi_val = float(last["rsi"])
        vol = float(last["volume"])
        vol_ma = float(last["vol_ma"])
        ema_trig = float(last["ema_trigger"])

        diag: dict = {
            "close": close,
            "rsi": rsi_val,
            "atr": atr_val,
            "volume": vol,
            "vol_ma": vol_ma,
            "ema_trigger": ema_trig,
        }

        if np.isnan(atr_val) or atr_val <= 0:
            diag["reason"] = "atr invalid"
            return False, 0, 0, 0, 0, diag

        swings = find_swings(df, left=2, right=2)
        prev_swing_high = last_swing_high(swings, before_index=len(df) - 1)
        prev_swing_low = last_swing_low(swings, before_index=len(df) - 1)

        # ------------ RSI filter ------------
        if bias == Bias.LONG and rsi_val > self.ctx.rsi_long_max:
            diag["reason"] = f"RSI too high ({rsi_val:.1f})"
            return False, 0, 0, 0, 0, diag
        if bias == Bias.SHORT and rsi_val < self.ctx.rsi_short_min:
            diag["reason"] = f"RSI too low ({rsi_val:.1f})"
            return False, 0, 0, 0, 0, diag

        # ------------ Volume filter (light) ------------
        vol_ok = vol >= vol_ma * 0.8  # not far below MA

        # ------------ Trigger detection ------------
        bos_ok = False
        retest_ok = False

        if bias == Bias.LONG:
            if prev_swing_high is not None:
                bos_ok = last["close"] > prev_swing_high.price and prev["close"] <= prev_swing_high.price
            # bullish reaction from EMA(20): prev candle red touching ema, curr candle green closing above
            touched_ema = prev["low"] <= ema_trig <= prev["high"] or last["low"] <= ema_trig <= last["high"]
            bullish_react = last["close"] > last["open"] and touched_ema
            retest_ok = bullish_react and last["close"] > ema_trig
        else:  # SHORT
            if prev_swing_low is not None:
                bos_ok = last["close"] < prev_swing_low.price and prev["close"] >= prev_swing_low.price
            touched_ema = prev["low"] <= ema_trig <= prev["high"] or last["low"] <= ema_trig <= last["high"]
            bearish_react = last["close"] < last["open"] and touched_ema
            retest_ok = bearish_react and last["close"] < ema_trig

        diag.update({"bos": bos_ok, "retest": retest_ok, "volume_ok": vol_ok})

        if not (bos_ok or retest_ok):
            diag["reason"] = "no BOS or retest"
            return False, 0, 0, 0, 0, diag

        if not vol_ok:
            diag["reason"] = "volume too low"
            return False, 0, 0, 0, 0, diag

        # ------------ Compute SL / TP -------------
        entry = close
        if bias == Bias.LONG:
            base_sl = prev_swing_low.price if prev_swing_low else float(df["low"].tail(10).min())
            sl = base_sl - self.ctx.atr_sl_mult * atr_val
            risk = entry - sl
            if risk <= 0:
                diag["reason"] = "computed SL >= entry"
                return False, 0, 0, 0, 0, diag
            tp1 = entry + risk * self.ctx.rr_tp1
            tp2 = entry + risk * self.ctx.rr_tp2
        else:
            base_sl = prev_swing_high.price if prev_swing_high else float(df["high"].tail(10).max())
            sl = base_sl + self.ctx.atr_sl_mult * atr_val
            risk = sl - entry
            if risk <= 0:
                diag["reason"] = "computed SL <= entry"
                return False, 0, 0, 0, 0, diag
            tp1 = entry - risk * self.ctx.rr_tp1
            tp2 = entry - risk * self.ctx.rr_tp2

        diag["reason"] = (
            f"{'BOS' if bos_ok else 'Retest'}"
            f"{' + Retest' if bos_ok and retest_ok else ''}"
            f"; RR set to {self.ctx.rr_tp1}/{self.ctx.rr_tp2}"
        )
        return True, entry, sl, tp1, tp2, diag

    # ------------------------------------------------------------------
    # Confidence score (heuristic 0..1)
    # ------------------------------------------------------------------

    def _score_confidence(self, bias_d: dict, setup_d: dict, trig_d: dict) -> float:
        score = 0.4  # baseline for passing all three gates

        sep = bias_d.get("separation_pct", 0.0) or 0.0
        score += min(sep / 3.0, 0.2)  # up to +0.2 for strong EMA separation

        if setup_d.get("nearest_zone") and setup_d.get("nearest_ob"):
            score += 0.15
        elif setup_d.get("nearest_zone") or setup_d.get("nearest_ob"):
            score += 0.08

        if trig_d.get("bos") and trig_d.get("retest"):
            score += 0.15
        elif trig_d.get("bos") or trig_d.get("retest"):
            score += 0.08

        if trig_d.get("volume_ok"):
            score += 0.05

        return round(min(max(score, 0.0), 1.0), 3)
