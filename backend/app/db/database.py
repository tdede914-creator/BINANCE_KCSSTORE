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
    ("user_config", "rr_tp3", "DOUBLE PRECISION NOT NULL DEFAULT 4.0"),
    ("user_config", "adx_min", "DOUBLE PRECISION NOT NULL DEFAULT 20.0"),
    ("user_config", "volume_mult", "DOUBLE PRECISION NOT NULL DEFAULT 1.2"),
    # Multi-strategy toggles + Range Breakout params.
    ("user_config", "mtf_confluence_enabled", "BOOLEAN NOT NULL DEFAULT TRUE"),
    ("user_config", "range_breakout_enabled", "BOOLEAN NOT NULL DEFAULT TRUE"),
    ("user_config", "rb_lookback", "INTEGER NOT NULL DEFAULT 30"),
    ("user_config", "rb_max_range_pct", "DOUBLE PRECISION NOT NULL DEFAULT 3.0"),
    ("user_config", "rb_atr_squeeze_ratio", "DOUBLE PRECISION NOT NULL DEFAULT 0.7"),
    ("user_config", "rb_breakout_buffer", "DOUBLE PRECISION NOT NULL DEFAULT 0.1"),
    ("user_config", "rb_measured_move_tp1", "DOUBLE PRECISION NOT NULL DEFAULT 1.0"),
    ("user_config", "rb_measured_move_tp2", "DOUBLE PRECISION NOT NULL DEFAULT 1.5"),
    # Signal.strategy — used to filter historical signals per strategy.
    ("signals", "strategy", "VARCHAR NOT NULL DEFAULT 'mtf_confluence'"),
    # Optional third profit target — nullable so old rows stay valid.
    ("signals", "take_profit_3", "DOUBLE PRECISION"),
    # Telegram notifier — bot token (encrypted), chat id, per-channel toggles.
    ("user_config", "telegram_enabled", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("user_config", "telegram_bot_token_enc", "VARCHAR NOT NULL DEFAULT ''"),
    ("user_config", "telegram_chat_id", "VARCHAR NOT NULL DEFAULT ''"),
    ("user_config", "telegram_notify_signals", "BOOLEAN NOT NULL DEFAULT TRUE"),
    ("user_config", "telegram_notify_trades", "BOOLEAN NOT NULL DEFAULT TRUE"),
    ("user_config", "telegram_notify_hourly_balance", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("user_config", "telegram_balance_interval_min", "INTEGER NOT NULL DEFAULT 60"),
    # Head-start window before LIVE auto-execution kicks in.
    ("user_config", "signal_execute_delay_seconds", "INTEGER NOT NULL DEFAULT 0"),
    # Signals-only master switch: fire alerts, never execute.
    ("user_config", "signal_only_mode", "BOOLEAN NOT NULL DEFAULT FALSE"),
    # MT5 bridge (Windows-side executor for forex).
    ("user_config", "mt5_bridge_secret", "VARCHAR NOT NULL DEFAULT ''"),
    ("user_config", "mt5_bridge_last_heartbeat", "TIMESTAMP WITH TIME ZONE"),
    ("signals", "mt5_ticket", "VARCHAR"),
    ("signals", "mt5_fill_price", "DOUBLE PRECISION"),
    ("signals", "mt5_lot", "DOUBLE PRECISION"),
    ("signals", "mt5_error", "VARCHAR"),
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
