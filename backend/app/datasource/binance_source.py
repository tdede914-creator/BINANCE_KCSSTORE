"""Binance USDT-M Futures data source — thin adapter over BinanceREST."""
from __future__ import annotations

from typing import Any

import pandas as pd

from app.binance.rest import BinanceREST
from app.datasource.base import MarketDataSource


class BinanceDataSource(MarketDataSource):
    def __init__(self, rest: BinanceREST | None = None) -> None:
        self._rest = rest or BinanceREST()
        self._owns_rest = rest is None

    @property
    def market(self) -> str:
        return "crypto"

    async def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 500,
    ) -> pd.DataFrame:
        return await self._rest.get_klines(symbol, interval, limit=limit)

    async def get_ticker_price(self, symbol: str) -> float:
        return await self._rest.get_ticker_price(symbol)

    async def get_symbol_filters(self, symbol: str) -> dict[str, Any] | None:
        return await self._rest.get_symbol_filters(symbol)

    async def close(self) -> None:
        if self._owns_rest:
            await self._rest.close()
