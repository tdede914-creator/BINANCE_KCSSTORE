"""Binance USDT-M Futures WebSocket client for real-time kline streams.

We use kline streams (not book ticker) because signal evaluation triggers on
candle close events. The stream sends a candle update on every tick with a
flag `x=True` when the candle is finalized.

Usage
-----
    async def on_candle(kline: dict) -> None:
        # kline: {symbol, interval, open, high, low, close, volume,
        #        open_time, close_time, is_closed}
        ...

    ws = BinanceKlineStream(
        symbols=["BTCUSDT", "ETHUSDT"],
        interval="5m",
        on_candle=on_candle,
        closed_only=True,
    )
    await ws.run_forever()
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import websockets

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

OnCandleCallback = Callable[[dict[str, Any]], Awaitable[None]]


class BinanceKlineStream:
    """Multiplexed kline subscription for one interval, many symbols."""

    def __init__(
        self,
        symbols: list[str],
        interval: str,
        on_candle: OnCandleCallback,
        *,
        closed_only: bool = True,
        base_ws_url: str | None = None,
    ) -> None:
        self.symbols = [s.lower() for s in symbols]
        self.interval = interval
        self.on_candle = on_candle
        self.closed_only = closed_only
        self.base_ws_url = base_ws_url or settings.binance_ws_url
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task | None = None

    @property
    def stream_url(self) -> str:
        streams = "/".join(f"{s}@kline_{self.interval}" for s in self.symbols)
        return f"{self.base_ws_url}/stream?streams={streams}"

    async def run_forever(self) -> None:
        """Connect + auto-reconnect with exponential backoff."""
        backoff = 1.0
        while not self._stop_event.is_set():
            try:
                log.info(
                    "ws.connecting",
                    symbols=self.symbols,
                    interval=self.interval,
                )
                async with websockets.connect(
                    self.stream_url,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=10,
                ) as ws:
                    log.info("ws.connected", interval=self.interval)
                    backoff = 1.0
                    async for raw in ws:
                        if self._stop_event.is_set():
                            break
                        await self._handle_message(raw)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "ws.disconnected",
                    error=str(e),
                    reconnect_in=backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    def stop(self) -> None:
        self._stop_event.set()

    async def _handle_message(self, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return

        data = payload.get("data") or payload
        if data.get("e") != "kline":
            return

        k = data["k"]
        is_closed = bool(k.get("x", False))
        if self.closed_only and not is_closed:
            return

        candle = {
            "symbol": k["s"],
            "interval": k["i"],
            "open_time": int(k["t"]),
            "close_time": int(k["T"]),
            "open": float(k["o"]),
            "high": float(k["h"]),
            "low": float(k["l"]),
            "close": float(k["c"]),
            "volume": float(k["v"]),
            "is_closed": is_closed,
        }
        try:
            await self.on_candle(candle)
        except Exception as e:  # noqa: BLE001
            log.error("ws.callback_error", error=str(e), symbol=k["s"])
