"""Async database engine + session factory."""
from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

# Ensure ./data dir exists for SQLite
if settings.DATABASE_URL.startswith("sqlite"):
    os.makedirs("./data", exist_ok=True)

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    future=True,
    pool_pre_ping=True,
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def init_db() -> None:
    """Create tables if they don't exist. Called once at app startup."""
    # Import models so SQLModel.metadata knows about them.
    from app.db import models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    log.info("db.initialized", url=settings.DATABASE_URL.split("@")[-1])


@asynccontextmanager
async def session_scope() -> AsyncGenerator[AsyncSession, None]:
    """Context-manager style session for background tasks (scanner, executor)."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency for request-scoped sessions."""
    async with AsyncSessionLocal() as session:
        yield session
