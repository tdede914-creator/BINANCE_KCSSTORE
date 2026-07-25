"""Abstract executor: how a signal → trade is submitted."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.db.models import Trade
from app.risk.manager import SizedOrder


@dataclass(slots=True)
class ExecutionResult:
    """Return value from :meth:`BaseExecutor.open_trade`."""

    ok: bool
    trade: Trade | None = None
    error: str | None = None
    # Optional Binance order ids (live only)
    entry_order_id: str | None = None
    sl_order_id: str | None = None
    tp1_order_id: str | None = None
    tp2_order_id: str | None = None


class BaseExecutor(ABC):
    """Interface implemented by PaperExecutor and LiveExecutor."""

    @abstractmethod
    async def open_trade(self, order: SizedOrder, signal_id: int | None = None) -> ExecutionResult:
        ...

    @abstractmethod
    async def close_trade(
        self,
        trade: Trade,
        *,
        current_price: float,
        reason: str = "manual",
    ) -> ExecutionResult:
        ...

    @abstractmethod
    async def check_open_trades(self, current_prices: dict[str, float]) -> list[Trade]:
        """Called periodically. Return trades whose state changed
        (e.g. SL/TP hit in paper mode, order status change in live mode)."""
        ...
