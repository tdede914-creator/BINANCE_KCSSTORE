"""FastAPI application entrypoint."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api import (
    auth,
    backtest as backtest_router,
    config as config_router,
    market,
    scanner as scanner_router,
    signals,
    trades,
    wallet as wallet_router,
    webhook as webhook_router,
    ws,
)
from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from app.db.database import init_db
from app.scanner.engine import ScannerEngine

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup + shutdown lifecycle."""
    configure_logging()
    log.info("app.startup", version=__version__, env=settings.APP_ENV)

    # Initialize DB (create tables if not exist)
    await init_db()

    # Start background scanner engine
    scanner = ScannerEngine()
    app.state.scanner = scanner
    scanner_task = asyncio.create_task(scanner.run_forever())

    try:
        yield
    finally:
        log.info("app.shutdown")
        scanner.stop()
        scanner_task.cancel()
        try:
            await scanner_task
        except asyncio.CancelledError:
            pass


def create_app() -> FastAPI:
    app = FastAPI(
        title="Binance Futures Signal Bot",
        version=__version__,
        description="Multi-Timeframe Confluence signal generator for Binance USDT-M Futures.",
        lifespan=lifespan,
    )

    # CORS
    # We accept any origin because this bot is a self-hosted single-user
    # app: users typically load the frontend from http://<vps-ip>:3000 or
    # http://localhost:3000 or their own domain, and none of those can be
    # known ahead of time. Since we do not rely on cookies (JWT is passed
    # as an Authorization header when auth is enabled), permissive CORS
    # does not open a real attack surface here.
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=".*",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Routers
    app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
    app.include_router(config_router.router, prefix="/api/config", tags=["config"])
    app.include_router(signals.router, prefix="/api/signals", tags=["signals"])
    app.include_router(trades.router, prefix="/api/trades", tags=["trades"])
    app.include_router(market.router, prefix="/api/market", tags=["market"])
    app.include_router(scanner_router.router, prefix="/api/scanner", tags=["scanner"])
    app.include_router(backtest_router.router, prefix="/api/backtest", tags=["backtest"])
    app.include_router(wallet_router.router, prefix="/api/wallet", tags=["wallet"])
    app.include_router(webhook_router.router, prefix="/api/webhook", tags=["webhook"])
    app.include_router(ws.router, prefix="/ws", tags=["ws"])

    @app.get("/", tags=["root"])
    async def root():
        return {
            "name": "binance-futures-signal-bot",
            "version": __version__,
            "status": "ok",
            "mode": settings.DEFAULT_TRADING_MODE,
            "testnet": settings.BINANCE_TESTNET,
        }

    @app.get("/health", tags=["root"])
    async def health():
        return {"status": "healthy"}

    return app


app = create_app()
