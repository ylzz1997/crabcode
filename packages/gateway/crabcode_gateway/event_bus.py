"""Event bus for SSE / WebSocket event distribution.

Inspired by OpenCode's event.ts + AsyncQueue pattern.  Supports
multiple subscribers per session with 10 s heartbeat keep-alive.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

from crabcode_core.logging_utils import get_logger
from crabcode_gateway.schemas import (
    ServerConnectedPayload,
    ServerHeartbeatPayload,
    core_event_to_payload,
)

logger = get_logger(__name__)

_HEARTBEAT_INTERVAL = 10  # seconds


class _Subscriber:
    """A single SSE / WS subscriber backed by an async queue."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._closed = False

    async def push(self, data: str) -> None:
        if self._closed:
            return
        await self._queue.put(data)

    def push_nowait(self, data: str) -> None:
        if self._closed:
            return
        try:
            self._queue.put_nowait(data)
        except asyncio.QueueFull:
            pass

    async def next(self) -> str | None:
        return await self._queue.get()

    def close(self) -> None:
        self._closed = True
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            pass


class EventBus:
    """Session-scoped event bus with multi-subscriber broadcast.

    All CoreEvents for a session are published here and fanned out
    to every connected subscriber (CLI, VSCode, web UI, etc.).
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[_Subscriber]] = {}
        self._global_subscribers: list[_Subscriber] = []

    # ── Subscribe ────────────────────────────────────────────────

    def subscribe(self, session_id: str | None = None) -> _Subscriber:
        """Subscribe to events for a specific session (or all sessions)."""
        sub = _Subscriber(session_id or "__global__")

        if session_id is None:
            self._global_subscribers.append(sub)
        else:
            self._subscribers.setdefault(session_id, []).append(sub)

        # Send connected event immediately
        connected = ServerConnectedPayload().model_dump_json()
        sub.push_nowait(connected)

        return sub

    def unsubscribe(self, sub: _Subscriber) -> None:
        """Remove a subscriber and close its queue."""
        sid = sub.session_id
        if sid == "__global__":
            if sub in self._global_subscribers:
                self._global_subscribers.remove(sub)
        else:
            subs = self._subscribers.get(sid, [])
            if sub in subs:
                subs.remove(sub)
            if not subs:
                self._subscribers.pop(sid, None)
        sub.close()

    # ── Publish ──────────────────────────────────────────────────

    async def publish(self, session_id: str, event: Any) -> None:
        """Publish a CoreEvent to all subscribers of the session."""
        payload = core_event_to_payload(event)
        data = payload.model_dump_json()

        targets = list(self._subscribers.get(session_id, []))
        targets.extend(self._global_subscribers)

        for sub in targets:
            await sub.push(data)

    def publish_nowait(self, session_id: str, event: Any) -> None:
        """Non-async publish (for use from sync contexts)."""
        payload = core_event_to_payload(event)
        data = payload.model_dump_json()

        targets = list(self._subscribers.get(session_id, []))
        targets.extend(self._global_subscribers)

        for sub in targets:
            sub.push_nowait(data)

    # ── SSE stream helper ────────────────────────────────────────

    async def sse_stream(self, session_id: str | None = None) -> AsyncIterator[str]:
        """Yield SSE-formatted event strings with heartbeat.

        Usage with sse-starlette::

            return EventSourceResponse(event_bus.sse_stream(session_id))
        """
        sub = self.subscribe(session_id)
        try:
            while True:
                # Wait for event or heartbeat timeout
                try:
                    data = await asyncio.wait_for(
                        sub.next(),
                        timeout=_HEARTBEAT_INTERVAL,
                    )
                except asyncio.TimeoutError:
                    heartbeat = ServerHeartbeatPayload().model_dump_json()
                    yield heartbeat
                    continue

                if data is None:
                    return
                yield data
        finally:
            self.unsubscribe(sub)

    # ── WebSocket helper ─────────────────────────────────────────

    async def ws_stream(self, ws: Any, session_id: str | None = None) -> None:
        """Push events to a WebSocket connection.

        The caller should run this in a background task and cancel
        it when the WebSocket disconnects.
        """
        sub = self.subscribe(session_id)
        try:
            while True:
                data = await asyncio.wait_for(
                    sub.next(),
                    timeout=_HEARTBEAT_INTERVAL,
                )
                if data is None:
                    return
                if isinstance(data, str):
                    await ws.send_text(data)
                else:
                    await ws.send_text(str(data))
        except asyncio.CancelledError:
            pass
        finally:
            self.unsubscribe(sub)
