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
from app.strategy.channel import compute_regression_channel

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


# --------------------------------------------------------------------------
# Regression channel (auto parallel channel)
# --------------------------------------------------------------------------


class _ChannelPointDTO(BaseModel):
    time: int
    price: float


class _ChannelLineDTO(BaseModel):
    start: _ChannelPointDTO
    end: _ChannelPointDTO


class ChannelResponse(BaseModel):
    symbol: str
    interval: str
    lookback: int
    upper: _ChannelLineDTO
    midline: _ChannelLineDTO
    lower: _ChannelLineDTO
    slope_per_bar: float
    slope_pct_total: float
    stddev: float
    width_pct: float


@router.get("/channel", response_model=ChannelResponse)
async def get_channel(
    symbol: str = Query(..., min_length=3, max_length=20),
    interval: str = Query("1h"),
    lookback: int = Query(100, ge=20, le=500),
) -> ChannelResponse:
    """Return an auto-computed parallel channel for the given symbol/interval.

    The bands touch the two most extreme candles in the lookback window, so
    visually it matches the classic manual trendline-and-parallel drawing.
    """
    if interval not in VALID_TIMEFRAMES:
        raise HTTPException(
            400,
            f"invalid interval; valid: {', '.join(VALID_TIMEFRAMES)}",
        )
    sym = symbol.upper()

    # Fetch a bit more than lookback so the regression has stable indexing.
    fetch_limit = min(lookback + 50, 1500)

    async with BinanceREST() as rest:
        try:
            df = await rest.get_klines(sym, interval, limit=fetch_limit)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"binance error: {e}") from e

    result = compute_regression_channel(df, lookback=lookback)
    if result is None:
        raise HTTPException(400, "not enough data to compute channel")

    def _line_dto(line) -> _ChannelLineDTO:  # noqa: ANN001
        return _ChannelLineDTO(
            start=_ChannelPointDTO(time=line.start.time, price=line.start.price),
            end=_ChannelPointDTO(time=line.end.time, price=line.end.price),
        )

    return ChannelResponse(
        symbol=sym,
        interval=interval,
        lookback=result.lookback,
        upper=_line_dto(result.upper),
        midline=_line_dto(result.midline),
        lower=_line_dto(result.lower),
        slope_per_bar=result.slope_per_bar,
        slope_pct_total=result.slope_pct_total,
        stddev=result.stddev,
        width_pct=result.width_pct,
    )
