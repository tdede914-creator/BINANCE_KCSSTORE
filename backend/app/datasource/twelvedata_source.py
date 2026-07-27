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


# Metals priced against USD — base of 3 chars, quote appended.
_METAL_BASES = {"XAG", "XAU", "XPT", "XPD"}

# Symbols that TwelveData accepts as-is (no slash). Includes:
#   * indices — S&P 500 (SPX), Nasdaq (NDX), Dow (DJI), UK (FTSE),
#     Germany (DAX), Japan (N225), Hong Kong (HSI)
#   * energies — WTI crude (WTI), Brent (BRENT), natural gas (NG)
#   * common Exness-style aliases we map into TwelveData equivalents
#     via _EXNESS_TO_TD below.
_ATOMIC_SYMBOLS = {
    # Indices
    "SPX", "NDX", "DJI", "FTSE", "DAX", "N225", "HSI", "STOXX50E",
    # Energies
    "WTI", "BRENT", "NG",
}

# Exness / MT5 conventions that we translate to TwelveData names.
# The dashboard accepts either the Exness ticker OR the TwelveData
# one — this keeps the UX broker-familiar.
_EXNESS_TO_TD = {
    # Indices (Exness naming → TwelveData)
    "US500": "SPX",
    "SPX500": "SPX",
    "US100": "NDX",
    "NAS100": "NDX",
    "USTEC": "NDX",
    "US30": "DJI",
    "WS30": "DJI",
    "UK100": "FTSE",
    "GER30": "DAX",
    "GER40": "DAX",
    "JPN225": "N225",
    "HK50": "HSI",
    # Energies
    "USOIL": "WTI",
    "UKOIL": "BRENT",
    "XTIUSD": "WTI",
    "XBRUSD": "BRENT",
    # Silver / platinum aliases
    "SILVER": "XAG/USD",
    "GOLD": "XAU/USD",
}


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
        """Normalise a user-typed ticker into a TwelveData symbol.

        Handled shapes (all case-insensitive):
            * ``EURUSD``        → ``EUR/USD``          (FX pair)
            * ``EUR/USD``       → ``EUR/USD``          (already slashed)
            * ``XAUUSD``        → ``XAU/USD``          (metal vs USD)
            * ``SPX``           → ``SPX``              (index, atomic)
            * ``US500``         → ``SPX``              (Exness alias)
            * ``NAS100``        → ``NDX``              (Exness alias)
            * ``USOIL``         → ``WTI``              (energy alias)
            * ``GOLD``          → ``XAU/USD``          (alias)
            * ``AAPL``          → ``AAPL``             (stock — passthrough)

        Unknown 3-4 char tickers are passed through untouched — TwelveData
        supports thousands of stock symbols, so treating them as atomic
        is the right default.
        """
        s = symbol.upper().strip()
        if not s:
            return s

        # 1. Exness-style aliases — translate first so the mapped value
        #    then falls through the rest of the normalisation.
        if s in _EXNESS_TO_TD:
            s = _EXNESS_TO_TD[s]

        # 2. Already slashed — trust the user.
        if "/" in s:
            return s

        # 3. Metals vs USD — "XAUUSD" -> "XAU/USD"
        for base in _METAL_BASES:
            if s.startswith(base) and len(s) > 3:
                return f"{base}/{s[len(base):]}"

        # 4. Atomic (indices / oil / other single-symbol tickers).
        if s in _ATOMIC_SYMBOLS:
            return s

        # 5. Six-char FX pair → slash it.
        if len(s) == 6 and s.isalpha():
            return f"{s[:3]}/{s[3:]}"

        # 6. Fallback — assume stock ticker or something TwelveData
        #    knows by its raw name.
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
