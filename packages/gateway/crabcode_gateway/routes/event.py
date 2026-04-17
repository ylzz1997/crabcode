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

    Incoming messages are commands (permission_response, choice_response, etc.).
    Outgoing messages are CoreEvent payloads (same JSON format as SSE).

    This is the preferred transport for VSCode extensions and other
    rich clients that need two-way communication without the overhead
    of separate HTTP requests for each interaction.
    """
    await ws.accept()
    event_bus: EventBus = ws.app.state.event_bus

    session_id = ws.query_params.get("session_id")

    # Start event push task
    push_task = asyncio.create_task(event_bus.ws_stream(ws, session_id))

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
            elif msg_type == "push_context":
                await _handle_push_context(ws, msg)
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


async def _handle_permission_response(ws: WebSocket, msg: dict) -> None:
    """Route a permission response from the client to the session."""
    from crabcode_core.types.event import PermissionResponseEvent

    sessions: dict = ws.app.state.sessions
    session_id = ws.query_params.get("session_id") or ws.app.state.default_session_id

    session = sessions.get(session_id) if session_id else None
    if not session:
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

    sessions: dict = ws.app.state.sessions
    session_id = ws.query_params.get("session_id") or ws.app.state.default_session_id

    session = sessions.get(session_id) if session_id else None
    if not session:
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
    sessions: dict = ws.app.state.sessions
    event_bus: EventBus = ws.app.state.event_bus
    session_id = ws.query_params.get("session_id") or ws.app.state.default_session_id

    session = sessions.get(session_id) if session_id else None
    if not session:
        await ws.send_text(json.dumps({"type": "error", "message": "no active session"}))
        return

    text = msg.get("text", "")
    max_turns = msg.get("max_turns", 0)

    async def _run():
        try:
            async for event in session.send_message(text, max_turns=max_turns):
                await event_bus.publish(session.session_id, event)
        except Exception as exc:
            from crabcode_core.types.event import ErrorEvent
            await event_bus.publish(
                session.session_id,
                ErrorEvent(message=str(exc), recoverable=False, error_type="internal"),
            )

    asyncio.create_task(_run())


async def _handle_push_context(ws: WebSocket, msg: dict) -> None:
    """Store client-pushed context."""
    contexts: dict = ws.app.state.client_contexts
    session_id = ws.query_params.get("session_id") or ws.app.state.default_session_id
    if session_id:
        contexts[session_id] = msg
