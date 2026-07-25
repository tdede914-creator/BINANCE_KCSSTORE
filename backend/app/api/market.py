"""Market-data endpoints — proxy for Binance public klines.

Why proxy?
- Keeps the frontend loosely coupled from Binance URL structure.
- Lets us respect our BINANCE_TESTNET flag consistently.
- Allows us to add caching / rate-limiting later without touching the UI.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.binance.rest import VALID_TIMEFRAMES, BinanceREST

router = APIRouter()


class Candle(BaseModel):
    time: int      # unix seconds (lightweight-charts expects seconds, not ms)
    open: float
    high: float
    low: float
    close: float
    volume: float


class KlinesResponse(BaseModel):
    symbol: str
    interval: str
    candles: list[Candle]


@router.get("/klines", response_model=KlinesResponse)
async def get_klines(
    symbol: str = Query(..., min_length=3, max_length=20),
    interval: str = Query("5m"),
    limit: int = Query(500, ge=10, le=1500),
) -> KlinesResponse:
    if interval not in VALID_TIMEFRAMES:
        raise HTTPException(
            400,
            f"invalid interval; valid: {', '.join(VALID_TIMEFRAMES)}",
        )
    sym = symbol.upper()
    async with BinanceREST() as rest:
        try:
            df = await rest.get_klines(sym, interval, limit=limit)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"binance error: {e}") from e

    candles: list[Candle] = []
    for ts, row in df.iterrows():
        candles.append(
            Candle(
                time=int(ts.timestamp()),  # UTC seconds — lightweight-charts input
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["volume"]),
            )
        )

    return KlinesResponse(symbol=sym, interval=interval, candles=candles)


@router.get("/ticker", response_model=dict)
async def get_ticker(symbol: str = Query(..., min_length=3, max_length=20)) -> dict:
    async with BinanceREST() as rest:
        try:
            price = await rest.get_ticker_price(symbol.upper())
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"binance error: {e}") from e
    return {"symbol": symbol.upper(), "price": price}
