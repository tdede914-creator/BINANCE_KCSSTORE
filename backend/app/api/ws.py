"""WebSocket endpoint for real-time frontend updates.

The scanner engine calls :func:`event_bus.publish` when a new signal is
generated or a trade state changes. This router fans that out to every
connected browser client.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.logging import get_logger

log = get_logger(__name__)

router = APIRouter()


class EventBus:
    """In-process pub-sub. Simple asyncio.Queue-per-subscriber fanout."""

    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    async def publish(self, event: dict[str, Any]) -> None:
        # Fire and forget — drop for slow subscribers rather than block.
        for q in list(self._subs):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                log.warning("ws.subscriber_slow_dropping")


# Global singleton used by scanner + this router
event_bus = EventBus()


@router.websocket("/events")
async def events_ws(ws: WebSocket) -> None:
    await ws.accept()
    log.info("ws.client_connected")
    q = event_bus.subscribe()

    async def receiver() -> None:
        """Consume client messages to detect disconnects. We don't act on them."""
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass

    recv_task = asyncio.create_task(receiver())

    try:
        # Send a hello event
        await ws.send_text(json.dumps({"type": "hello"}))
        while True:
            event = await q.get()
            await ws.send_text(json.dumps(event, default=str))
    except WebSocketDisconnect:
        pass
    except Exception as e:  # noqa: BLE001
        log.warning("ws.push_error", error=str(e))
    finally:
        event_bus.unsubscribe(q)
        recv_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await recv_task
        log.info("ws.client_disconnected")
