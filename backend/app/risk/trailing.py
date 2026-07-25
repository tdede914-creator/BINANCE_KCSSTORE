"""Trailing stop logic.

Design
------
- **Activation gate**: trailing is disabled until price has moved
  ``activation_rr × initial_risk`` in favor. So with ``activation_rr = 0``
  it trails from entry, with ``1.0`` it trails once price hits TP1, etc.
- **ATR mode**: SL = ``extreme - N × ATR_snapshot`` for LONG (mirror for SHORT).
  We use the ATR that was captured at signal time (not live ATR) to keep
  the trail consistent with the original risk plan.
- **PERCENT mode**: SL = ``extreme × (1 - pct/100)`` for LONG.
- **Monotonicity**: SL only moves in the favorable direction. Never widens.

The module is pure — no side effects, no DB, no I/O. It takes a
:class:`TrailingState` snapshot and a current price and returns an
updated snapshot. The caller decides how to persist / execute the change.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.db.models import TrailingMode
from app.strategy.types import Side


@dataclass(slots=True)
class TrailingConfig:
    """User-tunable trailing parameters passed into the executor at open time."""

    mode: TrailingMode = TrailingMode.OFF
    activation_rr: float = 1.0
    atr_mult: float = 1.5
    percent: float = 1.0
    atr_snapshot: float = 0.0  # ATR value captured at signal time (entry TF)

    @classmethod
    def from_user_config(cls, cfg, atr_snapshot: float = 0.0) -> "TrailingConfig":  # noqa: ANN001
        return cls(
            mode=(
                cfg.trailing_mode
                if isinstance(cfg.trailing_mode, TrailingMode)
                else TrailingMode(cfg.trailing_mode)
            ),
            activation_rr=cfg.trailing_activation_rr,
            atr_mult=cfg.trailing_atr_mult,
            percent=cfg.trailing_percent,
            atr_snapshot=atr_snapshot,
        )


@dataclass(slots=True)
class TrailingState:
    """Everything the trailing manager needs to know about a trade."""

    side: Side
    entry_price: float
    initial_sl: float                        # SL that was set at trade open
    current_sl: float                        # SL right now (may have moved)
    mode: TrailingMode
    activation_rr: float
    atr_mult: float
    atr_snapshot: float
    percent: float
    active: bool                             # activation threshold crossed?
    highest_price: float | None = None       # for LONG
    lowest_price: float | None = None        # for SHORT


@dataclass(slots=True)
class TrailingUpdate:
    """Result of a single ``update()`` call."""

    state: TrailingState
    sl_changed: bool = False
    newly_activated: bool = False
    new_sl: float | None = None
    old_sl: float | None = None
    reason: str = ""


class TrailingStopManager:
    """Stateless computation. All state is passed in per call."""

    @staticmethod
    def update(state: TrailingState, current_price: float) -> TrailingUpdate:
        """Advance the trailing state one tick given the latest price."""
        if state.mode == TrailingMode.OFF or current_price <= 0:
            return TrailingUpdate(state=state)

        # --- 1. Track extreme ---
        new_state = _copy(state)
        if state.side == Side.LONG:
            new_state.highest_price = (
                current_price
                if state.highest_price is None
                else max(state.highest_price, current_price)
            )
            extreme = new_state.highest_price
        else:
            new_state.lowest_price = (
                current_price
                if state.lowest_price is None
                else min(state.lowest_price, current_price)
            )
            extreme = new_state.lowest_price

        # --- 2. Activation gate ---
        activated_now = False
        if not new_state.active:
            initial_risk = abs(state.entry_price - state.initial_sl)
            if initial_risk <= 0:
                return TrailingUpdate(state=new_state)

            required_move = state.activation_rr * initial_risk
            if state.side == Side.LONG:
                move = extreme - state.entry_price
            else:
                move = state.entry_price - extreme

            if move >= required_move:
                new_state.active = True
                activated_now = True

        if not new_state.active:
            return TrailingUpdate(state=new_state, newly_activated=False)

        # --- 3. Compute candidate SL from extreme ---
        candidate = _compute_candidate_sl(new_state, extreme)
        if candidate is None:
            return TrailingUpdate(state=new_state, newly_activated=activated_now)

        # --- 4. Monotonicity: only move SL in favorable direction ---
        if state.side == Side.LONG:
            if candidate > new_state.current_sl:
                old = new_state.current_sl
                new_state.current_sl = candidate
                return TrailingUpdate(
                    state=new_state,
                    sl_changed=True,
                    newly_activated=activated_now,
                    new_sl=candidate,
                    old_sl=old,
                    reason=_reason(new_state, extreme),
                )
        else:  # SHORT
            if candidate < new_state.current_sl:
                old = new_state.current_sl
                new_state.current_sl = candidate
                return TrailingUpdate(
                    state=new_state,
                    sl_changed=True,
                    newly_activated=activated_now,
                    new_sl=candidate,
                    old_sl=old,
                    reason=_reason(new_state, extreme),
                )

        return TrailingUpdate(state=new_state, newly_activated=activated_now)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _copy(s: TrailingState) -> TrailingState:
    return TrailingState(
        side=s.side,
        entry_price=s.entry_price,
        initial_sl=s.initial_sl,
        current_sl=s.current_sl,
        mode=s.mode,
        activation_rr=s.activation_rr,
        atr_mult=s.atr_mult,
        atr_snapshot=s.atr_snapshot,
        percent=s.percent,
        active=s.active,
        highest_price=s.highest_price,
        lowest_price=s.lowest_price,
    )


def _compute_candidate_sl(state: TrailingState, extreme: float) -> float | None:
    """Compute the raw candidate SL price for the given extreme.

    Monotonicity filtering is applied by the caller.
    """
    if state.mode == TrailingMode.ATR:
        if state.atr_snapshot <= 0:
            return None
        offset = state.atr_mult * state.atr_snapshot
    elif state.mode == TrailingMode.PERCENT:
        if state.percent <= 0:
            return None
        offset = extreme * (state.percent / 100.0)
    else:
        return None

    if state.side == Side.LONG:
        return extreme - offset
    return extreme + offset


def _reason(state: TrailingState, extreme: float) -> str:
    if state.mode == TrailingMode.ATR:
        return f"trail ATR×{state.atr_mult} @ extreme={extreme:.6g}"
    if state.mode == TrailingMode.PERCENT:
        return f"trail {state.percent:.2f}% @ extreme={extreme:.6g}"
    return "trail"


# --------------------------------------------------------------------------
# Convenience: build a TrailingState from a DB Trade row
# --------------------------------------------------------------------------


def state_from_trade(trade) -> TrailingState:  # noqa: ANN001 — avoid circular import
    """Materialize a :class:`TrailingState` from a ``Trade`` ORM row."""
    side_val = trade.side.value if hasattr(trade.side, "value") else str(trade.side)
    return TrailingState(
        side=Side(side_val),
        entry_price=trade.entry_price,
        initial_sl=trade.initial_sl if trade.initial_sl is not None else trade.stop_loss,
        current_sl=trade.stop_loss,
        mode=(
            trade.trailing_mode
            if isinstance(trade.trailing_mode, TrailingMode)
            else TrailingMode(trade.trailing_mode)
        ),
        activation_rr=trade.trailing_activation_rr,
        atr_mult=trade.trailing_atr_mult,
        atr_snapshot=trade.trailing_atr_snapshot,
        percent=trade.trailing_percent,
        active=trade.trailing_active,
        highest_price=trade.highest_price,
        lowest_price=trade.lowest_price,
    )


def apply_state_to_trade(trade, state: TrailingState) -> None:  # noqa: ANN001
    """Copy relevant fields from a :class:`TrailingState` back onto a Trade row."""
    trade.stop_loss = state.current_sl
    trade.trailing_active = state.active
    trade.highest_price = state.highest_price
    trade.lowest_price = state.lowest_price
