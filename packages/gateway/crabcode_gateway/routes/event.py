"""SSE event stream and WebSocket endpoints — /event, /ws.

Mirrors OpenCode's event.ts SSE pattern with heartbeat keep-alive,
plus a WebSocket endpoint for bidirectional communication.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from sse_starlette.sse import EventSourceResponse

from crabcode_core.logging_utils import get_logger
from crabcode_gateway.event_bus import EventBus
from crabcode_gateway.session_registry import get_session_load_lock, get_session_lock
from crabcode_gateway.task_registry import (
    cancel_operation_task,
    cancel_owner_tasks,
    cancel_tasks,
    claim_operation,
    operation_is_registered,
    OperationAlreadyRegistered,
    release_operation_claim,
    run_session_operation,
    SessionOperationRejected,
    shielded_cleanup_session,
    track_task,
)

logger = get_logger(__name__)

_ACTIVE_SESSION_KEY = "crabcode_active_session_id"
_WS_TASKS_KEY = "crabcode_background_tasks"
_WS_TASK_SESSIONS_KEY = "crabcode_background_task_sessions"
# Keep the historical app-state name so embedding integrations that inspect
# plan_tasks continue to observe the active session claim.
_PLAN_TASKS_KEY = "plan_tasks"

router = APIRouter(tags=["events"])


class _ManagedEventSourceResponse(EventSourceResponse):
    """Ensure a pre-created EventBus subscriber is always released.

    A disconnect or send failure can cancel ``EventSourceResponse`` before
    its body iterator is entered.  In that case the generator's ``finally``
    block never runs, so the route-level subscription needs a response-level
    cleanup guard as well.
    """

    def __init__(self, content: Any, *, cleanup: Callable[[], None]) -> None:
        super().__init__(content)
        self._cleanup = cleanup

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._cleanup()


def _plan_tasks(app_state: Any) -> dict[str, Any]:
    """Return the process-wide plan-task map, lazily for lightweight apps."""
    tasks = getattr(app_state, _PLAN_TASKS_KEY, None)
    if tasks is None:
        tasks = {}
        setattr(app_state, _PLAN_TASKS_KEY, tasks)
    return tasks


def _release_plan_task(app_state: Any, session_id: str, owner: Any) -> None:
    """Release a plan claim only if it still belongs to *owner*."""
    tasks = getattr(app_state, _PLAN_TASKS_KEY, None)
    if tasks is not None and tasks.get(session_id) is owner:
        tasks.pop(session_id, None)


@router.get("/event")
async def event_stream(request: Request):
    """SSE endpoint for real-time event streaming.

    Clients connect here and receive a continuous stream of CoreEvent
    payloads.  Includes 10 s heartbeat to keep proxies from timing out.
    """
    event_bus: EventBus = request.app.state.event_bus
    session_id = request.query_params.get("session_id")
    # Subscribe while validating the selector and gateway lifecycle under the
    # same registry lock used by stop/archive.  Without this for the global
    # stream, a stale request could subscribe after ``close_all()`` and leave
    # a queue that no shutdown path would ever close.
    async with get_session_lock(request.app.state):
        if getattr(request.app.state, "gateway_closing", False):
            raise HTTPException(status_code=503, detail="Gateway is shutting down")
        if session_id is not None:
            if (
                session_id not in request.app.state.sessions
                or session_id in getattr(request.app.state, "closing_sessions", set())
            ):
                raise HTTPException(status_code=404, detail="Session not found")
            subscriber = event_bus.subscribe(session_id)
        else:
            subscriber = event_bus.subscribe(None)

    async def _generate():
        async for data in event_bus.sse_stream(
            session_id,
            subscriber=subscriber,
        ):
            yield data

    try:
        return _ManagedEventSourceResponse(
            _generate(),
            cleanup=lambda: event_bus.unsubscribe(subscriber),
        )
    except BaseException:
        event_bus.unsubscribe(subscriber)
        raise


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """Bidirectional WebSocket endpoint.

    Uses a global subscription — events from ALL sessions are forwarded
    to the client (each tagged with session_id). The client filters by
    active session. This ensures no events are lost when switching sessions.
    """
    query_session_id = ws.query_params.get("session_id")
    event_bus: EventBus = ws.app.state.event_bus
    async with get_session_lock(ws.app.state):
        gateway_closing = getattr(ws.app.state, "gateway_closing", False)
        invalid_selector = bool(
            query_session_id is not None
            and (
                query_session_id not in ws.app.state.sessions
                or query_session_id in getattr(ws.app.state, "closing_sessions", set())
            )
        )
        # Install the subscriber before releasing the same lock used by stop()
        # and close_all().  The queue is therefore always covered by either
        # this connection's cleanup or the gateway's shutdown sweep.
        subscriber = (
            None
            if gateway_closing or invalid_selector
            else event_bus.subscribe(None)
        )

    if gateway_closing:
        await ws.close(code=1012, reason="Gateway is shutting down")
        return
    if invalid_selector:
        await ws.close(code=1008, reason="Session not found")
        return
    assert subscriber is not None

    try:
        await ws.accept()
    except BaseException:
        if subscriber is not None:
            event_bus.unsubscribe(subscriber)
        raise
    # Snapshot the default at connection time.  Looking it up for every
    # command would let another client's new/resume operation retarget this
    # connection unexpectedly.
    ws.scope[_ACTIVE_SESSION_KEY] = (
        query_session_id
        if query_session_id is not None
        else ws.app.state.default_session_id
    )
    owner_tasks: set[asyncio.Task[Any]] = set()
    owner_sessions: dict[asyncio.Task[Any], str] = {}
    ws.scope[_WS_TASKS_KEY] = owner_tasks
    ws.scope[_WS_TASK_SESSIONS_KEY] = owner_sessions

    # A global queue survives session switches; ws_stream filters payloads to
    # this connection's active session before sending them.
    push_task = asyncio.create_task(
        event_bus.ws_stream(
            ws,
            None,
            session_id_getter=lambda: ws.scope.get(_ACTIVE_SESSION_KEY),
            subscriber=subscriber,
        )
    )

    receive_task: asyncio.Task[str] | None = None
    transport_disconnected = False
    try:
        while True:
            # A failed outbound send terminates ``ws_stream``.  Race that
            # producer against the inbound read so a half-closed transport
            # cannot leave this handler and its owner tasks blocked forever.
            receive_task = asyncio.create_task(ws.receive_text())
            done, _ = await asyncio.wait(
                {receive_task, push_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if push_task in done:
                return
            raw = receive_task.result()
            receive_task = None
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_text(json.dumps({"type": "error", "message": "invalid JSON"}))
                continue

            if not isinstance(msg, dict):
                await ws.send_text(
                    json.dumps({"type": "error", "message": "message must be a JSON object"})
                )
                continue

            msg_type = msg.get("type", "")
            if not isinstance(msg_type, str):
                await ws.send_text(
                    json.dumps({"type": "error", "message": "message type must be a string"})
                )
                continue

            try:
                if msg_type == "permission_response":
                    await _handle_permission_response(ws, msg)
                elif msg_type == "choice_response":
                    await _handle_choice_response(ws, msg)
                elif msg_type == "send_message":
                    await _handle_send_message(ws, msg)
                elif msg_type == "steer_message":
                    await _handle_steer_message(ws, msg)
                elif msg_type == "new_session":
                    await _handle_new_session(ws, msg)
                elif msg_type == "resume_session":
                    await _handle_resume_session(ws, msg)
                elif msg_type == "interrupt":
                    await _handle_interrupt(ws, msg)
                elif msg_type == "push_context":
                    await _handle_push_context(ws, msg)
                elif msg_type == "switch_model":
                    await _handle_switch_model(ws, msg)
                elif msg_type == "set_permission_mode":
                    await _handle_set_permission_mode(ws, msg)
                elif msg_type == "plan_action":
                    await _handle_plan_action(ws, msg)
                else:
                    await ws.send_text(json.dumps({
                        "type": "error",
                        "message": f"unknown message type: {msg_type}",
                    }))
            except WebSocketDisconnect:
                # The transport is already gone; attempting to send a second
                # error frame here can mask the disconnect and skip clean
                # shutdown paths in ASGI servers.
                raise
            except Exception as exc:
                # A malformed command or a client/session race must not tear
                # down the entire WebSocket stream.  Individual handlers still
                # log their domain failures; this boundary keeps the transport
                # usable for the next command.
                logger.warning("WebSocket command failed (%s)", msg_type, exc_info=True)
                await ws.send_text(json.dumps({"type": "error", "message": str(exc)}))
    except WebSocketDisconnect:
        transport_disconnected = True
        logger.info("WebSocket disconnected")
    finally:
        async def _cleanup() -> None:
            # Stop the event producer first, then drain command tasks.  Keep
            # both operations in one owned task so cancellation of the ASGI
            # handler cannot strand either side of the connection.
            transport_tasks = [push_task]
            if receive_task is not None:
                transport_tasks.append(receive_task)
            for task in transport_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*transport_tasks, return_exceptions=True)
            await cancel_owner_tasks(owner_tasks)

        cleanup_task = asyncio.create_task(_cleanup())
        cleanup_cancelled = False
        try:
            while not cleanup_task.done():
                try:
                    await asyncio.shield(cleanup_task)
                except asyncio.CancelledError:
                    # A second cancellation must still allow owner tasks and
                    # the event subscriber to settle before propagating.
                    cleanup_cancelled = True
            await cleanup_task
        except asyncio.CancelledError:
            cleanup_cancelled = True
        except Exception:
            logger.warning("WebSocket cleanup failed", exc_info=True)
        # ASGI test servers, and some production transports, cancel the
        # endpoint task immediately after delivering websocket.disconnect.
        # The disconnect has already been handled and cleanup has completed,
        # so propagating that trailing cancellation turns a normal close into
        # an application failure.  Preserve cancellation for every other
        # path, including server shutdown before a disconnect is observed.
        if cleanup_cancelled and not transport_disconnected:
            raise asyncio.CancelledError


def _resolve_session(ws: WebSocket, msg: dict):
    sessions: dict = ws.app.state.sessions
    if "session_id" in msg and msg.get("session_id") is not None:
        session_id = msg.get("session_id")
    else:
        session_id = ws.scope.get(_ACTIVE_SESSION_KEY)
    if not session_id:
        return None
    if session_id in getattr(ws.app.state, "closing_sessions", set()):
        return None
    return sessions.get(session_id)


def _set_active_session(ws: WebSocket, session_id: str) -> None:
    ws.scope[_ACTIVE_SESSION_KEY] = session_id


async def _store_ws_context(contexts: dict, session: Any, payload: dict) -> None:
    contexts[session.session_id] = payload


async def _switch_ws_model(session: Any, name: str) -> bool:
    await session.initialize()
    return bool(session.switch_model(name))


async def _set_ws_permission_mode(session: Any, mode: str) -> bool:
    await session.initialize()
    return bool(session.set_client_permission_mode(mode))


def _ws_owner_args(ws: WebSocket) -> dict[str, Any]:
    return {
        "owner_tasks": ws.scope.get(_WS_TASKS_KEY),
        "owner_sessions": ws.scope.get(_WS_TASK_SESSIONS_KEY),
    }


async def _handle_permission_response(ws: WebSocket, msg: dict) -> None:
    """Route a permission response from the client to the session."""
    from crabcode_core.types.event import PermissionResponseEvent

    session = _resolve_session(ws, msg)
    if not session:
        logger.warning("ws permission_response rejected: no active session")
        await ws.send_text(json.dumps({"type": "error", "message": "no active session"}))
        return

    tool_use_id = msg.get("tool_use_id", "")
    allowed = msg.get("allowed", False)
    always_allow = msg.get("always_allow", False)
    agent_id = msg.get("agent_id")
    feedback = msg.get("feedback")
    if (
        not isinstance(tool_use_id, str)
        or not isinstance(allowed, bool)
        or not isinstance(always_allow, bool)
        or (agent_id is not None and not isinstance(agent_id, str))
        or (feedback is not None and not isinstance(feedback, str))
    ):
        await ws.send_text(json.dumps({"type": "error", "message": "invalid permission response"}))
        return
    event = PermissionResponseEvent(
        tool_use_id=tool_use_id,
        allowed=allowed,
        always_allow=always_allow,
        agent_id=agent_id,
        feedback=feedback,
    )
    try:
        await run_session_operation(
            ws.app.state,
            session,
            lambda: session.respond_permission(event),
            **_ws_owner_args(ws),
        )
    except SessionOperationRejected:
        await ws.send_text(json.dumps({"type": "error", "message": "session is closing"}))


async def _handle_choice_response(ws: WebSocket, msg: dict) -> None:
    """Route a choice response from the client to the session."""
    from crabcode_core.types.event import ChoiceResponseEvent

    session = _resolve_session(ws, msg)
    if not session:
        logger.warning("ws choice_response rejected: no active session")
        await ws.send_text(json.dumps({"type": "error", "message": "no active session"}))
        return

    tool_use_id = msg.get("tool_use_id", "")
    selected = msg.get("selected", [])
    cancelled = msg.get("cancelled", False)
    agent_id = msg.get("agent_id")
    if (
        not isinstance(tool_use_id, str)
        or not isinstance(selected, list)
        or any(not isinstance(item, str) for item in selected)
        or not isinstance(cancelled, bool)
        or (agent_id is not None and not isinstance(agent_id, str))
    ):
        await ws.send_text(json.dumps({"type": "error", "message": "invalid choice response"}))
        return
    event = ChoiceResponseEvent(
        tool_use_id=tool_use_id,
        selected=selected,
        cancelled=cancelled,
        agent_id=agent_id,
    )
    try:
        await run_session_operation(
            ws.app.state,
            session,
            lambda: session.respond_choice(event),
            **_ws_owner_args(ws),
        )
    except SessionOperationRejected:
        await ws.send_text(json.dumps({"type": "error", "message": "session is closing"}))


async def _handle_send_message(ws: WebSocket, msg: dict) -> None:
    """Start a query loop from a WebSocket message."""
    event_bus: EventBus = ws.app.state.event_bus
    text = msg.get("text", "")
    max_turns = msg.get("max_turns", 0)
    images = msg.get("images")  # Optional list of {media_type, data} dicts
    requested_operation_id = msg.get("operation_id")

    if not isinstance(text, str) or not text.strip():
        await ws.send_text(json.dumps({"type": "error", "message": "text must be a non-empty string"}))
        return
    if isinstance(max_turns, bool) or not isinstance(max_turns, int) or max_turns < 0:
        await ws.send_text(json.dumps({"type": "error", "message": "max_turns must be a non-negative integer"}))
        return
    if requested_operation_id is not None and (
        not isinstance(requested_operation_id, str)
        or not requested_operation_id
    ):
        await ws.send_text(json.dumps({
            "type": "error",
            "message": "operation_id must be a non-empty string",
        }))
        return
    if images is not None:
        if not isinstance(images, list) or any(
            not isinstance(item, dict)
            or not isinstance(item.get("media_type"), str)
            or not isinstance(item.get("data"), str)
            for item in images
        ):
            await ws.send_text(json.dumps({"type": "error", "message": "images must be a list of media attachments"}))
            return

    # Resolve and create the detached task under the registry lock used by
    # archive/stop.  This prevents a stale WebSocket command from starting a
    # query after its CoreSession has begun closing.
    async with get_session_lock(ws.app.state):
        if getattr(ws.app.state, "gateway_closing", False):
            session = None
        else:
            session = _resolve_session(ws, msg)
        if session is not None:
            current = ws.app.state.sessions.get(session.session_id)
            if current is not session or session.session_id in getattr(
                ws.app.state, "closing_sessions", set()
            ):
                session = None

        if session is None:
            error_message = (
                "gateway shutting down"
                if getattr(ws.app.state, "gateway_closing", False)
                else "no active session"
            )
            task = None
        else:
            task = None

    operation_id = requested_operation_id or uuid.uuid4().hex
    logger.info(
        "ws send_message %s session=%s operation=%s chars=%d images=%d max_turns=%s",
        "accepted" if session is not None else "rejected",
        session.session_id if session is not None else None,
        operation_id,
        len(text),
        len(images) if isinstance(images, list) else 0,
        max_turns,
    )

    async def _run():
        try:
            from crabcode_core.types.event import ErrorEvent, TurnCompleteEvent

            kwargs = {"max_turns": max_turns}
            if images and isinstance(images, list):
                kwargs["images"] = images
            async for event in session.send_message(text, **kwargs):
                await event_bus.publish(
                    session.session_id,
                    event,
                    source=session,
                    operation_id=operation_id,
                    operation_scope="foreground",
                )
                if isinstance(event, TurnCompleteEvent) or (
                    isinstance(event, ErrorEvent) and event.agent_id is None
                ):
                    current = asyncio.current_task()
                    if current is not None:
                        setattr(current, "_crabcode_terminal_published", True)
            logger.info("ws send_message completed session=%s", session.session_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("ws send_message failed session=%s", session.session_id)
            from crabcode_core.types.event import ErrorEvent
            await event_bus.publish(
                session.session_id,
                ErrorEvent(message=str(exc), recoverable=False, error_type="internal"),
                source=session,
                operation_id=operation_id,
                operation_scope="foreground",
            )
            current = asyncio.current_task()
            if current is not None:
                setattr(current, "_crabcode_terminal_published", True)

    if session is None:
        await ws.send_text(json.dumps({"type": "error", "message": error_message}))
        return

    duplicate_operation = False
    async with get_session_lock(ws.app.state):
        # Re-check immediately before registration in case a future caller
        # moves task creation out of the first critical section.
        if (
            getattr(ws.app.state, "gateway_closing", False)
            or ws.app.state.sessions.get(session.session_id) is not session
            or session.session_id in getattr(ws.app.state, "closing_sessions", set())
        ):
            task = None
        elif operation_is_registered(
            ws.app.state,
            session.session_id,
            operation_id,
        ):
            if requested_operation_id is None:
                while operation_is_registered(
                    ws.app.state,
                    session.session_id,
                    operation_id,
                ):
                    operation_id = uuid.uuid4().hex
            else:
                duplicate_operation = True
                task = None
        if not duplicate_operation and not (
            getattr(ws.app.state, "gateway_closing", False)
            or ws.app.state.sessions.get(session.session_id) is not session
            or session.session_id in getattr(ws.app.state, "closing_sessions", set())
        ):
            task = asyncio.create_task(_run())
            track_task(
                ws.app.state,
                session.session_id,
                task,
                owner_tasks=ws.scope[_WS_TASKS_KEY],
                owner_sessions=ws.scope[_WS_TASK_SESSIONS_KEY],
                operation_id=operation_id,
                operation_scope="foreground",
            )
    if task is None:
        message = (
            f"operation already active: {operation_id}"
            if duplicate_operation
            else "session is closing"
        )
        await ws.send_text(json.dumps({"type": "error", "message": message}))
        return


async def _handle_steer_message(ws: WebSocket, msg: dict) -> None:
    """Inject user guidance at the foreground loop's next safe boundary."""
    text = msg.get("text", "")
    images = msg.get("images")
    if not isinstance(text, str) or not text.strip():
        await ws.send_text(json.dumps({
            "type": "error",
            "message": "text must be a non-empty string",
        }))
        return
    if images is not None and (
        not isinstance(images, list)
        or any(
            not isinstance(item, dict)
            or not isinstance(item.get("media_type"), str)
            or not isinstance(item.get("data"), str)
            for item in images
        )
    ):
        await ws.send_text(json.dumps({
            "type": "error",
            "message": "images must be a list of media attachments",
        }))
        return

    session = _resolve_session(ws, msg)
    if session is None:
        await ws.send_text(json.dumps({"type": "error", "message": "no active session"}))
        return

    try:
        queued = await run_session_operation(
            ws.app.state,
            session,
            lambda: session.steer_message(text, images=images),
            **_ws_owner_args(ws),
        )
    except SessionOperationRejected:
        await ws.send_text(json.dumps({"type": "error", "message": "session is closing"}))
        return

    logger.info(
        "ws steer_message %s session=%s chars=%d images=%d",
        "queued" if queued else "continued as a new turn",
        session.session_id,
        len(text),
        len(images) if isinstance(images, list) else 0,
    )
    if not queued:
        # The frontend can race with a just-completed turn. Falling back to the
        # ordinary serialized path ensures the user's message is never lost.
        fallback = dict(msg)
        fallback["type"] = "send_message"
        fallback.setdefault("max_turns", 0)
        await _handle_send_message(ws, fallback)


async def _handle_new_session(ws: WebSocket, msg: dict) -> None:
    """Create a new session and publish its id to connected clients."""
    import os
    from crabcode_core.session import CoreSession
    from crabcode_core.types.config import CrabCodeSettings
    from crabcode_gateway.schemas import ServerConnectedPayload

    if getattr(ws.app.state, "gateway_closing", False):
        await ws.send_text(json.dumps({"type": "error", "message": "gateway shutting down"}))
        return

    previous_id = ws.scope.get(_ACTIVE_SESSION_KEY)
    cwd = msg.get("cwd") or os.getcwd()
    settings = CrabCodeSettings()
    session = CoreSession(cwd=cwd, settings=settings)
    async def _publish_background(event) -> None:
        await ws.app.state.event_bus.publish_background(
            session.session_id,
            event,
            source=session,
        )
    session.set_background_event_sink(_publish_background)
    registered = False
    try:
        await session.initialize()
        session.new_session()

        async with get_session_lock(ws.app.state):
            if getattr(ws.app.state, "gateway_closing", False):
                rejected = True
            else:
                sessions: dict = ws.app.state.sessions
                ws.app.state.event_bus.register_session(session.session_id, session)
                sessions[session.session_id] = session
                if ws.app.state.default_session_id is None:
                    ws.app.state.default_session_id = session.session_id
                _set_active_session(ws, session.session_id)
                registered = True
                rejected = False
        if rejected:
            await shielded_cleanup_session(
                ws.app.state,
                session.session_id,
                session,
                owns_registry=False,
            )
            await ws.send_text(json.dumps({"type": "error", "message": "gateway shutting down"}))
            return
    except asyncio.CancelledError:
        if not registered:
            try:
                await shielded_cleanup_session(
                    ws.app.state,
                    getattr(session, "session_id", "") or f"unregistered:{id(session)}",
                    session,
                    owns_registry=False,
                )
            except Exception:
                logger.warning("Failed to clean up cancelled WebSocket session creation", exc_info=True)
        raise
    except Exception as exc:
        if not registered:
            try:
                await shielded_cleanup_session(
                    ws.app.state,
                    getattr(session, "session_id", "") or f"unregistered:{id(session)}",
                    session,
                    owns_registry=False,
                )
            except Exception:
                logger.warning("Failed to clean up failed WebSocket session creation", exc_info=True)
        await ws.send_text(json.dumps({"type": "error", "message": str(exc)}))
        return
    logger.info("ws new_session created session=%s cwd=%s", session.session_id, cwd)

    # Switching the connection's active session transfers ownership away from
    # its previous foreground/plan work.  Match resume_session's behavior so a
    # hidden old turn cannot keep using provider resources and writing history
    # after the client has moved to the newly created conversation.
    if previous_id and previous_id != session.session_id:
        owner_tasks = ws.scope.get(_WS_TASKS_KEY, set())
        owner_sessions = ws.scope.get(_WS_TASK_SESSIONS_KEY, {})
        old_tasks = [
            task
            for task in owner_tasks
            if owner_sessions.get(task) == previous_id
        ]
        if old_tasks:
            await cancel_tasks(old_tasks)

    await ws.send_text(
        ServerConnectedPayload(properties={"session_id": session.session_id}).model_dump_json()
    )


async def _handle_interrupt(ws: WebSocket, msg: dict) -> None:
    """Interrupt the current query loop for the active session."""
    session = _resolve_session(ws, msg)
    if not session:
        logger.warning("ws interrupt rejected: no active session")
        await ws.send_text(json.dumps({"type": "error", "message": "no active session"}))
        return

    operation_id = msg.get("operation_id")
    if operation_id is not None and (
        not isinstance(operation_id, str) or not operation_id
    ):
        await ws.send_text(json.dumps({
            "type": "error",
            "message": "operation_id must be a non-empty string",
        }))
        return

    logger.info(
        "ws interrupt session=%s operation=%s",
        session.session_id,
        operation_id,
    )
    if operation_id is not None:
        cancelled = await cancel_operation_task(
            ws.app.state,
            session.session_id,
            operation_id,
            expected_session=session,
        )
        if cancelled is None:
            await ws.send_text(json.dumps({
                "type": "error",
                "message": f"operation not found: {operation_id}",
            }))
            return
        task = cancelled.task
        try:
            if not getattr(task, "_crabcode_terminal_published", False):
                from crabcode_core.types.event import TurnCompleteEvent

                await ws.app.state.event_bus.publish(
                    session.session_id,
                    TurnCompleteEvent(reason="interrupted"),
                    source=session,
                    operation_id=operation_id,
                    operation_scope=(
                        getattr(task, "_crabcode_operation_scope", None)
                        or "foreground"
                    ),
                )
                setattr(task, "_crabcode_terminal_published", True)
        finally:
            release_operation_claim(
                ws.app.state,
                session.session_id,
                operation_id,
                cancelled.claim,
            )
        return

    try:
        await run_session_operation(
            ws.app.state,
            session,
            session.interrupt,
            **_ws_owner_args(ws),
        )
    except SessionOperationRejected:
        await ws.send_text(json.dumps({"type": "error", "message": "session is closing"}))


async def _handle_push_context(ws: WebSocket, msg: dict) -> None:
    """Store client-pushed context."""
    contexts: dict = ws.app.state.client_contexts
    session = _resolve_session(ws, msg)
    if session:
        try:
            await run_session_operation(
                ws.app.state,
                session,
                lambda: _store_ws_context(contexts, session, msg),
                **_ws_owner_args(ws),
            )
        except SessionOperationRejected:
            await ws.send_text(json.dumps({"type": "error", "message": "session is closing"}))
            return
        logger.info("ws push_context session=%s active_file=%s", session.session_id, msg.get("active_file"))
    else:
        logger.warning("ws push_context ignored: no active session")


async def _handle_switch_model(ws: WebSocket, msg: dict) -> None:
    """Switch named model profile on the active session (VS Code chat selector)."""
    session = _resolve_session(ws, msg)
    if not session:
        logger.warning("ws switch_model rejected: no active session")
        await ws.send_text(json.dumps({"type": "error", "message": "no active session"}))
        return

    name = msg.get("name", "")
    if not isinstance(name, str) or not name.strip():
        await ws.send_text(json.dumps({"type": "error", "message": "model name must be a string"}))
        return
    try:
        ok = await run_session_operation(
            ws.app.state,
            session,
            lambda: _switch_ws_model(session, name),
            **_ws_owner_args(ws),
        )
    except SessionOperationRejected:
        await ws.send_text(json.dumps({"type": "error", "message": "session is closing"}))
        return
    if not ok:
        logger.warning("ws switch_model failed session=%s name=%s", session.session_id, name)
        await ws.send_text(
            json.dumps({"type": "error", "message": f"model not found: {name}"}),
        )
        return
    logger.info("ws switch_model session=%s name=%s", session.session_id, name)


async def _handle_set_permission_mode(ws: WebSocket, msg: dict) -> None:
    """Apply extension chat footer permission mode (default vs run_everything)."""
    session = _resolve_session(ws, msg)
    if not session:
        logger.warning("ws set_permission_mode rejected: no active session")
        await ws.send_text(json.dumps({"type": "error", "message": "no active session"}))
        return

    mode = msg.get("mode", "default")
    if not isinstance(mode, str):
        await ws.send_text(json.dumps({"type": "error", "message": "permission mode must be a string"}))
        return
    try:
        ok = await run_session_operation(
            ws.app.state,
            session,
            lambda: _set_ws_permission_mode(session, mode),
            **_ws_owner_args(ws),
        )
    except SessionOperationRejected:
        await ws.send_text(json.dumps({"type": "error", "message": "session is closing"}))
        return
    if not ok:
        logger.warning("ws set_permission_mode failed session=%s mode=%s", session.session_id, mode)
        await ws.send_text(
            json.dumps({"type": "error", "message": f"invalid permission mode: {mode}"}),
        )
        return
    logger.info("ws set_permission_mode session=%s mode=%s", session.session_id, mode)


async def _handle_plan_action(ws: WebSocket, msg: dict) -> None:
    """Execute, revise, or cancel a plan submitted by plan mode."""
    event_bus: EventBus = ws.app.state.event_bus
    async with get_session_lock(ws.app.state):
        session = None if getattr(ws.app.state, "gateway_closing", False) else _resolve_session(ws, msg)
    if not session:
        logger.warning("ws plan_action rejected: no active session")
        await ws.send_text(json.dumps({"type": "error", "message": "no active session"}))
        return

    action = msg.get("action")
    requested_operation_id = msg.get("operation_id")
    if requested_operation_id is not None and (
        not isinstance(requested_operation_id, str)
        or not requested_operation_id
    ):
        await ws.send_text(json.dumps({
            "type": "error",
            "message": "operation_id must be a non-empty string",
        }))
        return
    operation_id = requested_operation_id or uuid.uuid4().hex

    if action == "revise":
        from crabcode_core.types.event import ModeChangeEvent

        async def _revise_plan() -> None:
            async with session._turn_scope():  # type: ignore[attr-defined]
                session.switch_mode("plan")
                await event_bus.publish(
                    session.session_id,
                    ModeChangeEvent(mode="plan"),
                    source=session,
                    operation_id=operation_id,
                    operation_scope="plan",
                )

        try:
            await run_session_operation(
                ws.app.state,
                session,
                _revise_plan,
                **_ws_owner_args(ws),
            )
        except SessionOperationRejected:
            await ws.send_text(json.dumps({"type": "error", "message": "session is closing"}))
        return

    if action == "cancel":
        running_plan: asyncio.Task[Any] | None = None
        pending_plan: Any = None
        running_operation_id: str | None = None
        async with get_session_lock(ws.app.state):
            invalid = (
                getattr(ws.app.state, "gateway_closing", False)
                or ws.app.state.sessions.get(session.session_id) is not session
                or session.session_id in getattr(ws.app.state, "closing_sessions", set())
            )
            if not invalid:
                pending_plan = getattr(session, "current_plan", None)
                plan_tasks = _plan_tasks(ws.app.state)
                owner = plan_tasks.get(session.session_id)
                if isinstance(owner, asyncio.Task) and owner.done():
                    plan_tasks.pop(session.session_id, None)
                    owner = None
                if isinstance(owner, asyncio.Task) and not owner.done():
                    owner_operation_id = getattr(
                        owner,
                        "_crabcode_operation_id",
                        None,
                    )
                    if (
                        requested_operation_id is None
                        or requested_operation_id == owner_operation_id
                    ):
                        running_plan = owner
                        running_operation_id = owner_operation_id
        if invalid:
            await ws.send_text(json.dumps({"type": "error", "message": "session is closing"}))
            return
        if requested_operation_id is not None and running_plan is None:
            await ws.send_text(json.dumps({
                "type": "error",
                "message": f"operation not found: {requested_operation_id}",
            }))
            return

        cancellation_claim: object | None = None
        if running_plan is not None:
            if running_operation_id is not None:
                cancelled = await cancel_operation_task(
                    ws.app.state,
                    session.session_id,
                    running_operation_id,
                    expected_session=session,
                )
                if cancelled is None:
                    return
                running_plan = cancelled.task
                cancellation_claim = cancelled.claim
            else:
                # Compatibility for custom integrations that installed a plan
                # task before operation attribution existed.
                await cancel_tasks([running_plan])
                running_operation_id = operation_id

            async with get_session_lock(ws.app.state):
                _release_plan_task(
                    ws.app.state,
                    session.session_id,
                    running_plan,
                )

        # Clear only the plan that was visible when cancellation began.  A
        # foreground turn may have produced a replacement while the plan task
        # was being drained; that newer plan belongs to the other operation.
        try:
            turn_scope = getattr(session, "_turn_scope", None)
            if callable(turn_scope):
                async with turn_scope():
                    if getattr(session, "current_plan", None) is pending_plan:
                        session.set_plan(None)
            elif getattr(session, "current_plan", None) is pending_plan:
                session.set_plan(None)

            if running_plan is not None and not getattr(
                running_plan,
                "_crabcode_terminal_published",
                False,
            ):
                # A cancelled executor cannot reach its natural completion.
                from crabcode_core.types.event import TurnCompleteEvent

                await event_bus.publish(
                    session.session_id,
                    TurnCompleteEvent(reason="plan_cancelled"),
                    source=session,
                    operation_id=running_operation_id,
                    operation_scope="plan",
                )
                setattr(running_plan, "_crabcode_terminal_published", True)
        finally:
            if cancellation_claim is not None and running_operation_id is not None:
                release_operation_claim(
                    ws.app.state,
                    session.session_id,
                    running_operation_id,
                    cancellation_claim,
                )
        return

    if action != "execute":
        await ws.send_text(json.dumps({"type": "error", "message": f"invalid plan action: {action}"}))
        return

    from crabcode_core.plan.executor import PlanExecutor
    from crabcode_core.plan.types import ExecutionPlan
    from crabcode_core.types.event import ErrorEvent, ModeChangeEvent, TurnCompleteEvent

    # Admission claims both the one-plan-per-session slot and the caller's
    # operation id without awaiting.  The detached task reads and consumes the
    # plan only after it owns CoreSession's turn boundary.
    plan_busy = False
    duplicate_operation = False
    invalid = False
    submitted_plan = msg.get("plan")
    operation_claim = object()

    async def _run() -> None:
        session_id = session.session_id
        event_count = 0
        producer: asyncio.Task[None] | None = None
        forwarder: asyncio.Task[None] | None = None
        consumed_plan: Any = None
        previous_mode = "plan"
        execution_started = False
        try:
            async with session._turn_scope():  # type: ignore[attr-defined]
                session._foreground_turn_active = True  # type: ignore[attr-defined]
                if hasattr(session, "_active_event_stream_token"):
                    session._active_event_stream_token = getattr(  # type: ignore[attr-defined]
                        session,
                        "_active_turn_token",
                        None,
                    )
                try:
                    previous_mode = getattr(session, "agent_mode", "plan")
                    plan_data = getattr(session, "current_plan", None) or submitted_plan
                    if not plan_data:
                        raise ValueError("no pending plan")
                    plan = (
                        ExecutionPlan.from_dict(plan_data)
                        if isinstance(plan_data, dict)
                        else plan_data
                    )
                    if not isinstance(plan, ExecutionPlan):
                        raise TypeError("invalid execution plan")
                    validation_errors = plan.validate_dag()
                    if validation_errors:
                        raise ValueError(
                            "Plan DAG validation failed: "
                            + "; ".join(validation_errors)
                        )

                    consumed_plan = plan_data
                    session.set_plan(None)
                    session.switch_mode("agent")
                    try:
                        await event_bus.publish(
                            session_id,
                            ModeChangeEvent(mode="agent"),
                            source=session,
                            operation_id=operation_id,
                            operation_scope="plan",
                        )
                    except BaseException:
                        session.set_plan(consumed_plan)
                        session.switch_mode(previous_mode)
                        consumed_plan = None
                        raise

                    merged_events: asyncio.Queue[object] = asyncio.Queue()
                    done_sentinel = object()

                    async def _produce_plan_events() -> None:
                        executor = PlanExecutor(
                            plan,
                            spawn_fn=session.spawn_agent,
                            wait_fn=session.wait_agent,
                            cancel_fn=getattr(session, "cancel_agent", None),
                        )
                        try:
                            async for plan_event in executor.execute():
                                await merged_events.put(plan_event)
                        finally:
                            await merged_events.put(done_sentinel)

                    async def _forward_agent_events() -> None:
                        from crabcode_core.types.event import ModeChangeEvent, PlanReadyEvent

                        while True:
                            event = await session._agent_event_queue.get()  # type: ignore[attr-defined]
                            stream_matcher = getattr(
                                session,
                                "_event_matches_active_stream",
                                None,
                            )
                            if callable(stream_matcher) and not stream_matcher(event):
                                continue
                            if isinstance(event, (ModeChangeEvent, PlanReadyEvent)):
                                logger.info(
                                    "ws plan execution suppressed sub-agent event session=%s type=%s",
                                    session_id,
                                    type(event).__name__,
                                )
                                continue
                            await merged_events.put(event)

                    try:
                        producer = asyncio.create_task(_produce_plan_events())
                        forwarder = asyncio.create_task(_forward_agent_events())
                        execution_started = True
                        while True:
                            event = await merged_events.get()
                            if event is done_sentinel:
                                break
                            await event_bus.publish(
                                session_id,
                                event,
                                source=session,
                                operation_id=operation_id,
                                operation_scope="plan",
                            )
                            event_count += 1
                        await producer
                    finally:
                        for child in (forwarder, producer):
                            if child is not None and not child.done():
                                child.cancel()
                        children = [
                            child for child in (forwarder, producer) if child is not None
                        ]
                        if children:
                            await asyncio.gather(*children, return_exceptions=True)

                    await event_bus.publish(
                        session_id,
                        TurnCompleteEvent(reason="plan_complete"),
                        source=session,
                        operation_id=operation_id,
                        operation_scope="plan",
                    )
                    current = asyncio.current_task()
                    if current is not None:
                        setattr(current, "_crabcode_terminal_published", True)
                except BaseException:
                    if consumed_plan is not None and not execution_started:
                        session.set_plan(consumed_plan)
                        session.switch_mode(previous_mode)
                        consumed_plan = None
                    raise
                finally:
                    if hasattr(session, "_active_event_stream_token"):
                        session._active_event_stream_token = None  # type: ignore[attr-defined]
                    session._foreground_turn_active = False  # type: ignore[attr-defined]
            logger.info("ws plan execution completed session=%s events=%d", session_id, event_count)
        except asyncio.CancelledError:
            logger.info("ws plan execution cancelled session=%s", session_id)
            raise
        except Exception as exc:
            logger.exception("ws plan execution failed session=%s", session_id)
            await event_bus.publish(
                session_id,
                ErrorEvent(message=str(exc), recoverable=False, error_type="internal"),
                source=session,
                operation_id=operation_id,
                operation_scope="plan",
            )
            await event_bus.publish(
                session_id,
                TurnCompleteEvent(reason="plan_error"),
                source=session,
                operation_id=operation_id,
                operation_scope="plan",
            )
            current = asyncio.current_task()
            if current is not None:
                setattr(current, "_crabcode_terminal_published", True)
        finally:
            for child in (forwarder, producer):
                if child and not child.done():
                    child.cancel()

    task: asyncio.Task[Any] | None = None
    rejected_task: asyncio.Task[Any] | None = None
    async with get_session_lock(ws.app.state):
        plan_tasks = _plan_tasks(ws.app.state)
        invalid = (
            getattr(ws.app.state, "gateway_closing", False)
            or ws.app.state.sessions.get(session.session_id) is not session
            or session.session_id in getattr(ws.app.state, "closing_sessions", set())
        )
        active_plan = plan_tasks.get(session.session_id)
        if isinstance(active_plan, asyncio.Task) and active_plan.done():
            plan_tasks.pop(session.session_id, None)
            active_plan = None
        plan_busy = active_plan is not None

        if not invalid and not plan_busy:
            if requested_operation_id is None:
                while operation_is_registered(
                    ws.app.state,
                    session.session_id,
                    operation_id,
                ):
                    operation_id = uuid.uuid4().hex
            elif operation_is_registered(
                ws.app.state,
                session.session_id,
                operation_id,
            ):
                duplicate_operation = True

        if not invalid and not plan_busy and not duplicate_operation:
            try:
                claim_operation(
                    ws.app.state,
                    session.session_id,
                    operation_id,
                    operation_claim,
                )
                plan_tasks[session.session_id] = operation_claim
                task = asyncio.create_task(_run())
                track_task(
                    ws.app.state,
                    session.session_id,
                    task,
                    owner_tasks=ws.scope[_WS_TASKS_KEY],
                    owner_sessions=ws.scope[_WS_TASK_SESSIONS_KEY],
                    operation_id=operation_id,
                    operation_scope="plan",
                    operation_claim=operation_claim,
                )
                plan_tasks[session.session_id] = task
                task.add_done_callback(
                    lambda done, app_state=ws.app.state, sid=session.session_id: _release_plan_task(
                        app_state,
                        sid,
                        done,
                    )
                )
            except (OperationAlreadyRegistered, RuntimeError):
                if task is not None:
                    task.cancel()
                    rejected_task = task
                    task = None
                _release_plan_task(ws.app.state, session.session_id, operation_claim)
                release_operation_claim(
                    ws.app.state,
                    session.session_id,
                    operation_id,
                    operation_claim,
                )
                duplicate_operation = True
    if rejected_task is not None:
        await asyncio.gather(rejected_task, return_exceptions=True)
    if invalid:
        await ws.send_text(json.dumps({"type": "error", "message": "session is closing"}))
        return
    if plan_busy:
        await ws.send_text(json.dumps({"type": "error", "message": "plan already executing"}))
        return
    if duplicate_operation or task is None:
        await ws.send_text(json.dumps({
            "type": "error",
            "message": f"operation already active: {operation_id}",
        }))
        return
    logger.info(
        "ws plan_action started execution session=%s operation=%s",
        session.session_id,
        operation_id,
    )


async def _handle_resume_session(ws: WebSocket, msg: dict) -> None:
    """Resume an existing session by ID and make it the active WS session."""
    import os
    from crabcode_core.session import CoreSession
    from crabcode_core.types.config import CrabCodeSettings
    from crabcode_gateway.schemas import ServerConnectedPayload

    session_id = msg.get("session_id")
    if not session_id:
        await ws.send_text(json.dumps({"type": "error", "message": "session_id required"}))
        return
    if getattr(ws.app.state, "gateway_closing", False):
        await ws.send_text(json.dumps({"type": "error", "message": "gateway shutting down"}))
        return

    # Keep the previous session intact until the requested target has been
    # resolved successfully.  Cancelling its owner tasks before loading the
    # target made a failed resume destructive: the WebSocket still pointed at
    # the old session, but its query/plan had already been interrupted.
    previous_id = ws.scope.get(_ACTIVE_SESSION_KEY)

    # Reuse an already-loaded session or load/register it atomically. The
    # shared load lock serializes expensive disk/provider work with the HTTP
    # resume route; the short registry lock only fences the in-memory map.
    resume_failed = False
    rejected = False
    session = None
    reused = False
    try:
        async with get_session_load_lock(ws.app.state):
            # Fast path and candidate construction run under the registry lock
            # only for synchronous state access. Never await provider code here.
            async with get_session_lock(ws.app.state):
                sessions: dict = ws.app.state.sessions
                if getattr(ws.app.state, "gateway_closing", False):
                    rejected = True
                elif session_id in getattr(ws.app.state, "closing_sessions", set()):
                    rejected = True
                elif session_id in sessions:
                    session = sessions[session_id]
                    if ws.app.state.default_session_id is None:
                        ws.app.state.default_session_id = session_id
                    _set_active_session(ws, session_id)
                    reused = True
                else:
                    cwd = os.getcwd()
                    current_id = ws.app.state.default_session_id
                    if current_id and current_id in sessions:
                        cwd = getattr(sessions[current_id], "cwd", cwd)
                    session = CoreSession(cwd=cwd, settings=CrabCodeSettings())

            if not rejected and not reused and session is not None:
                candidate = session

                async def _publish_background(event) -> None:
                    await ws.app.state.event_bus.publish_background(
                        candidate.session_id,
                        event,
                        source=candidate,
                    )

                candidate.set_background_event_sink(_publish_background)
                try:
                    await candidate.initialize()
                    ok = await candidate.resume(session_id)
                except BaseException:
                    try:
                        await shielded_cleanup_session(
                            ws.app.state,
                            session_id,
                            candidate,
                            owns_registry=False,
                        )
                    except Exception:
                        logger.warning(
                            "Failed to clean up failed WebSocket resume",
                            exc_info=True,
                        )
                    raise

                if not ok:
                    await shielded_cleanup_session(
                        ws.app.state,
                        session_id,
                        candidate,
                        owns_registry=False,
                    )
                    resume_failed = True
                    session = None
                else:
                    # Install atomically after load. A concurrent creator may
                    # have won through another route; discard this duplicate
                    # outside the registry lock and use the installed object.
                    discard_candidate = False
                    async with get_session_lock(ws.app.state):
                        if (
                            getattr(ws.app.state, "gateway_closing", False)
                            or session_id in getattr(ws.app.state, "closing_sessions", set())
                        ):
                            rejected = True
                            discard_candidate = True
                        else:
                            existing = ws.app.state.sessions.get(session_id)
                            if existing is None:
                                ws.app.state.event_bus.register_session(
                                    candidate.session_id,
                                    candidate,
                                )
                                ws.app.state.sessions[candidate.session_id] = candidate
                                session = candidate
                            else:
                                session = existing
                                reused = True
                                discard_candidate = True
                            if not rejected:
                                if ws.app.state.default_session_id is None:
                                    ws.app.state.default_session_id = session.session_id
                                _set_active_session(ws, session.session_id)
                    if discard_candidate:
                        try:
                            await shielded_cleanup_session(
                                ws.app.state,
                                session_id,
                                candidate,
                                owns_registry=False,
                            )
                        except Exception:
                            logger.warning(
                                "Failed to clean up discarded WebSocket resume candidate",
                                exc_info=True,
                            )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("WebSocket session resume failed", exc_info=True)
        await ws.send_text(json.dumps({"type": "error", "message": str(exc)}))
        return

    if rejected:
        await ws.send_text(json.dumps({"type": "error", "message": "gateway shutting down"}))
        return
    if resume_failed:
        await ws.send_text(json.dumps({"type": "error", "message": f"session {session_id} not found"}))
        return

    # The target is now installed/reused and the active selector has been
    # updated above.  Only then cancel work owned by the previous session on
    # this connection.  Keep tasks when resuming the same id; cancelling them
    # would interrupt an otherwise harmless reconnect/resume operation.
    if previous_id and previous_id != session_id:
        owner_tasks = ws.scope.get(_WS_TASKS_KEY, set())
        owner_sessions = ws.scope.get(_WS_TASK_SESSIONS_KEY, {})
        old_tasks = [
            task
            for task in owner_tasks
            if owner_sessions.get(task) == previous_id
        ]
        if old_tasks:
            logger.info(
                "ws resume_session cancelling previous tasks session=%s",
                previous_id,
            )
            await cancel_tasks(old_tasks)

    if reused:
        logger.info("ws resume_session reused in-memory session=%s", session_id)
        await ws.send_text(
            ServerConnectedPayload(properties={"session_id": session_id}).model_dump_json()
        )
        await _send_session_history(ws, session)
        return
    logger.info("ws resume_session loaded session=%s messages=%d", session.session_id, len(session.messages))

    await ws.send_text(
        ServerConnectedPayload(properties={"session_id": session.session_id}).model_dump_json()
    )
    await _send_session_history(ws, session)


async def _send_session_history(ws: WebSocket, session: Any) -> None:
    """Send existing conversation messages as a session_history payload."""
    messages = getattr(session, "messages", [])
    if not messages:
        return

    history_items = []
    for msg in messages:
        role = msg.role.value if hasattr(msg.role, "value") else str(msg.role)
        text = msg.text_content if hasattr(msg, "text_content") else ""
        if not text:
            continue
        history_items.append({
            "id": getattr(msg, "uuid", ""),
            "role": role,
            "text": text,
        })

    if history_items:
        await ws.send_text(json.dumps({
            "type": "session_history",
            "messages": history_items,
        }))

    # Restore context usage so the frontend meter reflects the last turn
    used = getattr(session, "last_context_used_tokens", 0) or 0
    window = getattr(session, "last_context_window_tokens", 0) or 0
    if used or window:
        remaining = max(0, window - used)
        percent = round(used / window * 100, 1) if window else 0.0
        await ws.send_text(json.dumps({
            "type": "turn_complete",
            "session_id": getattr(session, "session_id", None),
            "reason": "history_restore",
            "context_used_tokens": used,
            "context_window_tokens": window,
            "context_remaining_tokens": remaining,
            "context_used_percent": percent,
        }))
