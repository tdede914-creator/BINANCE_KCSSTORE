"""Async wrapper around Binance USDT-M Futures REST API.

We use raw httpx for read-only endpoints (klines, ticker, exchange info) so
that no API key is required for scanning. For trading endpoints we use
python-binance's async client.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any
from urllib.parse import urlencode

import httpx
import pandas as pd

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)


# --------------------------------------------------------------------------
# Kline / market-data helpers (no API key needed)
# --------------------------------------------------------------------------


TIMEFRAME_TO_INTERVAL = {
    "1m": "1m",
    "3m": "3m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1h",
    "2h": "2h",
    "4h": "4h",
    "6h": "6h",
    "8h": "8h",
    "12h": "12h",
    "1d": "1d",
    "3d": "3d",
    "1w": "1w",
}

VALID_TIMEFRAMES = list(TIMEFRAME_TO_INTERVAL.keys())


class BinanceREST:
    """Minimal async REST client for Binance USDT-M Futures.

    - Public methods (klines, ticker, exchange_info) require no credentials.
    - Signed methods (account, order create/cancel, leverage, etc.) require
      the caller to pass api_key + api_secret.
    """

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.base_url = base_url or settings.binance_rest_url
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> BinanceREST:
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    # ----------------------------------------------------------------------
    # Public endpoints
    # ----------------------------------------------------------------------

    async def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 500,
    ) -> pd.DataFrame:
        """Fetch historical klines as a pandas DataFrame.

        Columns: open_time (index), open, high, low, close, volume.
        """
        if interval not in TIMEFRAME_TO_INTERVAL:
            raise ValueError(f"Invalid interval: {interval}")

        r = await self._client.get(
            "/fapi/v1/klines",
            params={"symbol": symbol.upper(), "interval": interval, "limit": limit},
        )
        r.raise_for_status()
        raw = r.json()

        df = pd.DataFrame(
            raw,
            columns=[
                "open_time",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "close_time",
                "quote_asset_volume",
                "num_trades",
                "taker_buy_base_vol",
                "taker_buy_quote_vol",
                "ignore",
            ],
        )
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = df[col].astype(float)
        df = df.set_index("open_time")
        return df[["open", "high", "low", "close", "volume"]]

    async def get_ticker_price(self, symbol: str) -> float:
        r = await self._client.get(
            "/fapi/v1/ticker/price", params={"symbol": symbol.upper()}
        )
        r.raise_for_status()
        return float(r.json()["price"])

    async def get_exchange_info(self) -> dict:
        r = await self._client.get("/fapi/v1/exchangeInfo")
        r.raise_for_status()
        return r.json()

    async def get_symbol_filters(self, symbol: str) -> dict:
        """Return quantity/price filters for a symbol (for rounding orders)."""
        info = await self.get_exchange_info()
        for s in info.get("symbols", []):
            if s["symbol"] == symbol.upper():
                filters = {f["filterType"]: f for f in s["filters"]}
                return {
                    "quantityPrecision": s["quantityPrecision"],
                    "pricePrecision": s["pricePrecision"],
                    "LOT_SIZE": filters.get("LOT_SIZE", {}),
                    "PRICE_FILTER": filters.get("PRICE_FILTER", {}),
                    "MIN_NOTIONAL": filters.get("MIN_NOTIONAL", {}),
                }
        raise ValueError(f"Symbol not found: {symbol}")

    # ----------------------------------------------------------------------
    # Signed endpoints
    # ----------------------------------------------------------------------

    def _sign(self, params: dict[str, Any], secret: str) -> dict[str, Any]:
        params["timestamp"] = int(time.time() * 1000)
        query = urlencode(params)
        signature = hmac.new(
            secret.encode(), query.encode(), hashlib.sha256
        ).hexdigest()
        params["signature"] = signature
        return params

    async def _signed_request(
        self,
        method: str,
        path: str,
        api_key: str,
        api_secret: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        params = self._sign(params or {}, api_secret)
        headers = {"X-MBX-APIKEY": api_key}
        r = await self._client.request(method, path, params=params, headers=headers)
        if r.status_code >= 400:
            log.error(
                "binance.error",
                status=r.status_code,
                body=r.text,
                path=path,
                method=method,
            )
            r.raise_for_status()
        return r.json()

    async def get_account(self, api_key: str, api_secret: str) -> dict:
        return await self._signed_request(
            "GET", "/fapi/v2/account", api_key, api_secret
        )

    async def get_balance_usdt(self, api_key: str, api_secret: str) -> float:
        acct = await self.get_account(api_key, api_secret)
        for asset in acct.get("assets", []):
            if asset.get("asset") == "USDT":
                return float(asset.get("availableBalance", 0.0))
        return 0.0

    async def get_balance_info(self, api_key: str, api_secret: str) -> dict:
        """Return the full USDT balance snapshot for the futures wallet.

        Shape:
            {
                "wallet_balance": float,     # walletBalance (excludes unreal.)
                "available_balance": float,  # availableBalance (free margin)
                "unrealized_pnl": float,     # unrealizedProfit on all positions
                "margin_balance": float,     # walletBalance + unrealizedProfit
                "initial_margin": float,     # currently locked in open positions
            }

        This is what the dashboard uses to show LIVE-mode equity /
        free-margin cards, without collapsing to a single number the
        way ``get_balance_usdt`` does.
        """
        acct = await self.get_account(api_key, api_secret)
        for asset in acct.get("assets", []):
            if asset.get("asset") == "USDT":
                return {
                    "wallet_balance": float(asset.get("walletBalance", 0.0)),
                    "available_balance": float(asset.get("availableBalance", 0.0)),
                    "unrealized_pnl": float(asset.get("unrealizedProfit", 0.0)),
                    "margin_balance": float(asset.get("marginBalance", 0.0)),
                    "initial_margin": float(asset.get("initialMargin", 0.0)),
                }
        return {
            "wallet_balance": 0.0,
            "available_balance": 0.0,
            "unrealized_pnl": 0.0,
            "margin_balance": 0.0,
            "initial_margin": 0.0,
        }

    async def set_leverage(
        self, symbol: str, leverage: int, api_key: str, api_secret: str
    ) -> dict:
        return await self._signed_request(
            "POST",
            "/fapi/v1/leverage",
            api_key,
            api_secret,
            {"symbol": symbol.upper(), "leverage": leverage},
        )

    async def set_margin_type(
        self,
        symbol: str,
        margin_type: str,
        api_key: str,
        api_secret: str,
    ) -> dict:
        """margin_type: ISOLATED or CROSSED"""
        try:
            return await self._signed_request(
                "POST",
                "/fapi/v1/marginType",
                api_key,
                api_secret,
                {"symbol": symbol.upper(), "marginType": margin_type},
            )
        except httpx.HTTPStatusError as e:
            # -4046: "No need to change margin type."
            if "-4046" in str(e.response.text):
                return {"msg": "already-set"}
            raise

    async def place_order(
        self,
        api_key: str,
        api_secret: str,
        **params: Any,
    ) -> dict:
        """Create an order. Pass Binance API param keys directly.

        See: https://binance-docs.github.io/apidocs/futures/en/#new-order-trade
        """
        return await self._signed_request(
            "POST", "/fapi/v1/order", api_key, api_secret, params
        )

    async def cancel_order(
        self,
        symbol: str,
        order_id: str | int,
        api_key: str,
        api_secret: str,
    ) -> dict:
        return await self._signed_request(
            "DELETE",
            "/fapi/v1/order",
            api_key,
            api_secret,
            {"symbol": symbol.upper(), "orderId": order_id},
        )

    async def cancel_all_orders(
        self, symbol: str, api_key: str, api_secret: str
    ) -> dict:
        return await self._signed_request(
            "DELETE",
            "/fapi/v1/allOpenOrders",
            api_key,
            api_secret,
            {"symbol": symbol.upper()},
        )

    async def get_open_orders(
        self, symbol: str, api_key: str, api_secret: str
    ) -> list[dict]:
        return await self._signed_request(
            "GET",
            "/fapi/v1/openOrders",
            api_key,
            api_secret,
            {"symbol": symbol.upper()},
        )

    async def get_position(
        self, symbol: str, api_key: str, api_secret: str
    ) -> dict | None:
        rows = await self._signed_request(
            "GET",
            "/fapi/v2/positionRisk",
            api_key,
            api_secret,
            {"symbol": symbol.upper()},
        )
        for row in rows:
            if row.get("symbol") == symbol.upper():
                return row
        return None
