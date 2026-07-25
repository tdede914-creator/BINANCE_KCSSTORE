"""TwelveData source for Forex + commodity charts.

Free tier limits (as of 2024-2025):
    - 800 requests / day
    - 8 requests / minute
    - No real-time WebSocket on free plan → callers should poll REST.

Symbol format
-------------
Users type MT5-style symbols (``XAUUSD``, ``EURUSD``, ``GBPJPY``).
TwelveData wants a slash between base and quote (``XAU/USD``). The
adapter inserts it transparently.

Interval mapping
----------------
TwelveData uses minutes/hours units. We map the intervals the app
supports (subset of Binance's) to the TwelveData equivalents.
"""
from __future__ import annotations

from datetime import timezone

import httpx
import pandas as pd

from app.core.logging import get_logger
from app.datasource.base import MarketDataSource

log = get_logger(__name__)


# Binance-style interval → TwelveData interval string.
INTERVAL_MAP = {
    "1m": "1min",
    "5m": "5min",
    "15m": "15min",
    "30m": "30min",
    "1h": "1h",
    "2h": "2h",
    "4h": "4h",
    "1d": "1day",
}


# Currency codes that must NOT be sliced 3+3. Extend if needed.
_LONG_BASES = {"XAG", "XAU", "XPT", "XPD"}   # commodities priced in /USD


class TwelveDataSource(MarketDataSource):
    BASE_URL = "https://api.twelvedata.com"

    def __init__(self, api_key: str, timeout: float = 15.0) -> None:
        if not api_key:
            raise ValueError("TwelveData API key is required")
        self.api_key = api_key
        self._client = httpx.AsyncClient(base_url=self.BASE_URL, timeout=timeout)

    @property
    def market(self) -> str:
        return "forex"

    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Symbol formatting
    # ------------------------------------------------------------------

    @staticmethod
    def _format_symbol(symbol: str) -> str:
        """Convert MT5-style ``XAUUSD`` → TwelveData ``XAU/USD``.

        Already-slashed symbols and unusual shapes are passed through.
        """
        s = symbol.upper().strip()
        if "/" in s:
            return s
        # Base of 3 chars (typical FX): "EURUSD" -> "EUR/USD"
        if len(s) == 6:
            return f"{s[:3]}/{s[3:]}"
        # Commodities where the base is 3 chars against USD.
        for base in _LONG_BASES:
            if s.startswith(base) and len(s) > 3:
                return f"{base}/{s[len(base):]}"
        return s

    # ------------------------------------------------------------------
    # Klines
    # ------------------------------------------------------------------

    async def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 500,
    ) -> pd.DataFrame:
        td_interval = INTERVAL_MAP.get(interval)
        if td_interval is None:
            raise ValueError(
                f"Interval {interval!r} is not supported by TwelveData; "
                f"valid: {', '.join(INTERVAL_MAP.keys())}"
            )
        fmt = self._format_symbol(symbol)

        r = await self._client.get(
            "/time_series",
            params={
                "symbol": fmt,
                "interval": td_interval,
                "outputsize": min(limit, 5000),
                "apikey": self.api_key,
                "format": "JSON",
                "timezone": "UTC",
            },
        )
        r.raise_for_status()
        data = r.json()

        if data.get("status") == "error":
            raise RuntimeError(
                f"TwelveData error: {data.get('message', 'unknown')}"
            )

        values = data.get("values", [])
        if not values:
            return _empty_klines_df()

        # TwelveData returns newest-first; we normalise to ascending.
        rows = []
        for v in values:
            rows.append(
                {
                    "open_time": pd.Timestamp(v["datetime"], tz=timezone.utc),
                    "open": float(v["open"]),
                    "high": float(v["high"]),
                    "low": float(v["low"]),
                    "close": float(v["close"]),
                    # FX/commodities usually have no volume; TwelveData sends 0.
                    "volume": float(v.get("volume") or 0.0),
                }
            )
        df = pd.DataFrame(rows).set_index("open_time").sort_index()
        return df[["open", "high", "low", "close", "volume"]]

    # ------------------------------------------------------------------
    # Ticker
    # ------------------------------------------------------------------

    async def get_ticker_price(self, symbol: str) -> float:
        fmt = self._format_symbol(symbol)
        r = await self._client.get(
            "/price",
            params={"symbol": fmt, "apikey": self.api_key},
        )
        r.raise_for_status()
        data = r.json()
        if data.get("status") == "error":
            raise RuntimeError(
                f"TwelveData error: {data.get('message', 'unknown')}"
            )
        return float(data["price"])


def _empty_klines_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": pd.Series(dtype=float),
            "high": pd.Series(dtype=float),
            "low": pd.Series(dtype=float),
            "close": pd.Series(dtype=float),
            "volume": pd.Series(dtype=float),
        }
    )
