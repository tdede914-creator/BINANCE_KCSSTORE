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

Reconciliation logic
--------------------
After the entry fills, ``check_open_trades`` runs each scanner tick:

1. Poll open orders + position size.
2. If position size is zero → trade closed. Distinguish SL/TP by looking
   at which orders disappeared.
3. If TP1 order disappeared while SL is still open → TP1 hit. Update
   status to ``TP1_HIT``.
4. Trailing stop: run :class:`TrailingStopManager`. If it says SL should
   move, cancel the old ``STOP_MARKET`` and place a new one. The window
   between cancel and replace is ~100-300 ms; during that time the
   position is unprotected. We accept this because the alternative
   (Binance's native ``TRAILING_STOP_MARKET``) is less flexible.

For safety we only cancel+replace if the move is >= ``MIN_TRAIL_MOVE_BPS``
basis points of the current price (default 5 bps = 0.05%). This avoids
API rate-limit issues and micro-adjustments.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import select

from app.binance.rest import BinanceREST
from app.core.logging import get_logger
from app.core.security import decrypt_secret
from app.db.database import session_scope
from app.db.models import SignalStatus, Trade, TradingMode, TrailingMode, UserConfig
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

# --- Trailing thresholds ---
MIN_TRAIL_MOVE_BPS = 5   # 0.05% — don't cancel+replace for smaller moves
MIN_TRAIL_MOVE_ABS = 0.0  # override in ticks if you need tighter granularity


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
        *,
        trailing: TrailingConfig | None = None,
    ) -> ExecutionResult:
        tc = trailing or TrailingConfig()
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

        # -------------------------------------------------------------
        # Software SL / TP — we intentionally DO NOT place STOP_MARKET /
        # TAKE_PROFIT_MARKET orders on Binance any more.
        #
        # Binance's late-2024 policy started rejecting these order
        # types on POST /fapi/v1/order with error -4120 ("Order type
        # not supported for this endpoint. Please use the Algo Order
        # API endpoints instead.") — even with reduceOnly=true. That
        # would loop: entry fills, SL rejects, emergency-close, bleed
        # fees, repeat. Users lost real money to that loop.
        #
        # Instead we monitor price ourselves in `check_open_trades`
        # (identical to the PaperExecutor code path) and issue a
        # MARKET reduceOnly close when SL / TP1 / TP2 is crossed.
        # MARKET orders are Binance's most basic type and are never
        # rejected with -4120.
        #
        # Trade-offs:
        #   +  Never -4120 again. Never fee-bleed loop.
        #   +  Same code path as paper mode, easier to reason about.
        #   -  If the bot process dies, the position has no on-chain
        #      stop — the user must know this risk. Circuit breaker,
        #      restart-on-crash, and Telegram alerts keep the window
        #      of exposure small.
        #   -  ~1-2 seconds slippage on close vs on-exchange stop.
        #
        # For the $10-$100 paper-transitioning-to-live use case this
        # trade-off is very much worth it.
        # -------------------------------------------------------------
        sl_order_id = ""
        tp1_order_id = ""
        tp2_order_id = ""

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
            # --- Trailing state ---
            trailing_mode=tc.mode,
            trailing_activation_rr=tc.activation_rr,
            trailing_atr_mult=tc.atr_mult,
            trailing_percent=tc.percent,
            trailing_atr_snapshot=tc.atr_snapshot,
            trailing_active=False,
            highest_price=entry_price if order.side == Side.LONG else None,
            lowest_price=entry_price if order.side == Side.SHORT else None,
            initial_sl=order.stop_loss,
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
            trailing=tc.mode.value if tc.mode != TrailingMode.OFF else None,
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
        """Software SL/TP monitor for LIVE positions.

        Since we no longer place on-exchange stop orders (Binance -4120),
        we watch prices ourselves and close positions with MARKET
        reduceOnly orders when SL / TP1 / TP2 are crossed. Behaviour
        mirrors PaperExecutor.check_open_trades exactly, just wired to
        real Binance orders instead of DB updates.

        Trailing stop: if enabled, adjust the (in-memory) SL price on
        each tick — no on-exchange cancel-and-replace needed.
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
            price = current_prices.get(trade.symbol)
            if price is None:
                continue
            try:
                is_long = trade.side == Side.LONG.value or trade.side == "LONG"
                exit_side_str = "SELL" if is_long else "BUY"

                # ---- Update trailing (highest/lowest since entry) ----
                if is_long:
                    trade.highest_price = max(trade.highest_price or price, price)
                else:
                    trade.lowest_price = min(trade.lowest_price or price, price)

                # ---- SL hit ? ----
                sl_hit = (
                    (is_long and price <= trade.stop_loss)
                    or ((not is_long) and price >= trade.stop_loss)
                )
                if sl_hit:
                    await self._market_close(
                        trade, api_key, api_secret,
                        qty=trade.quantity if trade.status == SignalStatus.OPEN
                        else _round_qty(trade.quantity / 2, trade.quantity_precision or 3),
                        exit_side=exit_side_str,
                    )
                    trade.exit_price = trade.stop_loss
                    trade.closed_at = datetime.now(tz=timezone.utc)
                    if trade.trailing_active and trade.trailing_mode != TrailingMode.OFF:
                        trade.status = SignalStatus.CLOSED_TP
                        trade.notes = "live: trailing-SL exit"
                    else:
                        trade.status = SignalStatus.CLOSED_SL
                    changed.append(trade)
                    await _persist(trade)
                    continue

                # ---- TP2 hit (only when TP1 already gone) ? ----
                if trade.status == SignalStatus.TP1_HIT:
                    tp2_hit = (
                        (is_long and price >= trade.take_profit_2)
                        or ((not is_long) and price <= trade.take_profit_2)
                    )
                    if tp2_hit:
                        remaining = _round_qty(
                            trade.quantity / 2, trade.quantity_precision or 3
                        )
                        await self._market_close(
                            trade, api_key, api_secret,
                            qty=remaining, exit_side=exit_side_str,
                        )
                        trade.exit_price = trade.take_profit_2
                        trade.status = SignalStatus.CLOSED_TP
                        trade.closed_at = datetime.now(tz=timezone.utc)
                        changed.append(trade)
                        await _persist(trade)
                        continue

                # ---- TP1 hit ? (partial close + move SL to BE) ----
                if trade.status == SignalStatus.OPEN:
                    tp1_hit = (
                        (is_long and price >= trade.take_profit_1)
                        or ((not is_long) and price <= trade.take_profit_1)
                    )
                    if tp1_hit:
                        half = _round_qty(
                            trade.quantity / 2, trade.quantity_precision or 3
                        )
                        await self._market_close(
                            trade, api_key, api_secret,
                            qty=half, exit_side=exit_side_str,
                        )
                        trade.status = SignalStatus.TP1_HIT
                        # Move SL to break-even (unless trailing takes over)
                        if trade.trailing_mode == TrailingMode.OFF or not trade.trailing_active:
                            trade.stop_loss = trade.entry_price
                        changed.append(trade)
                        await _persist(trade)
                        continue

                # ---- Trailing adjustment (in-memory only) ----
                # Reuse the same TrailingStopManager the paper executor
                # uses so LIVE + PAPER behave identically.
                if trade.trailing_mode != TrailingMode.OFF:
                    state = state_from_trade(trade)
                    update = TrailingStopManager.update(state, price)
                    apply_state_to_trade(trade, update.state)
                    if update.sl_changed and update.new_sl is not None:
                        trade.stop_loss = update.new_sl
                    await _persist(trade)

            except Exception as e:  # noqa: BLE001
                log.warning("live.reconcile_error", trade_id=trade.id, error=str(e))

        return changed

    async def _market_close(
        self,
        trade: Trade,
        api_key: str,
        api_secret: str,
        *,
        qty: float,
        exit_side: str,
    ) -> None:
        """Close a chunk of the live position with a plain MARKET reduceOnly.

        Never raises. Errors are logged; caller continues to update
        the DB trade so the local view stays consistent with what the
        user sees in scanner_status / Telegram.
        """
        try:
            await self.rest.place_order(
                api_key,
                api_secret,
                symbol=trade.symbol,
                side=exit_side,
                type="MARKET",
                quantity=qty,
                reduceOnly="true",
                newOrderRespType="RESULT",
            )
            log.info(
                "live.market_close",
                trade_id=trade.id,
                symbol=trade.symbol,
                qty=qty,
                side=exit_side,
            )
        except Exception as e:  # noqa: BLE001
            log.error(
                "live.market_close_failed",
                trade_id=trade.id,
                error=str(e),
            )

    # ------------------------------------------------------------------
    # Internal — trailing / SL move
    # ------------------------------------------------------------------

    async def _apply_trailing(
        self,
        trade: Trade,
        current_price: float | None,
        api_key: str,
        api_secret: str,
    ) -> bool:
        """Advance trailing for one trade. Return True if the on-exchange SL moved."""
        if current_price is None or trade.trailing_mode == TrailingMode.OFF:
            return False

        state = state_from_trade(trade)
        update = TrailingStopManager.update(state, current_price)

        # Persist extreme drift even when SL didn't change.
        apply_state_to_trade(trade, update.state)
        await _persist(trade)

        if not update.sl_changed or update.new_sl is None:
            return update.newly_activated  # counts as a change worth broadcasting

        if not _worth_moving(update.old_sl, update.new_sl, current_price):
            log.debug(
                "live.trailing.skip_small_move",
                trade_id=trade.id,
                old_sl=update.old_sl,
                new_sl=update.new_sl,
            )
            return False

        ok = await self._move_sl_to(trade, update.new_sl, api_key, api_secret)
        if ok:
            log.info(
                "live.trailing.sl_moved",
                trade_id=trade.id,
                symbol=trade.symbol,
                old_sl=update.old_sl,
                new_sl=update.new_sl,
                reason=update.reason,
            )
        return ok

    async def _move_sl_to(
        self,
        trade: Trade,
        new_stop_price: float,
        api_key: str,
        api_secret: str,
    ) -> bool:
        """Cancel current SL order + place a new STOP_MARKET at ``new_stop_price``.

        Returns True on success, False on failure (in which case trade.stop_loss
        is NOT updated locally so we can retry).
        """
        exit_side = "SELL" if trade.side == Side.LONG.value else "BUY"

        # 1) Cancel existing SL, best-effort.
        try:
            if trade.sl_order_id:
                await self.rest.cancel_order(
                    trade.symbol, trade.sl_order_id, api_key, api_secret
                )
        except Exception as e:  # noqa: BLE001
            log.warning("live.sl_cancel_failed", trade_id=trade.id, error=str(e))

        # 2) Place new STOP_MARKET.
        try:
            resp = await self.rest.place_order(
                api_key,
                api_secret,
                symbol=trade.symbol,
                side=exit_side,
                type="STOP_MARKET",
                stopPrice=_fmt_price(new_stop_price, trade.price_precision if hasattr(trade, "price_precision") else 6),
                quantity=trade.quantity,
                reduceOnly="true",
                workingType="MARK_PRICE",
                newOrderRespType="RESULT",
            )
        except Exception as e:  # noqa: BLE001
            log.error(
                "live.sl_place_failed",
                trade_id=trade.id,
                new_sl=new_stop_price,
                error=str(e),
            )
            return False

        trade.sl_order_id = str(resp.get("orderId", ""))
        trade.stop_loss = new_stop_price
        await _persist(trade)
        return True

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


def _fmt_price(price: float, precision: int) -> str:
    """Format a price for Binance so we don't send float noise.

    Binance rejects orders whose price string doesn't match the
    symbol's PRICE_FILTER — 0.07286 is fine, but Python's default
    ``str(0.07286)`` can also emit ``0.07286000000000001`` when the
    float has representation noise. Formatting with a fixed number
    of decimals guarantees a clean payload.
    """
    return f"{price:.{max(precision, 0)}f}"


def _widen_stop(price: float, side: "Side", precision: int, ticks: int = 5) -> str:
    """Move a stop price ``ticks`` ticks further from the entry.

    For LONG (exit = SELL): SL is below entry, so a wider SL means a
    LOWER price → subtract.
    For SHORT (exit = BUY): SL is above entry, so a wider SL means a
    HIGHER price → add.
    """
    from app.strategy.types import Side  # local import to avoid cycle
    tick = 10 ** -precision
    delta = ticks * tick
    if side == Side.LONG:
        return _fmt_price(price - delta, precision)
    return _fmt_price(price + delta, precision)


def _worth_moving(old_sl: float | None, new_sl: float | None, current_price: float) -> bool:
    """Guard against thrashing the Binance API for micro-adjustments."""
    if old_sl is None or new_sl is None or current_price <= 0:
        return False
    diff = abs(new_sl - old_sl)
    bps = (diff / current_price) * 10_000
    if bps >= MIN_TRAIL_MOVE_BPS:
        return True
    if MIN_TRAIL_MOVE_ABS > 0 and diff >= MIN_TRAIL_MOVE_ABS:
        return True
    return False


async def _persist(trade: Trade) -> None:
    async with session_scope() as session:
        session.add(trade)


# Silence "imported but unused" for asyncio (kept for future extension)
_ = asyncio
