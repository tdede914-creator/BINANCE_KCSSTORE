"""Market data source abstraction.

Allows the rest of the app (scanner, market endpoints, strategy) to fetch
klines and tickers without caring whether the underlying provider is
Binance USDT-M Futures (for crypto) or TwelveData (for FX).

Implementations must return DataFrames with the same schema so downstream
consumers stay identical:
    - index: pandas DatetimeIndex (UTC)
    - columns: open, high, low, close, volume
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import pandas as pd


class MarketDataSource(ABC):
    """Minimal read-only market data interface used by the scanner + UI."""

    @property
    @abstractmethod
    def market(self) -> str:
        """Human-readable market label, e.g. 'crypto' or 'forex'."""

    @abstractmethod
    async def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 500,
    ) -> pd.DataFrame:
        """Return recent OHLCV klines as a DataFrame.

        Schema
        ------
        Index:   ``DatetimeIndex`` in UTC, ascending.
        Columns: ``open`` ``high`` ``low`` ``close`` ``volume`` (all float).
        """

    @abstractmethod
    async def get_ticker_price(self, symbol: str) -> float:
        """Return the most recent trade price for ``symbol``."""

    @abstractmethod
    async def close(self) -> None:
        """Release any underlying HTTP client / sockets."""

    # Optional: for execution-related endpoints. Crypto exchanges expose
    # per-symbol filters (LOT_SIZE, MIN_NOTIONAL); forex signal-only mode
    # returns None.
    async def get_symbol_filters(self, symbol: str) -> dict[str, Any] | None:
        return None

    async def __aenter__(self) -> "MarketDataSource":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()
