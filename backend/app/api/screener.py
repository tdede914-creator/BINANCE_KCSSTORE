"""Meme-coin screener.

**Not a pump predictor.** Nothing can predict a meme-coin pump — those
are driven by social virality, whale coordination and exchange
listings, none of which have a technical signature you can read off a
chart. What this screener DOES is combine a handful of measurable
metrics that empirically correlate with the pre-pump phase of past
big movers, and rank Binance USDT-M perpetual meme coins by a single
composite score. Users then decide whether to add the top candidates
to the strategy scanner's watchlist.

Metrics per symbol
------------------
- ``vol_ratio``       — 24h volume / rolling 7d average volume. Values
                        >1.5 mark "volume above usual", >3.0 is a spike.
- ``price_change_24h``— % change last 24h. Positive momentum feeds the score.
- ``price_change_7d`` — % change last 7d. Weekly trend.
- ``squeeze_ratio``   — recent ATR / ATR MA(50). Below 0.8 = volatility
                        compression, often preceding a breakout.
- ``open_interest``   — value of open interest (informational, not
                        scored — not all Binance perps expose OI freely).
- ``funding_rate``    — current funding. Very negative funding while
                        price is flat = squeezed shorts, common pre-pump.

Composite score
---------------
    0.40 × vol_ratio_norm
  + 0.20 × momentum_norm(24h)
  + 0.15 × momentum_norm(7d)
  + 0.15 × (1 - squeeze_ratio)   [tighter squeeze = higher]
  + 0.10 × funding_signal        [very negative funding adds a point]

Normalisations clip to [0, 1]. Missing data collapses gracefully to 0.

Endpoint
--------
GET /api/screener/memecoins?limit=20

Returns the ranked list. Sequential per-symbol fetch (Binance rate
limits are generous but not infinite). Total call time for the 25
default meme perpetuals is 5-15 seconds.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.binance.rest import BinanceREST
from app.core.logging import get_logger
from app.strategy.indicators import atr as atr_series

router = APIRouter()
log = get_logger(__name__)


# --------------------------------------------------------------------------
# Meme universe — Binance USDT-M perpetuals commonly classed as memes.
# We keep this list explicit rather than autodetecting by market-cap so
# the user can trust which coins get screened. Add / remove via PR.
# --------------------------------------------------------------------------
DEFAULT_MEMES: tuple[str, ...] = (
    "1000PEPEUSDT",
    "1000SHIBUSDT",
    "1000BONKUSDT",
    "1000FLOKIUSDT",
    "DOGEUSDT",
    "WIFUSDT",
    "BOMEUSDT",
    "MEMEUSDT",
    "POPCATUSDT",
    "MYROUSDT",
    "BRETTUSDT",
    "DEGENUSDT",
    "PNUTUSDT",
    "NEIROUSDT",
    "1000RATSUSDT",
    "TURBOUSDT",
    "1000LUNCUSDT",
    "PEOPLEUSDT",
    "TRUMPUSDT",
    "AI16ZUSDT",
    "MOODENGUSDT",
    "GOATUSDT",
    "FARTCOINUSDT",
    "CHILLGUYUSDT",
    "SPXUSDT",
)


class MemeScreenerRow(BaseModel):
    symbol: str
    price: float
    price_change_24h_pct: float
    price_change_7d_pct: float
    volume_24h_usdt: float
    vol_ratio: float             # 24h vs 7d-avg
    squeeze_ratio: float         # 1.0 = normal, <0.8 = compressed
    funding_rate_pct: float
    score: float                 # 0..1 composite — higher = better candidate
    reason: str                  # short human-readable summary of what drove the score
    error: str | None = None


class MemeScreenerResponse(BaseModel):
    generated_at: datetime
    disclaimer: str
    rows: list[MemeScreenerRow]


@router.get("/memecoins", response_model=MemeScreenerResponse)
async def memecoin_screener(
    limit: int = Query(25, ge=1, le=50),
) -> MemeScreenerResponse:
    symbols = list(DEFAULT_MEMES)[:limit]
    rows: list[MemeScreenerRow] = []
    async with BinanceREST() as rest:
        for sym in symbols:
            try:
                row = await _score_symbol(rest, sym)
            except Exception as e:  # noqa: BLE001
                log.warning("screener.symbol_failed", symbol=sym, error=str(e))
                row = MemeScreenerRow(
                    symbol=sym, price=0.0, price_change_24h_pct=0.0,
                    price_change_7d_pct=0.0, volume_24h_usdt=0.0,
                    vol_ratio=0.0, squeeze_ratio=1.0, funding_rate_pct=0.0,
                    score=0.0, reason="fetch failed", error=str(e),
                )
            rows.append(row)

    rows.sort(key=lambda r: r.score, reverse=True)
    return MemeScreenerResponse(
        generated_at=datetime.now(tz=timezone.utc),
        disclaimer=(
            "Ranking is derived from measurable metrics (volume spikes, "
            "squeeze, momentum, funding). It is NOT a pump prediction. "
            "Meme-coin moves are driven by non-technical factors and "
            "carry high risk of total loss."
        ),
        rows=rows,
    )


# --------------------------------------------------------------------------
# Per-symbol scoring
# --------------------------------------------------------------------------


async def _score_symbol(rest: BinanceREST, symbol: str) -> MemeScreenerRow:
    # 24h ticker + funding + klines in parallel — cheap network wins.
    ticker_task = rest.get_24h_ticker(symbol)
    kline_task = rest.get_klines(symbol, "1h", limit=200)
    funding_task = _safe_funding(rest, symbol)

    ticker, df, funding_rate = await asyncio.gather(
        ticker_task, kline_task, funding_task
    )

    price = float(ticker.get("lastPrice") or 0.0)
    change_24h = float(ticker.get("priceChangePercent") or 0.0)
    vol_24h = float(ticker.get("quoteVolume") or 0.0)

    if len(df) < 50:
        raise ValueError(f"not enough kline history ({len(df)})")
    # Approximate USDT volume per bar from base volume × close. Exact
    # for USDT-quoted perpetuals which is what the meme list is.
    if "quote_volume" not in df.columns:
        df = df.copy()
        df["quote_volume"] = df["volume"] * df["close"]

    # ---- Volume ratio: last 24h vs the median 24h over last 7 days
    last_24h_vol = df["quote_volume"].tail(24).sum()
    daily_totals = [
        df["quote_volume"].iloc[-24 * (i + 1): -24 * i or None].sum()
        for i in range(1, 8)
    ]
    baseline = sum(daily_totals) / max(len(daily_totals), 1)
    vol_ratio = (last_24h_vol / baseline) if baseline > 0 else 0.0

    # ---- Squeeze ratio: current ATR / ATR MA(50)
    atr = atr_series(df, period=14)
    atr_now = float(atr.iloc[-1]) if not atr.empty else 0.0
    atr_ma = float(atr.tail(50).mean()) if not atr.empty else 0.0
    squeeze = (atr_now / atr_ma) if atr_ma > 0 else 1.0

    # ---- 7d momentum
    if len(df) >= 24 * 7 and df["close"].iloc[-24 * 7] > 0:
        change_7d = (
            (df["close"].iloc[-1] - df["close"].iloc[-24 * 7])
            / df["close"].iloc[-24 * 7]
            * 100.0
        )
    else:
        change_7d = 0.0

    # ---- Score composition (all clipped to [0, 1])
    vol_score = min(max((vol_ratio - 1.0) / 2.0, 0.0), 1.0)    # ratio 3 = full point
    mom_24h = min(max(change_24h / 30.0, 0.0), 1.0)             # +30% = full point
    mom_7d = min(max(change_7d / 60.0, 0.0), 1.0)               # +60% = full point
    squeeze_score = min(max(1.0 - squeeze, 0.0), 1.0)           # tighter = higher
    fund_score = 0.0
    if funding_rate < -0.0005:
        fund_score = min(abs(funding_rate) * 200, 1.0)         # -0.5% = full point

    score = (
        0.40 * vol_score
        + 0.20 * mom_24h
        + 0.15 * mom_7d
        + 0.15 * squeeze_score
        + 0.10 * fund_score
    )
    score = round(min(max(score, 0.0), 1.0), 3)

    # Build a short reason summary — first 2-3 dominant contributors.
    contributions = sorted(
        [
            ("volume", 0.40 * vol_score, f"{vol_ratio:.1f}× avg"),
            ("mom24h", 0.20 * mom_24h, f"{change_24h:+.1f}% 24h"),
            ("mom7d",  0.15 * mom_7d, f"{change_7d:+.1f}% 7d"),
            ("squeeze", 0.15 * squeeze_score, f"squeeze {squeeze:.2f}"),
            ("funding", 0.10 * fund_score, f"funding {funding_rate * 100:+.3f}%"),
        ],
        key=lambda t: t[1], reverse=True,
    )
    reason = ", ".join(f"{c[2]}" for c in contributions[:3] if c[1] > 0.05) or "no strong signal"

    return MemeScreenerRow(
        symbol=symbol,
        price=price,
        price_change_24h_pct=round(change_24h, 2),
        price_change_7d_pct=round(change_7d, 2),
        volume_24h_usdt=round(vol_24h, 0),
        vol_ratio=round(vol_ratio, 2),
        squeeze_ratio=round(squeeze, 2),
        funding_rate_pct=round(funding_rate * 100, 4),
        score=score,
        reason=reason,
    )


async def _safe_funding(rest: BinanceREST, symbol: str) -> float:
    try:
        info = await rest.get_funding_rate(symbol)
        return float(info.get("lastFundingRate", 0.0)) if info else 0.0
    except Exception:
        return 0.0



