"""Paper trading executor.

Simulates real fills using live market prices. State is persisted in the
DB just like live trades, so the frontend can render them uniformly.

Simplifications
---------------
- Entry fills instantly at the sized entry price (no slippage model).
- SL / TP are triggered when the current mark price crosses the level.
  (Using intra-candle high/low would be less conservative — this way is
  pessimistic and honest.)
- Fees: 0.05% per side (~ Binance taker fee) applied on entry and exit.

Trailing-stop integration
-------------------------
Each ``check_open_trades`` tick, for every open trade the trailing manager
is run **first**. If it moves the SL in the favorable direction, the
change is persisted to the DB and included in the returned "changed" list
so the frontend gets a live update. The SL-hit check then uses the
possibly-updated level.

When ``trailing_mode`` is OFF, the legacy behavior applies: on TP1 hit,
close 50% and shift SL to break-even (entry price).
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from app.core.logging import get_logger
from app.db.database import session_scope
from app.db.models import SignalStatus, Trade, TradingMode, TrailingMode
from app.executor.base import BaseExecutor, ExecutionResult
from app.risk.manager import SizedOrder
from app.risk.trailing import (
    TrailingConfig,
    TrailingStopManager,
    apply_state_to_trade,
    state_from_trade,
)
from app.strategy.types import Side

log = get_logger(__name__)

FEE_RATE = 0.0005  # 0.05% per side


class PaperExecutor(BaseExecutor):
    """Virtual executor. Uses the DB as source of truth."""

    def __init__(self) -> None:
        pass

    # ------------------------------------------------------------------
    # Open
    # ------------------------------------------------------------------

    async def open_trade(
        self,
        order: SizedOrder,
        signal_id: int | None = None,
        *,
        trailing: TrailingConfig | None = None,
    ) -> ExecutionResult:
        tc = trailing or TrailingConfig()

        trade = Trade(
            signal_id=signal_id,
            mode=TradingMode.PAPER,
            symbol=order.symbol,
            side=order.side.value,          # type: ignore[arg-type]
            leverage=order.leverage,
            entry_price=order.entry_price,
            quantity=order.quantity,
            stop_loss=order.stop_loss,
            take_profit_1=order.take_profit_1,
            take_profit_2=order.take_profit_2,
            status=SignalStatus.OPEN,
            fee_usdt=order.notional_usdt * FEE_RATE,
            notes="paper open",
            # --- Trailing state ---
            trailing_mode=tc.mode,
            trailing_activation_rr=tc.activation_rr,
            trailing_atr_mult=tc.atr_mult,
            trailing_percent=tc.percent,
            trailing_atr_snapshot=tc.atr_snapshot,
            trailing_active=False,
            highest_price=order.entry_price if order.side == Side.LONG else None,
            lowest_price=order.entry_price if order.side == Side.SHORT else None,
            initial_sl=order.stop_loss,
        )
        async with session_scope() as session:
            session.add(trade)
            await session.flush()
            trade_id = trade.id

        log.info(
            "paper.open",
            trade_id=trade_id,
            symbol=order.symbol,
            side=order.side.value,
            qty=order.quantity,
            entry=order.entry_price,
            sl=order.stop_loss,
            tp1=order.take_profit_1,
            tp2=order.take_profit_2,
            trailing=tc.mode.value if tc.mode != TrailingMode.OFF else None,
        )
        return ExecutionResult(ok=True, trade=trade)

    # ------------------------------------------------------------------
    # Close (manual or auto)
    # ------------------------------------------------------------------

    async def close_trade(
        self,
        trade: Trade,
        *,
        current_price: float,
        reason: str = "manual",
    ) -> ExecutionResult:
        pnl_usdt, pnl_pct = _compute_pnl(
            side=trade.side,
            entry=trade.entry_price,
            exit_price=current_price,
            qty=trade.quantity,
            leverage=trade.leverage,
        )
        exit_fee = current_price * trade.quantity * FEE_RATE
        total_fee = trade.fee_usdt + exit_fee
        realized = pnl_usdt - exit_fee

        trade.exit_price = current_price
        trade.realized_pnl_usdt = round(realized, 4)
        trade.realized_pnl_pct = round(pnl_pct * 100.0, 4)
        trade.fee_usdt = round(total_fee, 4)
        trade.closed_at = datetime.now(tz=timezone.utc)
        trade.notes = f"paper close: {reason}"
        # Status set by caller based on reason

        async with session_scope() as session:
            session.add(trade)

        log.info(
            "paper.close",
            trade_id=trade.id,
            reason=reason,
            price=current_price,
            pnl_usdt=realized,
        )
        return ExecutionResult(ok=True, trade=trade)

    # ------------------------------------------------------------------
    # Reconciler — SL/TP crossing + trailing
    # ------------------------------------------------------------------

    async def check_open_trades(self, current_prices: dict[str, float]) -> list[Trade]:
        """Advance every open paper trade one tick.

        Order of operations per trade:
        1. Trailing update (may move SL favorably) → persist + broadcast.
        2. SL hit check (uses the possibly-updated SL).
        3. TP hit checks (TP1 partial, TP2 full).
        """
        changed: list[Trade] = []

        async with session_scope() as session:
            result = await session.execute(
                select(Trade).where(
                    Trade.mode == TradingMode.PAPER,
                    Trade.status.in_([SignalStatus.OPEN, SignalStatus.TP1_HIT]),
                )
            )
            open_trades = list(result.scalars().all())

        for trade in open_trades:
            px = current_prices.get(trade.symbol)
            if px is None:
                continue

            # ---- 1. Trailing ----
            trailing_moved = await self._apply_trailing(trade, px)
            if trailing_moved:
                changed.append(trade)

            side = trade.side
            # ---- 2. SL hit ----
            sl_hit = (
                (side == Side.LONG.value and px <= trade.stop_loss)
                or (side == Side.SHORT.value and px >= trade.stop_loss)
            )
            # ---- 3. TP1 hit (only if not already hit) ----
            tp1_hit = trade.status == SignalStatus.OPEN and (
                (side == Side.LONG.value and px >= trade.take_profit_1)
                or (side == Side.SHORT.value and px <= trade.take_profit_1)
            )
            # ---- 4. TP2 hit (only after TP1) ----
            tp2_hit = trade.status == SignalStatus.TP1_HIT and (
                (side == Side.LONG.value and px >= trade.take_profit_2)
                or (side == Side.SHORT.value and px <= trade.take_profit_2)
            )

            if sl_hit:
                await self.close_trade(trade, current_price=trade.stop_loss, reason="SL")
                # Distinguish trailing exit from initial-SL loss for reporting.
                if trade.trailing_active and trade.trailing_mode != TrailingMode.OFF:
                    trade.status = SignalStatus.CLOSED_TP  # trailing = profit-lock exit
                    trade.notes = f"paper close: trailing-SL exit @ {trade.stop_loss}"
                else:
                    trade.status = SignalStatus.CLOSED_SL
                await _persist_status(trade)
                if trade not in changed:
                    changed.append(trade)
            elif tp2_hit:
                await self.close_trade(trade, current_price=trade.take_profit_2, reason="TP2")
                trade.status = SignalStatus.CLOSED_TP
                await _persist_status(trade)
                if trade not in changed:
                    changed.append(trade)
            elif tp1_hit:
                # Partial 50% close at TP1
                half_qty = trade.quantity / 2
                partial_pnl, _ = _compute_pnl(
                    side=side,
                    entry=trade.entry_price,
                    exit_price=trade.take_profit_1,
                    qty=half_qty,
                    leverage=trade.leverage,
                )
                trade.realized_pnl_usdt = round((trade.realized_pnl_usdt or 0.0) + partial_pnl, 4)

                # If trailing is off, do the legacy "move SL to BE".
                # If trailing is active, leave SL where trailing put it.
                if trade.trailing_mode == TrailingMode.OFF and not trade.trailing_active:
                    trade.stop_loss = trade.entry_price
                    trade.notes = "paper: TP1 partial 50%, SL moved to BE"
                else:
                    trade.notes = "paper: TP1 partial 50%, trailing SL active"

                trade.status = SignalStatus.TP1_HIT
                await _persist_status(trade)
                log.info(
                    "paper.tp1",
                    trade_id=trade.id,
                    partial_pnl=partial_pnl,
                    price=trade.take_profit_1,
                    trailing_active=trade.trailing_active,
                )
                if trade not in changed:
                    changed.append(trade)

        return changed

    # ------------------------------------------------------------------
    # Trailing helper
    # ------------------------------------------------------------------

    async def _apply_trailing(self, trade: Trade, current_price: float) -> bool:
        """Run the trailing manager for one trade. Return True if SL moved."""
        if trade.trailing_mode == TrailingMode.OFF:
            return False

        state = state_from_trade(trade)
        update = TrailingStopManager.update(state, current_price)

        if not (update.sl_changed or update.newly_activated):
            # Still record the extreme drift so highest/lowest_price stay fresh.
            apply_state_to_trade(trade, update.state)
            await _persist_status(trade)
            return False

        apply_state_to_trade(trade, update.state)

        if update.sl_changed:
            log.info(
                "paper.trailing.sl_moved",
                trade_id=trade.id,
                symbol=trade.symbol,
                old_sl=update.old_sl,
                new_sl=update.new_sl,
                reason=update.reason,
            )
        if update.newly_activated:
            log.info(
                "paper.trailing.activated",
                trade_id=trade.id,
                symbol=trade.symbol,
                current_price=current_price,
            )

        await _persist_status(trade)
        return True


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _compute_pnl(
    *,
    side: str,
    entry: float,
    exit_price: float,
    qty: float,
    leverage: int,
) -> tuple[float, float]:
    """Return (pnl_usdt, pnl_pct_of_margin)."""
    if side == Side.LONG.value:
        move = exit_price - entry
    else:
        move = entry - exit_price
    pnl_usdt = move * qty
    margin = (entry * qty) / max(leverage, 1)
    pnl_pct = pnl_usdt / max(margin, 1e-9)
    return pnl_usdt, pnl_pct


async def _persist_status(trade: Trade) -> None:
    async with session_scope() as session:
        session.add(trade)
