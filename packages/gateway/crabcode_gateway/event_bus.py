"""Event bus for SSE / WebSocket event distribution.

Inspired by OpenCode's event.ts + AsyncQueue pattern.  Supports
multiple subscribers per session with 10 s heartbeat keep-alive.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable

from crabcode_core.logging_utils import get_logger
from crabcode_gateway.schemas import (
    ServerConnectedPayload,
    ServerHeartbeatPayload,
    core_event_to_payload,
)

logger = get_logger(__name__)

_HEARTBEAT_INTERVAL = 10  # seconds
_DEFAULT_SUBSCRIBER_QUEUE_SIZE = 1024

_ORDINARY_EVENT = 0
_INTERACTIVE_CONTROL_EVENT = 1
_TERMINAL_CONTROL_EVENT = 2
_LIFECYCLE_CONTROL_EVENT = 3


def _event_priority(data: str | None) -> int:
    """Classify buffered payloads for bounded-queue eviction."""
    if data is None:
        return _LIFECYCLE_CONTROL_EVENT
    try:
        payload = json.loads(data)
    except (TypeError, json.JSONDecodeError):
        return _ORDINARY_EVENT
    if not isinstance(payload, dict):
        return _ORDINARY_EVENT

    event_type = payload.get("type")
    if event_type == "server.connected":
        return _LIFECYCLE_CONTROL_EVENT
    if event_type in {"permission_request", "choice_request"}:
        return _INTERACTIVE_CONTROL_EVENT
    # Only an explicit turn boundary is terminal.  Recoverable foreground
    # errors can be followed by retries or a later TurnCompleteEvent; treating
    # them as terminal here could evict the actual boundary from a full queue.
    if event_type == "turn_complete":
        return _TERMINAL_CONTROL_EVENT
    return _ORDINARY_EVENT


@dataclass(frozen=True)
class _BufferedEvent:
    data: str | None
    priority: int


class _BoundedEventQueue:
    """FIFO buffer with O(1) removal of the oldest item at a priority."""

    def __init__(self, maxsize: int) -> None:
        self.maxsize = maxsize
        self._next_id = 0
        self._items: OrderedDict[int, _BufferedEvent] = OrderedDict()
        self._ids_by_priority = tuple(
            deque() for _ in range(_LIFECYCLE_CONTROL_EVENT + 1)
        )
        self._not_empty = asyncio.Event()

    def qsize(self) -> int:
        return len(self._items)

    def empty(self) -> bool:
        return not self._items

    def full(self) -> bool:
        return len(self._items) >= self.maxsize

    def put_nowait(
        self,
        data: str | None,
        *,
        priority: int | None = None,
    ) -> None:
        if self.full():
            raise asyncio.QueueFull
        cached_priority = _event_priority(data) if priority is None else priority
        item_id = self._next_id
        self._next_id += 1
        self._items[item_id] = _BufferedEvent(data, cached_priority)
        self._ids_by_priority[cached_priority].append(item_id)
        self._not_empty.set()

    def get_nowait(self) -> str | None:
        if not self._items:
            raise asyncio.QueueEmpty
        item_id, item = self._items.popitem(last=False)
        priority_ids = self._ids_by_priority[item.priority]
        popped_id = priority_ids.popleft()
        assert popped_id == item_id
        if not self._items:
            self._not_empty.clear()
        return item.data

    async def get(self) -> str | None:
        while not self._items:
            self._not_empty.clear()
            await self._not_empty.wait()
        return self.get_nowait()

    def discard_oldest(self, max_priority: int) -> bool:
        """Discard the oldest item at the lowest available priority."""
        victim_priority = next(
            (
                priority
                for priority in range(max_priority + 1)
                if self._ids_by_priority[priority]
            ),
            None,
        )
        if victim_priority is None:
            return False
        item_id = self._ids_by_priority[victim_priority].popleft()
        self._items.pop(item_id)
        if not self._items:
            self._not_empty.clear()
        return True


async def _cancel_and_drain(task: asyncio.Task[Any]) -> None:
    """Cancel a queue waiter and settle it through repeated cancellation.

    ``asyncio.wait_for(coro)`` creates an internal task.  If the surrounding
    task is cancelled between loop iterations, that internal task can be
    created after the cancellation has already been delivered and become an
    orphaned ``Queue.get`` waiter.  Keeping the handle explicit lets every
    transport teardown drain it deterministically.
    """
    if not task.done():
        task.cancel()
    caller = asyncio.current_task()
    caller_cancelled = bool(caller is not None and caller.cancelling())
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # ``task.cancel()`` also makes ``shield(task)`` raise
            # ``CancelledError`` once the child settles.  That is expected
            # during a timeout and must not be propagated as cancellation of
            # the transport itself.  ``Task.cancelling`` identifies an
            # outstanding cancellation request on this waiter even when the
            # child happens to settle in the same event-loop turn.
            if caller is not None and caller.cancelling():
                caller_cancelled = True
    try:
        await task
    except asyncio.CancelledError:
        pass
    if caller_cancelled:
        raise asyncio.CancelledError


async def _next_with_timeout(subscriber: Any, timeout: float) -> Any:
    """Read one subscriber item without leaking a timeout/cancel waiter."""
    next_task = asyncio.create_task(subscriber.next())
    try:
        done, _ = await asyncio.wait({next_task}, timeout=timeout)
        if not done:
            await _cancel_and_drain(next_task)
            raise asyncio.TimeoutError
        return next_task.result()
    except BaseException:
        if not next_task.done():
            await _cancel_and_drain(next_task)
        else:
            # ``result`` may not have been reached if cancellation raced with
            # task completion; retrieve any exception to avoid warnings.
            try:
                next_task.exception()
            except (asyncio.CancelledError, Exception):
                pass
        raise


class _Subscriber:
    """A single SSE / WS subscriber backed by an async queue."""

    def __init__(
        self,
        session_id: str,
        *,
        max_queue_size: int,
        connected: str,
    ) -> None:
        self.session_id = session_id
        self._queue = _BoundedEventQueue(max_queue_size)
        # The protocol handshake is not business traffic and must not consume
        # bounded queue capacity.  Keeping it out of band also guarantees that
        # a size-one subscriber can observe both the handshake and one event.
        self._connected = connected
        self._connected_pending = True
        self._closed = False
        self._overflowed = False
        self.dropped_messages = 0

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def overflowed(self) -> bool:
        return self._overflowed

    async def push(self, data: str) -> None:
        # Publishing must never wait for a slow transport.  Keep the newest
        # events in a bounded buffer so one disconnected or stalled client
        # cannot stall every producer or grow memory without limit.
        self.push_nowait(data)

    def push_nowait(self, data: str) -> None:
        if self._closed:
            return
        incoming_priority = _event_priority(data)
        if self._queue.full():
            # Ordinary traffic remains lossy, but a control event must never
            # silently replace another control event: either evict ordinary
            # traffic or terminate the subscriber so its transport can recover
            # instead of continuing with an incomplete control history.
            if not self._queue.discard_oldest(_ORDINARY_EVENT):
                if incoming_priority > _ORDINARY_EVENT:
                    self._overflowed = True
                    self.dropped_messages += self._queue.qsize() + 1
                    self.close()
                    return
                self.dropped_messages += 1
                return
            self.dropped_messages += 1
        try:
            self._queue.put_nowait(data, priority=incoming_priority)
        except asyncio.QueueFull:
            # Defensive handling for callers publishing across threads.  A
            # raced control event follows the same fail-fast rule as above.
            if incoming_priority > _ORDINARY_EVENT:
                self._overflowed = True
                self.dropped_messages += self._queue.qsize() + 1
                self.close()
            else:
                self.dropped_messages += 1

    async def next(self) -> str | None:
        if self._connected_pending:
            self._connected_pending = False
            return self._connected
        return await self._queue.get()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # A closed session must not flush buffered events before the sentinel.
        # In particular, a session id can be resumed immediately after close;
        # retaining even one queued payload would leak the old lifecycle to a
        # client that is still draining this subscriber.  The connection
        # handshake is stored out of band and remains observable before EOF.
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        try:
            self._queue.put_nowait(None, priority=_LIFECYCLE_CONTROL_EVENT)
        except asyncio.QueueFull:
            # The queue was drained synchronously above, so this can only be a
            # cross-thread race.  A subsequent publisher observes _closed and
            # cannot add more data; retry after clearing the raced item.
            while True:
                try:
                    self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            self._queue.put_nowait(None, priority=_LIFECYCLE_CONTROL_EVENT)

    def drop_session(self, session_id: str) -> None:
        """Remove queued payloads belonging to a closed session lifecycle."""
        retained: list[str | None] = []
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if item is None:
                retained.append(item)
                continue
            try:
                payload = json.loads(item)
            except (TypeError, json.JSONDecodeError):
                retained.append(item)
                continue
            if not isinstance(payload, dict) or payload.get("session_id") != session_id:
                retained.append(item)
            else:
                self.dropped_messages += 1
        for item in retained:
            try:
                self._queue.put_nowait(item)
            except asyncio.QueueFull:
                # The queue was drained above, so this is only a defensive
                # guard for a concurrent producer.  Preserve the newest data.
                self.dropped_messages += 1


class EventBus:
    """Session-scoped event bus with multi-subscriber broadcast.

    All CoreEvents for a session are published here and fanned out
    to every connected subscriber (CLI, VSCode, web UI, etc.).
    """

    def __init__(
        self,
        *,
        subscriber_queue_size: int = _DEFAULT_SUBSCRIBER_QUEUE_SIZE,
    ) -> None:
        if subscriber_queue_size <= 0:
            raise ValueError("subscriber_queue_size must be greater than zero")
        self._subscriber_queue_size = subscriber_queue_size
        self._subscribers: dict[str, list[_Subscriber]] = {}
        self._global_subscribers: list[_Subscriber] = []
        # The same session id can be reused after archive/resume.  Keep the
        # currently installed object so late events from the old object cannot
        # enter a replacement session's global stream.
        self._session_sources: dict[str, object] = {}
        self._invalidated_sessions: set[str] = set()

    def register_session(self, session_id: str, source: object) -> None:
        """Bind a session id to its currently installed CoreSession object."""
        previous = self._session_sources.get(session_id)
        if previous is not None and previous is not source:
            # Defensive fencing for integrations that install a replacement
            # without first calling ``close_session``.  Drop queued events and
            # terminate id-bound streams before exposing the new source.
            self.close_session(session_id, previous)
        self._session_sources[session_id] = source
        self._invalidated_sessions.discard(session_id)

    def _source_is_current(
        self,
        session_id: str,
        source: object | None,
        event: Any | None = None,
    ) -> bool:
        # CoreSession objects are reused by the synchronous ``new_session``
        # API. Private lifecycle metadata prevents an event that was already
        # in flight from being relabelled with the replacement session id.
        event_session_id = getattr(event, "_crabcode_core_session_id", None)
        event_generation = getattr(
            event,
            "_crabcode_core_lifecycle_generation",
            None,
        )
        if event_session_id is not None and event_session_id != session_id:
            return False
        if source is not None:
            source_session_id = getattr(source, "session_id", None)
            if (
                event_session_id is not None
                and source_session_id
                and event_session_id != source_session_id
            ):
                return False
            source_generation = getattr(source, "_lifecycle_generation", None)
            if (
                event_generation is not None
                and source_generation is not None
                and event_generation != source_generation
            ):
                return False
        if source is None:
            # Keep legacy callers that publish before a session is registered
            # working, but do not let an unscoped late callback leak into a
            # lifecycle that was explicitly invalidated.  Re-registration
            # clears the tombstone before the replacement can publish.
            return session_id not in self._invalidated_sessions
        if session_id in self._invalidated_sessions:
            return False
        current = self._session_sources.get(session_id)
        # Legacy callers may publish before the gateway has registered the
        # object. Once a lifecycle has been explicitly invalidated, however,
        # late events from that object must remain suppressed until re-register.
        return current is None or current is source

    # ── Subscribe ────────────────────────────────────────────────

    def subscribe(self, session_id: str | None = None) -> _Subscriber:
        """Subscribe to events for a specific session (or all sessions)."""
        connected = ServerConnectedPayload().model_dump_json()
        sub = _Subscriber(
            session_id or "__global__",
            max_queue_size=self._subscriber_queue_size,
            connected=connected,
        )

        if session_id is None:
            self._global_subscribers.append(sub)
        else:
            self._subscribers.setdefault(session_id, []).append(sub)

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

    def close_session(self, session_id: str, source: object | None = None) -> None:
        """Terminate subscribers bound to a session lifecycle.

        Session IDs may be resumed after archive or process restart.  Leaving
        an old SSE subscriber registered would make it receive events from the
        replacement object with the same ID, which is a cross-lifecycle data
        leak.  Global subscribers are intentionally kept alive; their caller
        filters active-session events.
        """
        if source is not None:
            current = self._session_sources.get(session_id)
            # If a source is bound, only its owner may close the lifecycle.
            # With no binding there is nothing to compare against; still
            # perform cleanup so legacy/embedded callers cannot strand a
            # session-scoped subscriber created before registration.
            if current is not None and current is not source:
                return
        self._invalidated_sessions.add(session_id)
        self._session_sources.pop(session_id, None)
        subscribers = self._subscribers.pop(session_id, [])
        for sub in subscribers:
            sub.close()
        # Global WebSocket subscribers intentionally survive session switches,
        # but queued payloads from a closed lifecycle must not be replayed after
        # the same id is resumed.
        for sub in self._global_subscribers:
            sub.drop_session(session_id)

    def close_all(self) -> None:
        """Close every subscriber and invalidate all session sources."""
        # Include ids that only had a session-scoped subscriber. Embedded
        # callers are allowed to subscribe before registering a source; those
        # ids must still be fenced after a gateway-wide shutdown.
        self._invalidated_sessions.update(self._session_sources)
        self._invalidated_sessions.update(self._subscribers)
        self._session_sources.clear()
        for session_id in list(self._subscribers):
            subscribers = self._subscribers.pop(session_id, [])
            for sub in subscribers:
                sub.close()
        subscribers = self._global_subscribers[:]
        self._global_subscribers.clear()
        for sub in subscribers:
            sub.close()

    # ── Publish ──────────────────────────────────────────────────

    async def publish(
        self,
        session_id: str,
        event: Any,
        *,
        source: object | None = None,
        operation_id: str | None = None,
        operation_scope: str | None = None,
    ) -> None:
        """Publish a CoreEvent to all subscribers of the session."""
        if not self._source_is_current(session_id, source, event):
            return
        payload = core_event_to_payload(event)
        data_dict = json.loads(payload.model_dump_json())
        data_dict["session_id"] = session_id
        if operation_id is not None:
            data_dict["operation_id"] = operation_id
        if operation_scope is not None:
            data_dict["operation_scope"] = operation_scope
        data = json.dumps(data_dict)

        targets = list(self._subscribers.get(session_id, []))
        targets.extend(self._global_subscribers)

        for sub in targets:
            await sub.push(data)
            if sub.closed:
                self.unsubscribe(sub)

    def publish_nowait(
        self,
        session_id: str,
        event: Any,
        *,
        source: object | None = None,
        operation_id: str | None = None,
        operation_scope: str | None = None,
    ) -> None:
        """Non-async publish (for use from sync contexts)."""
        if not self._source_is_current(session_id, source, event):
            return
        payload = core_event_to_payload(event)
        data_dict = json.loads(payload.model_dump_json())
        data_dict["session_id"] = session_id
        if operation_id is not None:
            data_dict["operation_id"] = operation_id
        if operation_scope is not None:
            data_dict["operation_scope"] = operation_scope
        data = json.dumps(data_dict)

        targets = list(self._subscribers.get(session_id, []))
        targets.extend(self._global_subscribers)

        for sub in targets:
            sub.push_nowait(data)
            if sub.closed:
                self.unsubscribe(sub)

    async def publish_background(
        self,
        session_id: str,
        event: Any,
        *,
        source: object | None = None,
    ) -> None:
        """Publish an event that is not owned by a client foreground turn."""
        await self.publish(
            session_id,
            event,
            source=source,
            operation_id=uuid.uuid4().hex,
            operation_scope="background",
        )

    # ── SSE stream helper ────────────────────────────────────────

    async def sse_stream(
        self,
        session_id: str | None = None,
        *,
        subscriber: _Subscriber | None = None,
    ) -> AsyncIterator[str]:
        """Yield SSE-formatted event strings with heartbeat.

        Usage with sse-starlette::

            return EventSourceResponse(event_bus.sse_stream(session_id))
        """
        # Most callers let the generator subscribe lazily.  HTTP handlers
        # that validate a session selector before returning the response may
        # pass a pre-created subscriber instead: async-generator bodies do
        # not run until after the handler yields, leaving a window in which a
        # session can be archived and its id reused by a new CoreSession.
        sub = subscriber if subscriber is not None else self.subscribe(session_id)
        try:
            while True:
                # Wait for event or heartbeat timeout
                try:
                    data = await _next_with_timeout(sub, _HEARTBEAT_INTERVAL)
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

    async def ws_stream(
        self,
        ws: Any,
        session_id: str | None = None,
        *,
        session_id_getter: (
            Callable[[], str | set[str] | frozenset[str] | None] | None
        ) = None,
        subscriber: _Subscriber | None = None,
    ) -> None:
        """Push events to a WebSocket connection.

        The caller should run this in a background task and cancel
        it when the WebSocket disconnects.
        """
        # Callers that validate a selector under a registry lock can pass a
        # pre-created subscriber.  Lazy subscription here would leave a small
        # shutdown race: close_all() could run after validation but before this
        # background task starts, leaking a queue outside the shutdown sweep.
        sub = subscriber if subscriber is not None else self.subscribe(session_id)
        try:
            while True:
                try:
                    data = await _next_with_timeout(sub, _HEARTBEAT_INTERVAL)
                except asyncio.TimeoutError:
                    await ws.send_text(ServerHeartbeatPayload().model_dump_json())
                    continue
                if data is None:
                    return
                if isinstance(data, str):
                    if session_id is None and session_id_getter is not None:
                        # Global subscriptions are useful while a connection
                        # switches sessions.  Only sessions explicitly selected
                        # by this connection are forwarded; a single active id
                        # would drop events from an already-started background
                        # operation as soon as the UI switches conversations.
                        try:
                            selected = session_id_getter()
                            payload = json.loads(data)
                        except (TypeError, json.JSONDecodeError):
                            selected = None
                            payload = {}
                        if isinstance(selected, str):
                            selected_ids = {selected} if selected else set()
                        elif isinstance(selected, (set, frozenset)):
                            selected_ids = {
                                value for value in selected if isinstance(value, str) and value
                            }
                        else:
                            selected_ids = set()
                        event_session_id = payload.get("session_id")
                        if not selected_ids or (
                            event_session_id and event_session_id not in selected_ids
                        ):
                            continue
                    await ws.send_text(data)
                else:
                    await ws.send_text(str(data))
        except asyncio.CancelledError:
            pass
        except Exception:
            # A client can disappear between the queue wake-up and send_text.
            # Treat transport failures as a normal stream termination so the
            # endpoint can finish its cleanup without an unhandled task error.
            logger.debug("WebSocket event subscriber disconnected", exc_info=True)
        finally:
            self.unsubscribe(sub)
