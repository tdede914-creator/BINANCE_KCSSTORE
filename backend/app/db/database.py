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


# --------------------------------------------------------------------------
# Lightweight in-place schema evolution.
#
# We don't run Alembic in this project (yet) — instead of writing full
# migrations for every additive column, ``init_db`` also runs a small
# ``ADD COLUMN IF NOT EXISTS`` step after ``create_all``. That covers
# the 95% case (adding a new tunable to UserConfig) without breaking
# existing deployments that already have data.
#
# Both Postgres 9.6+ and SQLite 3.35+ support ``ADD COLUMN IF NOT EXISTS``,
# but SQLite's syntax lacks the ``IF NOT EXISTS`` keyword; we fall back
# to inspecting the current columns and skipping the ALTER when the
# column already exists, which works on both dialects.
# --------------------------------------------------------------------------

# (table_name, column_name, ddl_type_and_default)
_ADDITIVE_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("user_config", "adx_period", "INTEGER NOT NULL DEFAULT 14"),
    ("user_config", "adx_min", "DOUBLE PRECISION NOT NULL DEFAULT 20.0"),
    ("user_config", "volume_mult", "DOUBLE PRECISION NOT NULL DEFAULT 1.2"),
)


def _apply_additive_columns_sync(sync_conn) -> None:
    """Run ADD COLUMN for any columns present in the model but absent in DB."""
    from sqlalchemy import inspect, text

    inspector = inspect(sync_conn)
    dialect = sync_conn.dialect.name  # "postgresql", "sqlite", ...

    for table, column, ddl in _ADDITIVE_COLUMNS:
        # Skip if the table itself doesn't exist yet (fresh DB — create_all
        # will handle everything).
        if not inspector.has_table(table):
            continue
        existing = {c["name"] for c in inspector.get_columns(table)}
        if column in existing:
            continue

        # SQLite doesn't support DOUBLE PRECISION — normalize.
        col_ddl = ddl
        if dialect == "sqlite":
            col_ddl = col_ddl.replace("DOUBLE PRECISION", "REAL")

        log.info("db.migrate.add_column", table=table, column=column)
        sync_conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {col_ddl}"))


async def init_db() -> None:
    """Create tables if they don't exist and apply any additive column migrations."""
    # Import models so SQLModel.metadata knows about them.
    from app.db import models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
        await conn.run_sync(_apply_additive_columns_sync)
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
