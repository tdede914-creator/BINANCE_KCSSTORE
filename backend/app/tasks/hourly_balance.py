"""Periodic Telegram wallet-balance snapshot.

Runs as a single asyncio task started from ``main.lifespan``. Every N
minutes (configurable per user via ``telegram_balance_interval_min``),
it:

1. Reads the latest ``UserConfig``
2. Bails early if Telegram is disabled or the specific hourly-balance
   channel is off
3. Fetches a wallet snapshot with the same routine the ``/api/wallet/balance``
   endpoint uses (LIVE → Binance, PAPER → DB)
4. Sends a formatted message to the configured chat

Design notes
------------
- ONE task, not one per notification setting. Simplifies shutdown.
- Config is re-read every tick so users can enable / change interval
  without restarting the backend.
- The interval is honoured via a short poll loop (60s sleep) that
  checks if enough time has passed. This lets the task wake up quickly
  on shutdown and also handle the case where the user changes the
  interval from 60 → 15 mid-run.
- Failures are swallowed at INFO / WARNING level. This task should
  NEVER take down the app; a bad token or Binance outage should just
  drop that one notification.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlmodel import select

from app.api.wallet import _live_balance, _paper_balance
from app.core.logging import get_logger
from app.db.database import session_scope
from app.db.models import TradingMode, UserConfig
from app.telegram import notifier as tg_notifier

log = get_logger(__name__)


class HourlyBalanceReporter:
    """Sleep-and-check loop that pushes wallet snapshots to Telegram."""

    def __init__(self) -> None:
        self._stop = asyncio.Event()
        self._last_sent_at: datetime | None = None

    def stop(self) -> None:
        self._stop.set()

    async def run_forever(self) -> None:
        log.info("hourly_balance.started")
        # Small initial delay so we don't send a message before the DB
        # migrations have finished on cold start.
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=15)
            return  # someone signalled stop before we got going
        except asyncio.TimeoutError:
            pass

        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception as e:  # noqa: BLE001
                log.warning("hourly_balance.tick_failed", error=str(e))

            # Wake up every 60s to check if it's time to send (or if
            # shutdown was requested). We don't sleep the full interval
            # because the user may change telegram_balance_interval_min
            # in Settings and we want the change to take effect
            # promptly.
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=60)
                return
            except asyncio.TimeoutError:
                continue

    async def _tick(self) -> None:
        async with session_scope() as session:
            cfg_result = await session.execute(select(UserConfig).limit(1))
            cfg = cfg_result.scalars().first()
            if cfg is None:
                return

            if not cfg.telegram_enabled or not cfg.telegram_notify_hourly_balance:
                return
            if cfg.telegram_balance_interval_min <= 0:
                return

            # Rate-limit ourselves: only send if enough time elapsed.
            now = datetime.now(tz=timezone.utc)
            interval = timedelta(minutes=cfg.telegram_balance_interval_min)
            if self._last_sent_at is not None and now - self._last_sent_at < interval:
                return

            # Snapshot logic mirrors /api/wallet/balance.
            if cfg.trading_mode == TradingMode.LIVE:
                snap = await _live_balance(cfg)
            else:
                snap = await _paper_balance(session, cfg)

        text = tg_notifier.render_balance(
            mode=snap.mode.value if hasattr(snap.mode, "value") else str(snap.mode),
            wallet=snap.wallet_balance,
            available=snap.available_balance,
            locked=snap.locked_margin,
            unrealized=snap.unrealized_pnl,
            source=snap.source,
            error=snap.error,
        )
        ok = await tg_notifier.send_message(cfg, text)
        if ok:
            self._last_sent_at = now
            log.info(
                "hourly_balance.sent",
                mode=snap.mode.value if hasattr(snap.mode, "value") else str(snap.mode),
                wallet=snap.wallet_balance,
            )
        else:
            log.info("hourly_balance.send_failed_or_disabled")
