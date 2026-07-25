"""Application settings loaded from environment variables."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-driven settings.

    Values are loaded from environment variables (or a `.env` file at project root
    when running outside Docker).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # --- Application ---
    APP_ENV: Literal["development", "production"] = "development"
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    LOG_LEVEL: str = "INFO"
    FRONTEND_ORIGIN: str = "http://localhost:3000"

    # --- Security ---
    ENCRYPTION_KEY: str = Field(
        default="",
        description="Fernet key for encrypting Binance API secrets at rest.",
    )
    JWT_SECRET: str = Field(default="change-me-in-prod")
    JWT_EXPIRES_MINUTES: int = 60 * 24

    # --- Database ---
    DATABASE_URL: str = "sqlite+aiosqlite:///./data/app.db"

    # --- Redis ---
    REDIS_URL: str = "redis://redis:6379/0"

    # --- Binance ---
    BINANCE_TESTNET: bool = True
    BINANCE_FUTURES_REST_MAINNET: str = "https://fapi.binance.com"
    BINANCE_FUTURES_WS_MAINNET: str = "wss://fstream.binance.com"
    BINANCE_FUTURES_REST_TESTNET: str = "https://testnet.binancefuture.com"
    BINANCE_FUTURES_WS_TESTNET: str = "wss://stream.binancefuture.com"

    # --- Bot defaults ---
    DEFAULT_TRADING_MODE: Literal["paper", "live"] = "paper"
    PAPER_START_BALANCE: float = 1000.0
    RISK_PER_TRADE_PCT: float = 1.0
    DEFAULT_LEVERAGE: int = 5
    DEFAULT_WATCHLIST: str = (
        "ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,DOGEUSDT,"
        "ADAUSDT,AVAXUSDT,LINKUSDT,SUIUSDT,1000PEPEUSDT"
    )

    # Multi-timeframe defaults (user-editable in UI)
    BIAS_TIMEFRAME: str = "4h"
    SETUP_TIMEFRAME: str = "1h"
    ENTRY_TIMEFRAME: str = "5m"

    SCAN_INTERVAL_SECONDS: int = 15

    @field_validator("ENCRYPTION_KEY")
    @classmethod
    def _validate_key(cls, v: str) -> str:
        # Empty allowed in dev — we auto-generate at runtime in security.py.
        return v

    # --- Derived ---
    @property
    def binance_rest_url(self) -> str:
        return (
            self.BINANCE_FUTURES_REST_TESTNET
            if self.BINANCE_TESTNET
            else self.BINANCE_FUTURES_REST_MAINNET
        )

    @property
    def binance_ws_url(self) -> str:
        return (
            self.BINANCE_FUTURES_WS_TESTNET
            if self.BINANCE_TESTNET
            else self.BINANCE_FUTURES_WS_MAINNET
        )

    @property
    def watchlist(self) -> list[str]:
        return [s.strip().upper() for s in self.DEFAULT_WATCHLIST.split(",") if s.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
