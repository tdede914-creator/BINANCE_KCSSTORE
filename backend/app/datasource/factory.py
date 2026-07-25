"""Factory to pick the correct market data source based on user config."""
from __future__ import annotations

import os

from app.core.logging import get_logger
from app.core.security import decrypt_secret
from app.datasource.base import MarketDataSource
from app.datasource.binance_source import BinanceDataSource
from app.datasource.twelvedata_source import TwelveDataSource
from app.db.database import session_scope
from app.db.models import MarketMode, UserConfig

log = get_logger(__name__)


async def get_data_source(mode: MarketMode | str | None = None) -> MarketDataSource:
    """Return a data source instance for the given market mode.

    - If ``mode`` is None, we read the current market_mode from the DB config.
    - Callers are responsible for calling ``await source.close()``, or using
      ``async with`` context management.
    """
    if mode is None:
        async with session_scope() as session:
            cfg = await session.get(UserConfig, 1)
            mode = cfg.market_mode if cfg else MarketMode.CRYPTO

    if isinstance(mode, str):
        mode = MarketMode(mode)

    if mode == MarketMode.CRYPTO:
        return BinanceDataSource()

    if mode == MarketMode.FOREX:
        api_key = os.getenv("TWELVEDATA_API_KEY", "")
        if not api_key:
            # Fall back to encrypted config value.
            async with session_scope() as session:
                cfg = await session.get(UserConfig, 1)
                if cfg and cfg.twelvedata_api_key_enc:
                    api_key = decrypt_secret(cfg.twelvedata_api_key_enc)
        if not api_key:
            raise RuntimeError(
                "TwelveData API key not configured. Set TWELVEDATA_API_KEY "
                "in .env or save it via /api/config/twelvedata-key."
            )
        return TwelveDataSource(api_key)

    raise ValueError(f"Unknown market mode: {mode}")
