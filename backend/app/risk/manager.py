"""Risk manager — position sizing and SL/TP rounding to exchange filters.

Position sizing rule
--------------------
::

    risk_usdt      = equity * (risk_pct / 100)
    price_risk_pct = abs(entry - stop_loss) / entry
    notional       = risk_usdt / price_risk_pct
    quantity       = notional / entry                # rounded to LOT_SIZE step
    required_margin = notional / leverage

We then check:

- ``notional >= MIN_NOTIONAL``
- ``required_margin <= equity`` (with safety buffer)
- ``quantity`` is rounded down to the symbol's step size

The strategy already returns SL/TP. Risk manager just double-checks that
the SL is not absurdly close (min 0.1% distance) and rounds everything
to valid exchange precision.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from app.core.logging import get_logger
from app.strategy.types import Side, SignalProposal

log = get_logger(__name__)


@dataclass(slots=True)
class SizedOrder:
    """Complete order-ready plan produced by the risk manager."""

    symbol: str
    side: Side
    entry_price: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    quantity: float
    notional_usdt: float
    margin_usdt: float
    leverage: int
    risk_usdt: float
    price_precision: int
    quantity_precision: int


# --------------------------------------------------------------------------
# Rounding helpers
# --------------------------------------------------------------------------


def _step_from_filter(f: dict, key: str) -> float:
    try:
        return float(f.get(key, "0") or 0)
    except (TypeError, ValueError):
        return 0.0


def round_step(value: float, step: float, *, mode: str = "down") -> float:
    """Round value to a multiple of step.

    :param mode: 'down' | 'up' | 'nearest'
    """
    if step <= 0:
        return value
    n = value / step
    if mode == "down":
        n = math.floor(n)
    elif mode == "up":
        n = math.ceil(n)
    else:
        n = round(n)
    return n * step


def round_price(price: float, precision: int) -> float:
    factor = 10**precision
    return math.floor(price * factor) / factor


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class RiskRejected(Exception):
    """Raised when the plan can't safely be sized."""


# --------------------------------------------------------------------------
# Manager
# --------------------------------------------------------------------------


class RiskManager:
    """Sizes a SignalProposal against exchange filters + user risk config."""

    def __init__(
        self,
        equity_usdt: float,
        risk_per_trade_pct: float,
        leverage: int,
        symbol_filters: dict,
        *,
        min_price_risk_pct: float = 0.001,   # 0.1%
        max_price_risk_pct: float = 0.05,    # 5%
    ) -> None:
        self.equity = float(equity_usdt)
        self.risk_pct = float(risk_per_trade_pct)
        self.leverage = int(leverage)
        self.filters = symbol_filters
        self.min_price_risk_pct = min_price_risk_pct
        self.max_price_risk_pct = max_price_risk_pct

        # Extract useful bits
        lot = symbol_filters.get("LOT_SIZE", {})
        price_f = symbol_filters.get("PRICE_FILTER", {})
        min_notional_f = symbol_filters.get("MIN_NOTIONAL", {})

        self.step_size = _step_from_filter(lot, "stepSize")
        self.min_qty = _step_from_filter(lot, "minQty")
        self.tick_size = _step_from_filter(price_f, "tickSize")
        self.min_notional = _step_from_filter(min_notional_f, "notional") or 5.0

        self.quantity_precision = int(symbol_filters.get("quantityPrecision", 3))
        self.price_precision = int(symbol_filters.get("pricePrecision", 2))

    def size(self, proposal: SignalProposal) -> SizedOrder:
        entry = proposal.entry_price
        sl = proposal.stop_loss
        tp1 = proposal.take_profit_1
        tp2 = proposal.take_profit_2

        # ---------- basic sanity ----------
        if proposal.side == Side.LONG and not (sl < entry < tp1 <= tp2):
            raise RiskRejected(f"long price order invalid: sl={sl} entry={entry} tp1={tp1} tp2={tp2}")
        if proposal.side == Side.SHORT and not (tp2 <= tp1 < entry < sl):
            raise RiskRejected(f"short price order invalid: sl={sl} entry={entry} tp1={tp1} tp2={tp2}")

        price_risk_pct = abs(entry - sl) / entry
        if price_risk_pct < self.min_price_risk_pct:
            raise RiskRejected(
                f"SL too tight ({price_risk_pct*100:.3f}% < {self.min_price_risk_pct*100:.2f}%)"
            )
        if price_risk_pct > self.max_price_risk_pct:
            raise RiskRejected(
                f"SL too wide ({price_risk_pct*100:.3f}% > {self.max_price_risk_pct*100:.2f}%)"
            )

        # ---------- size ----------
        risk_usdt = self.equity * (self.risk_pct / 100.0)
        notional = risk_usdt / price_risk_pct
        margin = notional / self.leverage

        if margin > self.equity:
            # Cap notional so we don't over-leverage
            max_notional = self.equity * self.leverage * 0.95
            notional = min(notional, max_notional)
            margin = notional / self.leverage
            log.warning("risk.margin_capped", symbol=proposal.symbol, margin=margin)

        raw_qty = notional / entry
        qty = round_step(raw_qty, self.step_size, mode="down") if self.step_size else raw_qty

        if self.min_qty and qty < self.min_qty:
            raise RiskRejected(
                f"qty {qty} < minQty {self.min_qty} — increase equity/risk or use smaller-priced symbol"
            )

        if qty * entry < self.min_notional:
            raise RiskRejected(
                f"notional {qty*entry:.2f} < minNotional {self.min_notional:.2f}"
            )

        # ---------- round prices ----------
        entry_r = round_price(entry, self.price_precision)
        sl_r = round_price(sl, self.price_precision)
        tp1_r = round_price(tp1, self.price_precision)
        tp2_r = round_price(tp2, self.price_precision)

        return SizedOrder(
            symbol=proposal.symbol,
            side=proposal.side,
            entry_price=entry_r,
            stop_loss=sl_r,
            take_profit_1=tp1_r,
            take_profit_2=tp2_r,
            quantity=round(qty, self.quantity_precision),
            notional_usdt=round(qty * entry_r, 2),
            margin_usdt=round(qty * entry_r / self.leverage, 2),
            leverage=self.leverage,
            risk_usdt=round(risk_usdt, 2),
            price_precision=self.price_precision,
            quantity_precision=self.quantity_precision,
        )
