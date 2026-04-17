"""Session management routes — /session/*."""

from __future__ import annotations

from fastapi import APIRouter, Request

from crabcode_gateway.schemas import (
    CompactRequest,
    InterruptRequest,
    NewSessionRequest,
    ResumeSessionRequest,
    SendMessageRequest,
    SessionInfo,
)
from crabcode_gateway.event_bus import EventBus

router = APIRouter(prefix="/session", tags=["session"])


def _get_session(request: Request, session_id: str | None = None):
    """Retrieve a CoreSession from app state."""
    sessions: dict = request.app.state.sessions
    sid = session_id or request.app.state.default_session_id
    if not sid or sid not in sessions:
        return None
    return sessions[sid]


@router.post("/new", response_model=SessionInfo)
async def new_session(req: NewSessionRequest, request: Request) -> SessionInfo:
    """Create a new CrabCode session."""
    import os
    from crabcode_core.session import CoreSession
    from crabcode_core.types.config import CrabCodeSettings

    cwd = req.cwd or os.getcwd()
    settings = CrabCodeSettings()
    session = CoreSession(cwd=cwd, settings=settings)
    await session.initialize()

    sessions: dict = request.app.state.sessions
    sessions[session.session_id] = session
    request.app.state.default_session_id = session.session_id

    return SessionInfo(
        session_id=session.session_id,
        message_count=0,
        model="",
        provider="",
    )


@router.post("/resume", response_model=SessionInfo)
async def resume_session(req: ResumeSessionRequest, request: Request) -> SessionInfo:
    """Resume an existing session by ID."""
    import os
    from crabcode_core.session import CoreSession
    from crabcode_core.types.config import CrabCodeSettings

    sessions: dict = request.app.state.sessions

    # If already loaded, return it
    if req.session_id in sessions:
        s = sessions[req.session_id]
        return SessionInfo(
            session_id=s.session_id,
            message_count=len(s.messages),
            model="",
            provider="",
        )

    session = CoreSession(cwd=os.getcwd(), settings=CrabCodeSettings())
    await session.initialize()
    ok = await session.resume(req.session_id)
    if not ok:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"Session {req.session_id} not found")

    sessions[session.session_id] = session
    request.app.state.default_session_id = session.session_id

    return SessionInfo(
        session_id=session.session_id,
        message_count=len(session.messages),
        model="",
        provider="",
    )


@router.get("/list", response_model=list[SessionInfo])
async def list_sessions(request: Request) -> list[SessionInfo]:
    """List all active sessions."""
    sessions: dict = request.app.state.sessions
    result = []
    for sid, s in sessions.items():
        result.append(SessionInfo(
            session_id=sid,
            message_count=len(s.messages),
            model="",
            provider="",
        ))
    return result


@router.post("/send")
async def send_message(req: SendMessageRequest, request: Request):
    """Send a message and stream back CoreEvents as SSE.

    This is the primary interaction endpoint.  It starts the query
    loop and streams events via the event bus SSE channel.
    """
    session = _get_session(request, req.session_id)
    if not session:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Session not found")

    event_bus: EventBus = request.app.state.event_bus

    # Fire-and-forget: run the query loop, publish events to the bus
    import asyncio

    async def _run():
        try:
            async for event in session.send_message(req.text, max_turns=req.max_turns):
                await event_bus.publish(session.session_id, event)
        except Exception as exc:
            from crabcode_core.types.event import ErrorEvent
            await event_bus.publish(
                session.session_id,
                ErrorEvent(message=str(exc), recoverable=False, error_type="internal"),
            )

    asyncio.create_task(_run())

    return {"status": "started", "session_id": session.session_id}


@router.post("/compact")
async def compact_session(req: CompactRequest, request: Request):
    """Manually trigger conversation compaction."""
    session = _get_session(request, req.session_id)
    if not session:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Session not found")

    await session.compact()
    return {"status": "ok"}


@router.post("/interrupt")
async def interrupt_session(req: InterruptRequest, request: Request):
    """Interrupt the current query loop."""
    session = _get_session(request, req.session_id)
    if not session:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Session not found")

    await session.interrupt()
    return {"status": "ok"}
