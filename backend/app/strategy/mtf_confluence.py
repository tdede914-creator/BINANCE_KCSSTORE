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
   - **ADX regime filter**: ADX >= ``adx_min`` (default 20). Below that,
     the market is ranging and trend signals fail. Set adx_min=0 to
     disable.
   - **Volume confirmation**: entry-candle volume >=
     ``volume_mult × MA(volume, 20)``. Default multiplier 1.2 rejects
     moves that lack participation. Set volume_mult=0 to disable.

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


# --------------------------------------------------------------------------
# Adaptive TP computation
# --------------------------------------------------------------------------


def _adaptive_tps(
    *,
    entry: float,
    risk: float,
    atr_val: float,
    side: str,
    zones: list["SRZone"],
    ctx: "StrategyContext",
) -> tuple[float, float, float, str]:
    """Compute TP1/TP2/TP3 with market-structure awareness.

    Strategy:
      1. Collect S/R zones "in the direction of the trade":
           LONG  → resistances above entry
           SHORT → supports    below entry
      2. Filter out zones that are too close (< 1×ATR) or too far
         (> 10×ATR). Sort by distance from entry.
      3. Enforce a per-tier minimum RR (0.8 / 1.5 / 2.5). A zone
         that violates the minimum is skipped for that tier only.
      4. Any tier that ends up without a valid zone falls back to
         the classic risk-multiple TP (rr_tp1 / rr_tp2 / rr_tp3
         from settings).

    Returns (tp1, tp2, tp3, note) where ``note`` is a short human
    string describing where each target came from — surfaced in
    signal diagnostics + Telegram reason line.
    """
    min_atr_distance = atr_val * 1.0
    max_atr_distance = atr_val * 10.0
    min_rr_by_tier = (0.8, 1.5, 2.5)

    if side == "LONG":
        candidates = [
            z.price for z in zones
            if z.kind == "resistance"
            and z.price > entry + min_atr_distance
            and z.price < entry + max_atr_distance
        ]
        candidates.sort()   # nearest first
        fallbacks = (
            entry + risk * ctx.rr_tp1,
            entry + risk * ctx.rr_tp2,
            entry + risk * ctx.rr_tp3,
        )
    else:  # SHORT
        candidates = [
            z.price for z in zones
            if z.kind == "support"
            and z.price < entry - min_atr_distance
            and z.price > entry - max_atr_distance
        ]
        candidates.sort(reverse=True)  # nearest first (highest support)
        fallbacks = (
            entry - risk * ctx.rr_tp1,
            entry - risk * ctx.rr_tp2,
            entry - risk * ctx.rr_tp3,
        )

    def _rr(target: float) -> float:
        return (target - entry) / risk if side == "LONG" else (entry - target) / risk

    tps: list[float] = []
    notes: list[str] = []
    for tier_idx, min_rr in enumerate(min_rr_by_tier):
        picked = None
        # Try each unused candidate. Must beat previous TP and hit
        # the tier's min RR.
        for c in candidates:
            if any(abs(c - t) < 1e-12 for t in tps):
                continue  # already used
            if tps and ((side == "LONG" and c <= tps[-1]) or (side == "SHORT" and c >= tps[-1])):
                continue  # must be further than previous TP
            if _rr(c) < min_rr:
                continue
            picked = c
            notes.append(f"TP{tier_idx + 1}=SR({_rr(c):.2f}R)")
            break
        if picked is None:
            picked = fallbacks[tier_idx]
            notes.append(f"TP{tier_idx + 1}=RR{min_rr_by_tier[tier_idx]:.1f}+")
        tps.append(picked)

    return tps[0], tps[1], tps[2], "  ".join(notes)


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
        trigger_ok, entry_price, sl, tp1, tp2, tp3, trig_diag = self._compute_trigger(
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
            take_profit_3=tp3,
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
            adx_period=self.ctx.adx_period,
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
            adx_period=self.ctx.adx_period,
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
    ) -> tuple[bool, float, float, float, float, float, dict]:
        enriched = enrich(
            df,
            ema_fast=self.ctx.ema_fast,
            ema_slow=self.ctx.ema_slow,
            ema_trigger=self.ctx.ema_trigger,
            rsi_period=self.ctx.rsi_period,
            atr_period=self.ctx.atr_period,
            adx_period=self.ctx.adx_period,
        )
        last = enriched.iloc[-1]
        prev = enriched.iloc[-2]

        close = float(last["close"])
        atr_val = float(last["atr"])
        rsi_val = float(last["rsi"])
        adx_val = float(last["adx"]) if not pd.isna(last["adx"]) else 0.0
        vol = float(last["volume"])
        vol_ma = float(last["vol_ma"])
        ema_trig = float(last["ema_trigger"])

        diag: dict = {
            "close": close,
            "rsi": rsi_val,
            "atr": atr_val,
            "adx": adx_val,
            "volume": vol,
            "vol_ma": vol_ma,
            "ema_trigger": ema_trig,
        }

        if np.isnan(atr_val) or atr_val <= 0:
            diag["reason"] = "atr invalid"
            return False, 0, 0, 0, 0, 0, diag

        swings = find_swings(df, left=2, right=2)
        prev_swing_high = last_swing_high(swings, before_index=len(df) - 1)
        prev_swing_low = last_swing_low(swings, before_index=len(df) - 1)

        # ------------ RSI filter ------------
        if bias == Bias.LONG and rsi_val > self.ctx.rsi_long_max:
            diag["reason"] = f"RSI too high ({rsi_val:.1f})"
            return False, 0, 0, 0, 0, 0, diag
        if bias == Bias.SHORT and rsi_val < self.ctx.rsi_short_min:
            diag["reason"] = f"RSI too low ({rsi_val:.1f})"
            return False, 0, 0, 0, 0, 0, diag

        # ------------ ADX regime filter ------------
        # ADX measures trend strength. Below the minimum threshold the
        # market is ranging and trend-following signals fail badly
        # (choppy MEAN 40-50% of losing trades in our earlier backtests).
        # A zero threshold effectively disables the check so we can
        # A/B test with and without it.
        if self.ctx.adx_min > 0 and adx_val < self.ctx.adx_min:
            diag["reason"] = f"ADX too low ({adx_val:.1f} < {self.ctx.adx_min:.1f}) — sideways market"
            return False, 0, 0, 0, 0, 0, diag

        # ------------ Volume confirmation ------------
        # Require the entry candle's volume to exceed a multiple of its
        # 20-bar average. A move without volume is usually a fake.
        vol_ok = True
        if self.ctx.volume_mult > 0 and vol_ma > 0:
            vol_ok = vol >= vol_ma * self.ctx.volume_mult

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

        # Cast numpy bools to native Python bool. pandas comparisons return
        # numpy.bool_, which SQLAlchemy's JSON encoder rejects with:
        #   TypeError: Object of type bool_ is not JSON serializable
        # This bug was silently killing every FIRED signal at INSERT time.
        diag.update(
            {
                "bos": bool(bos_ok),
                "retest": bool(retest_ok),
                "volume_ok": bool(vol_ok),
            }
        )

        if not (bos_ok or retest_ok):
            diag["reason"] = "no BOS or retest"
            return False, 0, 0, 0, 0, 0, diag

        if not vol_ok:
            diag["reason"] = "volume too low"
            return False, 0, 0, 0, 0, 0, diag

        # ------------ Compute SL / TP -------------
        entry = close
        # --------------------------------------------------------------
        # Adaptive TP model.
        #
        # SL is anchored to the last opposite-side swing +/- an ATR
        # buffer (unchanged). TPs used to be a fixed RR multiple of
        # that risk, which routinely placed the target through a real
        # resistance / support level the market was going to react to.
        #
        # New rule:
        #   1. Find zones from the same S/R detector that draws S1..Sn
        #      / R1..Rn on the chart. Filter to the ones "in the
        #      direction of the trade" (resistance above entry for
        #      LONG, support below entry for SHORT).
        #   2. Sort by distance and pick the first 3 as TP1/TP2/TP3.
        #   3. Reject a zone if it's too close (< 1×ATR) or too far
        #      (> 10×ATR). Fall back to the classic risk-multiple TP
        #      for whichever tier is missing.
        #   4. Enforce a minimum RR (0.8 for TP1, 1.5 for TP2, 2.5 for
        #      TP3) so a nearby zone doesn't produce a laughably tight
        #      target.
        # --------------------------------------------------------------
        # Get SR zones from a slightly wider swing net for better coverage.
        zones_wide = sr_zones(df, find_swings(df, left=3, right=3))

        if bias == Bias.LONG:
            base_sl = prev_swing_low.price if prev_swing_low else float(df["low"].tail(10).min())
            sl = base_sl - self.ctx.atr_sl_mult * atr_val
            risk = entry - sl
            if risk <= 0:
                diag["reason"] = "computed SL >= entry"
                return False, 0, 0, 0, 0, 0, diag
            tp1, tp2, tp3, tp_note = _adaptive_tps(
                entry=entry,
                risk=risk,
                atr_val=atr_val,
                side="LONG",
                zones=zones_wide,
                ctx=self.ctx,
            )
        else:
            base_sl = prev_swing_high.price if prev_swing_high else float(df["high"].tail(10).max())
            sl = base_sl + self.ctx.atr_sl_mult * atr_val
            risk = sl - entry
            if risk <= 0:
                diag["reason"] = "computed SL <= entry"
                return False, 0, 0, 0, 0, 0, diag
            tp1, tp2, tp3, tp_note = _adaptive_tps(
                entry=entry,
                risk=risk,
                atr_val=atr_val,
                side="SHORT",
                zones=zones_wide,
                ctx=self.ctx,
            )

        diag["reason"] = (
            f"{'BOS' if bos_ok else 'Retest'}"
            f"{' + Retest' if bos_ok and retest_ok else ''}"
            f"; TPs: {tp_note}"
        )
        return True, entry, sl, tp1, tp2, tp3, diag

    # ------------------------------------------------------------------
    # Confidence score (heuristic 0..1)
    # ------------------------------------------------------------------

    def _score_confidence(self, bias_d: dict, setup_d: dict, trig_d: dict) -> float:
        score = 0.35  # baseline for passing all three gates

        sep = bias_d.get("separation_pct", 0.0) or 0.0
        score += min(sep / 3.0, 0.15)  # up to +0.15 for strong EMA separation

        if setup_d.get("nearest_zone") and setup_d.get("nearest_ob"):
            score += 0.12
        elif setup_d.get("nearest_zone") or setup_d.get("nearest_ob"):
            score += 0.06

        if trig_d.get("bos") and trig_d.get("retest"):
            score += 0.12
        elif trig_d.get("bos") or trig_d.get("retest"):
            score += 0.06

        # ADX contribution — strong trend increases confidence
        adx_v = trig_d.get("adx", 0.0) or 0.0
        if adx_v >= 40:
            score += 0.15
        elif adx_v >= 25:
            score += 0.10
        elif adx_v >= 20:
            score += 0.05

        # Volume spike contribution — outsized volume = conviction
        vol = trig_d.get("volume", 0.0) or 0.0
        vol_ma = trig_d.get("vol_ma", 0.0) or 0.0
        if vol_ma > 0:
            spike_ratio = vol / vol_ma
            if spike_ratio >= 2.0:
                score += 0.10
            elif spike_ratio >= 1.5:
                score += 0.06
            elif spike_ratio >= 1.2:
                score += 0.03

        return round(min(max(score, 0.0), 1.0), 3)
