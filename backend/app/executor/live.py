"""Live trading executor for Binance USDT-M Futures.

Key design decision — zero-latency SL/TP
----------------------------------------
The moment an entry order fills, we submit **on-exchange stop orders**:

- ``STOP_MARKET`` with ``closePosition=true``            → SL
- ``TAKE_PROFIT_MARKET`` with ``reduceOnly=true, qty=50%`` → TP1
- ``TAKE_PROFIT_MARKET`` with ``closePosition=true``     → TP2

These orders live on Binance's matching engine. They fire the instant
price crosses the trigger, without any round-trip to our bot. If our
bot crashes, disconnects, or the server dies, the exit is still safe.

After TP1 fills, a periodic reconciliation task moves the SL to the
entry price (break-even) by cancelling + re-creating the STOP_MARKET
with the new trigger.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import select

from app.binance.rest import BinanceREST
from app.core.logging import get_logger
from app.core.security import decrypt_secret
from app.db.database import session_scope
from app.db.models import SignalStatus, Trade, TradingMode, UserConfig
from app.executor.base import BaseExecutor, ExecutionResult
from app.risk.manager import SizedOrder
from app.strategy.types import Side

log = get_logger(__name__)


class LiveExecutor(BaseExecutor):
    """Executes real orders. Requires encrypted API creds in UserConfig."""

    def __init__(self, rest: BinanceREST | None = None) -> None:
        self.rest = rest or BinanceREST()

    # ------------------------------------------------------------------
    # Credentials
    # ------------------------------------------------------------------

    async def _creds(self) -> tuple[str, str]:
        async with session_scope() as session:
            cfg = await session.get(UserConfig, 1)
            if cfg is None or not cfg.binance_api_key_enc:
                raise RuntimeError(
                    "Binance API credentials are not configured. "
                    "Save them via /api/config first."
                )
            key = decrypt_secret(cfg.binance_api_key_enc)
            secret = decrypt_secret(cfg.binance_api_secret_enc)
            return key, secret

    # ------------------------------------------------------------------
    # Open
    # ------------------------------------------------------------------

    async def open_trade(
        self,
        order: SizedOrder,
        signal_id: int | None = None,
    ) -> ExecutionResult:
        api_key, api_secret = await self._creds()
        symbol = order.symbol
        binance_side = "BUY" if order.side == Side.LONG else "SELL"
        exit_side = "SELL" if order.side == Side.LONG else "BUY"

        try:
            # Ensure leverage + margin mode.
            await self.rest.set_leverage(symbol, order.leverage, api_key, api_secret)
            await self.rest.set_margin_type(symbol, "ISOLATED", api_key, api_secret)
        except Exception as e:  # noqa: BLE001
            log.warning("live.leverage_setup_failed", error=str(e), symbol=symbol)

        # 1) Entry — MARKET order (fastest fill).
        try:
            entry_resp = await self.rest.place_order(
                api_key,
                api_secret,
                symbol=symbol,
                side=binance_side,
                type="MARKET",
                quantity=order.quantity,
                newOrderRespType="RESULT",
            )
        except Exception as e:  # noqa: BLE001
            log.error("live.entry_failed", symbol=symbol, error=str(e))
            return ExecutionResult(ok=False, error=f"entry failed: {e}")

        entry_price = float(entry_resp.get("avgPrice") or order.entry_price)
        entry_order_id = str(entry_resp.get("orderId", ""))

        # 2) SL — STOP_MARKET closePosition
        try:
            sl_resp = await self.rest.place_order(
                api_key,
                api_secret,
                symbol=symbol,
                side=exit_side,
                type="STOP_MARKET",
                stopPrice=order.stop_loss,
                closePosition="true",
                workingType="MARK_PRICE",
                priceProtect="true",
                newOrderRespType="RESULT",
            )
        except Exception as e:  # noqa: BLE001
            log.error("live.sl_failed", symbol=symbol, error=str(e))
            # Try emergency market close of the position.
            await self._emergency_close(symbol, order.quantity, exit_side, api_key, api_secret)
            return ExecutionResult(ok=False, error=f"SL failed, position closed: {e}")

        sl_order_id = str(sl_resp.get("orderId", ""))

        # 3) TP1 — TAKE_PROFIT_MARKET reduceOnly, 50%
        tp1_qty = _round_qty(order.quantity / 2, order.quantity_precision)
        tp1_order_id = ""
        if tp1_qty > 0:
            try:
                tp1_resp = await self.rest.place_order(
                    api_key,
                    api_secret,
                    symbol=symbol,
                    side=exit_side,
                    type="TAKE_PROFIT_MARKET",
                    stopPrice=order.take_profit_1,
                    quantity=tp1_qty,
                    reduceOnly="true",
                    workingType="MARK_PRICE",
                    priceProtect="true",
                    newOrderRespType="RESULT",
                )
                tp1_order_id = str(tp1_resp.get("orderId", ""))
            except Exception as e:  # noqa: BLE001
                log.warning("live.tp1_failed", symbol=symbol, error=str(e))

        # 4) TP2 — TAKE_PROFIT_MARKET closePosition
        tp2_order_id = ""
        try:
            tp2_resp = await self.rest.place_order(
                api_key,
                api_secret,
                symbol=symbol,
                side=exit_side,
                type="TAKE_PROFIT_MARKET",
                stopPrice=order.take_profit_2,
                closePosition="true",
                workingType="MARK_PRICE",
                priceProtect="true",
                newOrderRespType="RESULT",
            )
            tp2_order_id = str(tp2_resp.get("orderId", ""))
        except Exception as e:  # noqa: BLE001
            log.warning("live.tp2_failed", symbol=symbol, error=str(e))

        # 5) Persist
        trade = Trade(
            signal_id=signal_id,
            mode=TradingMode.LIVE,
            symbol=symbol,
            side=order.side.value,          # type: ignore[arg-type]
            leverage=order.leverage,
            entry_price=entry_price,
            quantity=order.quantity,
            stop_loss=order.stop_loss,
            take_profit_1=order.take_profit_1,
            take_profit_2=order.take_profit_2,
            entry_order_id=entry_order_id,
            sl_order_id=sl_order_id,
            tp1_order_id=tp1_order_id,
            tp2_order_id=tp2_order_id,
            status=SignalStatus.OPEN,
            fee_usdt=0.0,
            notes="live opened; SL/TP on exchange",
        )
        async with session_scope() as session:
            session.add(trade)
            await session.flush()

        log.info(
            "live.open",
            trade_id=trade.id,
            symbol=symbol,
            side=order.side.value,
            entry=entry_price,
            sl=order.stop_loss,
            tp1=order.take_profit_1,
            tp2=order.take_profit_2,
        )
        return ExecutionResult(
            ok=True,
            trade=trade,
            entry_order_id=entry_order_id,
            sl_order_id=sl_order_id,
            tp1_order_id=tp1_order_id,
            tp2_order_id=tp2_order_id,
        )

    # ------------------------------------------------------------------
    # Close (manual)
    # ------------------------------------------------------------------

    async def close_trade(
        self,
        trade: Trade,
        *,
        current_price: float,
        reason: str = "manual",
    ) -> ExecutionResult:
        api_key, api_secret = await self._creds()
        exit_side = "SELL" if trade.side == Side.LONG.value else "BUY"

        try:
            await self.rest.cancel_all_orders(trade.symbol, api_key, api_secret)
        except Exception as e:  # noqa: BLE001
            log.warning("live.cancel_failed", trade_id=trade.id, error=str(e))

        try:
            await self.rest.place_order(
                api_key,
                api_secret,
                symbol=trade.symbol,
                side=exit_side,
                type="MARKET",
                quantity=trade.quantity,
                reduceOnly="true",
                newOrderRespType="RESULT",
            )
        except Exception as e:  # noqa: BLE001
            log.error("live.close_failed", trade_id=trade.id, error=str(e))
            return ExecutionResult(ok=False, error=str(e))

        trade.exit_price = current_price
        trade.closed_at = datetime.now(tz=timezone.utc)
        trade.status = SignalStatus.CLOSED_MANUAL
        trade.notes = f"live closed: {reason}"
        async with session_scope() as session:
            session.add(trade)

        return ExecutionResult(ok=True, trade=trade)

    # ------------------------------------------------------------------
    # Reconciler
    # ------------------------------------------------------------------

    async def check_open_trades(self, current_prices: dict[str, float]) -> list[Trade]:
        """Poll Binance to see if SL/TP orders have filled.

        Also moves SL to break-even after TP1 fills.
        """
        try:
            api_key, api_secret = await self._creds()
        except RuntimeError:
            return []

        changed: list[Trade] = []
        async with session_scope() as session:
            result = await session.execute(
                select(Trade).where(
                    Trade.mode == TradingMode.LIVE,
                    Trade.status.in_([SignalStatus.OPEN, SignalStatus.TP1_HIT]),
                )
            )
            open_trades = list(result.scalars().all())

        for trade in open_trades:
            try:
                open_orders = await self.rest.get_open_orders(
                    trade.symbol, api_key, api_secret
                )
                order_ids = {str(o["orderId"]) for o in open_orders}

                sl_still_open = trade.sl_order_id in order_ids
                tp1_still_open = bool(trade.tp1_order_id) and trade.tp1_order_id in order_ids
                tp2_still_open = bool(trade.tp2_order_id) and trade.tp2_order_id in order_ids

                # If SL is gone AND we have no position → SL executed.
                position = await self.rest.get_position(trade.symbol, api_key, api_secret)
                pos_amt = float(position.get("positionAmt", 0)) if position else 0.0

                if abs(pos_amt) < 1e-9:
                    # Position closed. Determine reason.
                    if not sl_still_open and trade.status == SignalStatus.OPEN:
                        trade.status = SignalStatus.CLOSED_SL
                        trade.exit_price = trade.stop_loss
                    elif not tp2_still_open:
                        trade.status = SignalStatus.CLOSED_TP
                        trade.exit_price = trade.take_profit_2
                    else:
                        trade.status = SignalStatus.CLOSED_MANUAL

                    trade.closed_at = datetime.now(tz=timezone.utc)
                    # PnL via position risk not available here — leave to Binance income endpoint (TODO)
                    changed.append(trade)
                    await _persist(trade)
                    continue

                # If TP1 filled but SL still original → move SL to entry.
                if (
                    trade.status == SignalStatus.OPEN
                    and trade.tp1_order_id
                    and not tp1_still_open
                    and sl_still_open
                ):
                    await self._move_sl_to_breakeven(
                        trade, api_key, api_secret
                    )
                    trade.status = SignalStatus.TP1_HIT
                    trade.notes = "live: TP1 hit, SL moved to BE"
                    changed.append(trade)
                    await _persist(trade)

            except Exception as e:  # noqa: BLE001
                log.warning("live.reconcile_error", trade_id=trade.id, error=str(e))

        return changed

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _move_sl_to_breakeven(
        self,
        trade: Trade,
        api_key: str,
        api_secret: str,
    ) -> None:
        """Cancel existing SL, place new STOP_MARKET at entry price."""
        exit_side = "SELL" if trade.side == Side.LONG.value else "BUY"

        try:
            if trade.sl_order_id:
                await self.rest.cancel_order(
                    trade.symbol, trade.sl_order_id, api_key, api_secret
                )
        except Exception as e:  # noqa: BLE001
            log.warning("live.sl_cancel_failed", trade_id=trade.id, error=str(e))

        try:
            resp = await self.rest.place_order(
                api_key,
                api_secret,
                symbol=trade.symbol,
                side=exit_side,
                type="STOP_MARKET",
                stopPrice=trade.entry_price,
                closePosition="true",
                workingType="MARK_PRICE",
                priceProtect="true",
                newOrderRespType="RESULT",
            )
            trade.sl_order_id = str(resp.get("orderId", ""))
            trade.stop_loss = trade.entry_price
        except Exception as e:  # noqa: BLE001
            log.error("live.sl_move_failed", trade_id=trade.id, error=str(e))

    async def _emergency_close(
        self,
        symbol: str,
        qty: float,
        exit_side: str,
        api_key: str,
        api_secret: str,
    ) -> None:
        """Best-effort: close whatever position was just opened."""
        try:
            await self.rest.place_order(
                api_key,
                api_secret,
                symbol=symbol,
                side=exit_side,
                type="MARKET",
                quantity=qty,
                reduceOnly="true",
            )
            log.warning("live.emergency_close", symbol=symbol, qty=qty)
        except Exception as e:  # noqa: BLE001
            log.critical(
                "live.emergency_close_failed",
                symbol=symbol,
                qty=qty,
                error=str(e),
            )


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _round_qty(qty: float, precision: int) -> float:
    factor = 10**precision
    return int(qty * factor) / factor


async def _persist(trade: Trade) -> None:
    async with session_scope() as session:
        session.add(trade)


# Silence "imported but unused" for asyncio (kept for future extension)
_ = asyncio
