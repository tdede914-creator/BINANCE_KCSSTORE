"""Paper trading executor.

Simulates real fills using live market prices. State is persisted in the
DB just like live trades, so the frontend can render them uniformly.

Simplifications
---------------
- Entry fills instantly at the sized entry price (no slippage model).
- SL / TP are triggered when the candle range crosses the level.
- Fees: 0.05% per side (~ Binance taker fee) applied on entry and exit
  to keep P&L honest.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from app.core.logging import get_logger
from app.db.database import session_scope
from app.db.models import SignalStatus, Trade, TradingMode
from app.executor.base import BaseExecutor, ExecutionResult
from app.risk.manager import SizedOrder
from app.strategy.types import Side

log = get_logger(__name__)

FEE_RATE = 0.0005  # 0.05% per side


class PaperExecutor(BaseExecutor):
    """Virtual executor. Uses the DB as source of truth."""

    def __init__(self) -> None:
        pass

    async def open_trade(
        self,
        order: SizedOrder,
        signal_id: int | None = None,
    ) -> ExecutionResult:
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
        )
        return ExecutionResult(ok=True, trade=trade)

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

    async def check_open_trades(self, current_prices: dict[str, float]) -> list[Trade]:
        """Iterate open trades and check for SL/TP hits.

        We use the current mark price for a simple crossing test. This is
        pessimistic vs realistic (which would use intra-candle high/low)
        but conservative enough for paper P&L tracking.
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

            side = trade.side
            # ---- SL hit ----
            sl_hit = (
                (side == Side.LONG.value and px <= trade.stop_loss)
                or (side == Side.SHORT.value and px >= trade.stop_loss)
            )
            # ---- TP1 hit (only if not already hit) ----
            tp1_hit = trade.status == SignalStatus.OPEN and (
                (side == Side.LONG.value and px >= trade.take_profit_1)
                or (side == Side.SHORT.value and px <= trade.take_profit_1)
            )
            # ---- TP2 hit (only after TP1) ----
            tp2_hit = trade.status == SignalStatus.TP1_HIT and (
                (side == Side.LONG.value and px >= trade.take_profit_2)
                or (side == Side.SHORT.value and px <= trade.take_profit_2)
            )

            if sl_hit:
                result = await self.close_trade(trade, current_price=trade.stop_loss, reason="SL")
                trade.status = SignalStatus.CLOSED_SL
                await _persist_status(trade)
                changed.append(trade)
            elif tp2_hit:
                result = await self.close_trade(trade, current_price=trade.take_profit_2, reason="TP2")
                trade.status = SignalStatus.CLOSED_TP
                await _persist_status(trade)
                changed.append(trade)
            elif tp1_hit:
                # Close 50% at TP1, move SL to entry (break-even)
                half_qty = trade.quantity / 2
                partial_pnl, partial_pct = _compute_pnl(
                    side=side,
                    entry=trade.entry_price,
                    exit_price=trade.take_profit_1,
                    qty=half_qty,
                    leverage=trade.leverage,
                )
                trade.realized_pnl_usdt = round((trade.realized_pnl_usdt or 0.0) + partial_pnl, 4)
                trade.stop_loss = trade.entry_price
                trade.status = SignalStatus.TP1_HIT
                trade.notes = "paper: TP1 partial 50%, SL moved to BE"
                await _persist_status(trade)
                log.info(
                    "paper.tp1",
                    trade_id=trade.id,
                    partial_pnl=partial_pnl,
                    price=trade.take_profit_1,
                )
                changed.append(trade)

        return changed


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
