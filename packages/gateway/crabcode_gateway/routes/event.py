"""SSE event stream and WebSocket endpoints — /event, /ws.

Mirrors OpenCode's event.ts SSE pattern with heartbeat keep-alive,
plus a WebSocket endpoint for bidirectional communication.
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from sse_starlette.sse import EventSourceResponse

from crabcode_core.logging_utils import get_logger
from crabcode_gateway.event_bus import EventBus

logger = get_logger(__name__)

router = APIRouter(tags=["events"])


@router.get("/event")
async def event_stream(request: Request):
    """SSE endpoint for real-time event streaming.

    Clients connect here and receive a continuous stream of CoreEvent
    payloads.  Includes 10 s heartbeat to keep proxies from timing out.
    """
    event_bus: EventBus = request.app.state.event_bus
    session_id = request.query_params.get("session_id")

    async def _generate():
        async for data in event_bus.sse_stream(session_id):
            yield data

    return EventSourceResponse(_generate())


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """Bidirectional WebSocket endpoint.

    Uses a global subscription — events from ALL sessions are forwarded
    to the client (each tagged with session_id). The client filters by
    active session. This ensures no events are lost when switching sessions.
    """
    await ws.accept()
    event_bus: EventBus = ws.app.state.event_bus

    # Global subscription: receive events from all sessions
    push_task = asyncio.create_task(event_bus.ws_stream(ws, None))

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_text(json.dumps({"type": "error", "message": "invalid JSON"}))
                continue

            msg_type = msg.get("type", "")

            if msg_type == "permission_response":
                await _handle_permission_response(ws, msg)
            elif msg_type == "choice_response":
                await _handle_choice_response(ws, msg)
            elif msg_type == "send_message":
                await _handle_send_message(ws, msg)
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
            else:
                await ws.send_text(json.dumps({
                    "type": "error",
                    "message": f"unknown message type: {msg_type}",
                }))
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected")
    finally:
        push_task.cancel()
        try:
            await push_task
        except asyncio.CancelledError:
            pass


def _resolve_session(ws: WebSocket, msg: dict):
    sessions: dict = ws.app.state.sessions
    session_id = msg.get("session_id") or ws.query_params.get("session_id") or ws.app.state.default_session_id
    return sessions.get(session_id) if session_id else None


async def _handle_permission_response(ws: WebSocket, msg: dict) -> None:
    """Route a permission response from the client to the session."""
    from crabcode_core.types.event import PermissionResponseEvent

    session = _resolve_session(ws, msg)
    if not session:
        logger.warning("ws permission_response rejected: no active session")
        await ws.send_text(json.dumps({"type": "error", "message": "no active session"}))
        return

    event = PermissionResponseEvent(
        tool_use_id=msg.get("tool_use_id", ""),
        allowed=msg.get("allowed", False),
        always_allow=msg.get("always_allow", False),
        agent_id=msg.get("agent_id"),
    )
    await session.respond_permission(event)


async def _handle_choice_response(ws: WebSocket, msg: dict) -> None:
    """Route a choice response from the client to the session."""
    from crabcode_core.types.event import ChoiceResponseEvent

    session = _resolve_session(ws, msg)
    if not session:
        logger.warning("ws choice_response rejected: no active session")
        await ws.send_text(json.dumps({"type": "error", "message": "no active session"}))
        return

    event = ChoiceResponseEvent(
        tool_use_id=msg.get("tool_use_id", ""),
        selected=msg.get("selected", []),
        cancelled=msg.get("cancelled", False),
        agent_id=msg.get("agent_id"),
    )
    await session.respond_choice(event)


async def _handle_send_message(ws: WebSocket, msg: dict) -> None:
    """Start a query loop from a WebSocket message."""
    event_bus: EventBus = ws.app.state.event_bus
    session = _resolve_session(ws, msg)
    if not session:
        logger.warning("ws send_message rejected: no active session")
        await ws.send_text(json.dumps({"type": "error", "message": "no active session"}))
        return

    text = msg.get("text", "")
    max_turns = msg.get("max_turns", 0)
    images = msg.get("images")  # Optional list of {media_type, data} dicts
    logger.info(
        "ws send_message accepted session=%s chars=%d images=%d max_turns=%s",
        session.session_id,
        len(text),
        len(images) if isinstance(images, list) else 0,
        max_turns,
    )

    async def _run():
        try:
            kwargs = {"max_turns": max_turns}
            if images and isinstance(images, list):
                kwargs["images"] = images
            async for event in session.send_message(text, **kwargs):
                await event_bus.publish(session.session_id, event)
            logger.info("ws send_message completed session=%s", session.session_id)
        except Exception as exc:
            logger.exception("ws send_message failed session=%s", session.session_id)
            from crabcode_core.types.event import ErrorEvent
            await event_bus.publish(
                session.session_id,
                ErrorEvent(message=str(exc), recoverable=False, error_type="internal"),
            )

    asyncio.create_task(_run())


async def _handle_new_session(ws: WebSocket, msg: dict) -> None:
    """Create a new session and publish its id to connected clients."""
    import os
    from crabcode_core.session import CoreSession
    from crabcode_core.types.config import CrabCodeSettings
    from crabcode_gateway.schemas import ServerConnectedPayload

    cwd = msg.get("cwd") or os.getcwd()
    settings = CrabCodeSettings()
    session = CoreSession(cwd=cwd, settings=settings)
    await session.initialize()
    session.new_session()

    sessions: dict = ws.app.state.sessions
    sessions[session.session_id] = session
    ws.app.state.default_session_id = session.session_id
    logger.info("ws new_session created session=%s cwd=%s", session.session_id, cwd)

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

    logger.info("ws interrupt session=%s", session.session_id)
    await session.interrupt()


async def _handle_push_context(ws: WebSocket, msg: dict) -> None:
    """Store client-pushed context."""
    contexts: dict = ws.app.state.client_contexts
    session = _resolve_session(ws, msg)
    if session:
        contexts[session.session_id] = msg
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
    await session.initialize()
    ok = session.switch_model(name)
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
    await session.initialize()
    ok = session.set_client_permission_mode(mode)
    if not ok:
        logger.warning("ws set_permission_mode failed session=%s mode=%s", session.session_id, mode)
        await ws.send_text(
            json.dumps({"type": "error", "message": f"invalid permission mode: {mode}"}),
        )
        return
    logger.info("ws set_permission_mode session=%s mode=%s", session.session_id, mode)


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

    sessions: dict = ws.app.state.sessions

    # Reuse already-loaded session if available
    if session_id in sessions:
        session = sessions[session_id]
        ws.app.state.default_session_id = session_id
        logger.info("ws resume_session reused in-memory session=%s", session_id)
        await ws.send_text(
            ServerConnectedPayload(properties={"session_id": session_id}).model_dump_json()
        )
        await _send_session_history(ws, session)
        return

    # Load from disk
    cwd = os.getcwd()
    current_id = ws.app.state.default_session_id
    if current_id and current_id in sessions:
        cwd = getattr(sessions[current_id], "cwd", cwd)

    settings = CrabCodeSettings()
    session = CoreSession(cwd=cwd, settings=settings)
    await session.initialize()
    ok = await session.resume(session_id)
    if not ok:
        await ws.send_text(json.dumps({"type": "error", "message": f"session {session_id} not found"}))
        return

    sessions[session.session_id] = session
    ws.app.state.default_session_id = session.session_id
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

