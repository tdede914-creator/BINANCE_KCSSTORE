"""Market-data endpoints — proxy for Binance public klines.

Why proxy?
- Keeps the frontend loosely coupled from Binance URL structure.
- Lets us respect our BINANCE_TESTNET flag consistently.
- Allows us to add caching / rate-limiting later without touching the UI.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.binance.rest import VALID_TIMEFRAMES
from app.datasource.factory import get_data_source
from app.strategy.channel import compute_channel
from app.strategy.indicators import find_swings, sr_zones

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
    try:
        source = await get_data_source()
    except RuntimeError as e:
        raise HTTPException(400, str(e)) from e
    async with source:
        try:
            df = await source.get_klines(sym, interval, limit=limit)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"market data error: {e}") from e

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
    try:
        source = await get_data_source()
    except RuntimeError as e:
        raise HTTPException(400, str(e)) from e
    async with source:
        try:
            price = await source.get_ticker_price(symbol.upper())
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"market data error: {e}") from e
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
    algorithm: str  # "pivot" | "regression" — which one produced this result


@router.get("/channel", response_model=ChannelResponse)
async def get_channel(
    symbol: str = Query(..., min_length=3, max_length=20),
    interval: str = Query("1h"),
    lookback: int = Query(100, ge=20, le=500),
    algorithm: str = Query("pivot", regex="^(pivot|regression)$"),
) -> ChannelResponse:
    """Return an auto-computed parallel channel for the given symbol/interval.

    ``algorithm='pivot'`` (default) draws the channel through actual swing
    higher-lows / lower-highs so the edges pass exactly through pivot
    candles. Falls back to ``regression`` automatically when there are
    not enough swings in the lookback window.
    """
    if interval not in VALID_TIMEFRAMES:
        raise HTTPException(
            400,
            f"invalid interval; valid: {', '.join(VALID_TIMEFRAMES)}",
        )
    sym = symbol.upper()

    # Fetch a bit more than lookback so the regression has stable indexing.
    fetch_limit = min(lookback + 50, 1500)

    try:
        source = await get_data_source()
    except RuntimeError as e:
        raise HTTPException(400, str(e)) from e
    async with source:
        try:
            df = await source.get_klines(sym, interval, limit=fetch_limit)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"market data error: {e}") from e

    result = compute_channel(df, lookback=lookback, prefer=algorithm)
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
        algorithm=result.algorithm,
    )



# --------------------------------------------------------------------------
# Support / Resistance levels
# --------------------------------------------------------------------------


class SRLevelDTO(BaseModel):
    price: float
    kind: str                # "support" | "resistance"
    touches: int             # how many swings clustered at this price
    last_touch_time: int     # unix seconds


class SRResponse(BaseModel):
    symbol: str
    interval: str
    lookback: int
    current_price: float          # last close — used by the chart for S/R numbering
    levels: list[SRLevelDTO]


@router.get("/sr", response_model=SRResponse)
async def get_sr(
    symbol: str = Query(..., min_length=3, max_length=20),
    interval: str = Query("1h"),
    lookback: int = Query(300, ge=50, le=1000),
    max_levels: int = Query(10, ge=1, le=30),
    min_touches: int = Query(2, ge=2, le=10),
    cluster_pct: float = Query(0.003, ge=0.0005, le=0.02),
) -> SRResponse:
    """Return the most relevant horizontal S/R levels for a symbol.

    Uses the same detector the strategy engine uses (fractal swings +
    proximity clustering). Levels are ranked by number of touches, and
    the ``max_levels`` most-relevant zones are returned, capping the
    number of horizontal lines drawn on the chart.
    """
    if interval not in VALID_TIMEFRAMES:
        raise HTTPException(
            400,
            f"invalid interval; valid: {', '.join(VALID_TIMEFRAMES)}",
        )
    sym = symbol.upper()

    try:
        source = await get_data_source()
    except RuntimeError as e:
        raise HTTPException(400, str(e)) from e
    async with source:
        try:
            df = await source.get_klines(sym, interval, limit=lookback)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"market data error: {e}") from e

    if len(df) < 20:
        return SRResponse(
            symbol=sym,
            interval=interval,
            lookback=len(df),
            current_price=float(df["close"].iloc[-1]) if len(df) else 0.0,
            levels=[],
        )

    current_price = float(df["close"].iloc[-1])

    swings = find_swings(df, left=3, right=3)
    zones = sr_zones(
        df,
        swings,
        cluster_pct=cluster_pct,
        min_touches=min_touches,
    )

    # Rank: more touches first, tie-break by most recent last_touch_index.
    zones.sort(key=lambda z: (-z.touches, -z.last_touch_index))
    zones = zones[:max_levels]

    levels: list[SRLevelDTO] = []
    for z in zones:
        # Guard against index just outside df (e.g. if strategy grew it).
        idx = min(max(z.last_touch_index, 0), len(df) - 1)
        levels.append(
            SRLevelDTO(
                price=float(z.price),
                kind=z.kind,
                touches=int(z.touches),
                last_touch_time=int(df.index[idx].timestamp()),
            )
        )

    return SRResponse(
        symbol=sym,
        interval=interval,
        lookback=len(df),
        current_price=current_price,
        levels=levels,
    )
