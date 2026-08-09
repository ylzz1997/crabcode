"""Session management routes — /session/*."""

from __future__ import annotations

from fastapi import APIRouter, Request

from crabcode_gateway.schemas import (
    ArchiveSessionRequest,
    CompactRequest,
    ExportSessionRequest,
    InterruptRequest,
    NewSessionRequest,
    ResumeSessionRequest,
    SearchSessionsRequest,
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

    # Clean up empty sessions (no messages) before creating a new one
    try:
        from crabcode_core.session.storage import SessionStorage
        from crabcode_core.session.meta_db import SessionMetaStore
        import shutil

        store = SessionMetaStore()
        empty_sessions = [
            r for r in store.list_by_cwd(os.path.abspath(cwd), limit=200)
            if r.get("message_count", 0) == 0
        ]
        for r in empty_sessions:
            sid = r["id"]
            store.delete(sid)
            # Remove JSONL transcript file if it exists
            try:
                from crabcode_core.session.storage import get_transcript_path
                transcript = get_transcript_path(r.get("cwd", cwd), sid)
                if transcript.exists():
                    transcript.unlink()
            except Exception:
                pass
        store.close()
    except Exception:
        pass

    settings = CrabCodeSettings()
    session = CoreSession(cwd=cwd, settings=settings)
    await session.initialize()
    session.new_session()

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

    from crabcode_core.session.storage import SessionStorage

    resolved = SessionStorage.from_session_id(req.session_id)
    cwd = resolved.cwd if resolved is not None else os.getcwd()
    session = CoreSession(cwd=cwd, settings=CrabCodeSettings())
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
    """List all persisted sessions for the current working directory.

    Uses SessionStorage.list_sessions (disk-based) so sub-agent sessions
    are never included — only top-level user conversations appear here.
    """
    import os
    from crabcode_core.session.storage import SessionStorage

    # Determine cwd from the active session, or fall back to process cwd
    sessions: dict = request.app.state.sessions
    default_id = request.app.state.default_session_id
    cwd = os.getcwd()
    if default_id and default_id in sessions:
        cwd = getattr(sessions[default_id], "cwd", cwd)

    try:
        stored = SessionStorage.list_sessions(cwd)
    except Exception:
        stored = []

    result = []
    for s in stored:
        result.append(SessionInfo(
            session_id=s["session_id"],
            message_count=s.get("message_count", 0),
            model=s.get("model", ""),
            provider=s.get("provider", ""),
            created_at=s.get("modified", ""),
            title=s.get("title", ""),
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
            images = [img.model_dump() for img in req.images] if req.images else None
            async for event in session.send_message(req.text, max_turns=req.max_turns, images=images):
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

    accepted = await session.compact(req.custom_instructions)
    return {"status": "ok" if accepted else "not_compacted"}


@router.post("/interrupt")
async def interrupt_session(req: InterruptRequest, request: Request):
    """Interrupt the current query loop."""
    session = _get_session(request, req.session_id)
    if not session:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Session not found")

    await session.interrupt()
    return {"status": "ok"}


@router.get("/status")
async def session_status(session_id: str | None = None, request: Request = None):
    """Return status information for the active (or specified) session."""
    session = _get_session(request, session_id)
    if not session:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Session not found")

    used = getattr(session, "last_context_used_tokens", 0) or 0
    window = getattr(session, "last_context_window_tokens", 0) or 0
    percent = round(used / window * 100, 1) if window else 0.0
    return {
        "session_id": session.session_id,
        "message_count": len(session.messages),
        "model": getattr(session, "model", ""),
        "provider": getattr(session, "provider", ""),
        "mode": getattr(session, "mode", "agent"),
        "context_used_tokens": used,
        "context_window_tokens": window,
        "context_used_percent": percent,
    }


@router.get("/recent", response_model=list[SessionInfo])
async def recent_sessions(limit: int = 10, request: Request = None):
    """List recently updated sessions across all projects."""
    import os
    from crabcode_core.session.storage import SessionStorage

    sessions: dict = request.app.state.sessions
    default_id = request.app.state.default_session_id
    cwd = os.getcwd()
    if default_id and default_id in sessions:
        cwd = getattr(sessions[default_id], "cwd", cwd)

    try:
        stored = SessionStorage.list_sessions(cwd)
    except Exception:
        stored = []

    result = []
    for s in stored[:limit]:
        result.append(SessionInfo(
            session_id=s["session_id"],
            message_count=s.get("message_count", 0),
            model=s.get("model", ""),
            provider=s.get("provider", ""),
            created_at=s.get("modified", ""),
            title=s.get("title", ""),
        ))
    return result


@router.post("/search", response_model=list[SessionInfo])
async def search_sessions(req: SearchSessionsRequest, request: Request):
    """Search sessions by title or first message content."""
    from crabcode_core.session.storage import SessionStorage

    try:
        rows = SessionStorage.search_sessions(req.query, limit=req.limit)
    except Exception:
        rows = []

    return [
        SessionInfo(
            session_id=r["id"],
            message_count=r.get("message_count", 0),
            model=r.get("model", ""),
            provider=r.get("provider", ""),
            created_at=str(r.get("created_at", "")),
            title=r.get("title", ""),
        )
        for r in rows
    ]


@router.post("/archive")
async def archive_session(req: ArchiveSessionRequest, request: Request):
    """Archive a session so it no longer appears in the default list."""
    try:
        from crabcode_core.session.meta_db import SessionMetaStore
        store = SessionMetaStore()
        store.archive(req.session_id)
        store.close()
    except Exception as exc:
        from fastapi import HTTPException
        raise HTTPException(status_code=500, detail=str(exc))

    sessions: dict = request.app.state.sessions
    sessions.pop(req.session_id, None)
    if request.app.state.default_session_id == req.session_id:
        request.app.state.default_session_id = None

    return {"status": "ok", "session_id": req.session_id}


@router.post("/export")
async def export_session(req: ExportSessionRequest, request: Request):
    """Export a session transcript as Markdown or JSON."""
    import os
    from crabcode_core.session.export import export_json, export_markdown

    sessions: dict = request.app.state.sessions
    default_id = request.app.state.default_session_id
    cwd = os.getcwd()
    if req.session_id in sessions:
        cwd = getattr(sessions[req.session_id], "cwd", cwd)
    elif default_id and default_id in sessions:
        cwd = getattr(sessions[default_id], "cwd", cwd)

    try:
        if req.format == "json":
            content = export_json(req.session_id, cwd)
            media_type = "application/json"
            filename = f"session-{req.session_id[:8]}.json"
        else:
            content = export_markdown(req.session_id, cwd)
            media_type = "text/markdown"
            filename = f"session-{req.session_id[:8]}.md"
    except Exception as exc:
        from fastapi import HTTPException
        raise HTTPException(status_code=500, detail=str(exc))

    from fastapi.responses import Response
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/stats")
async def session_stats(request: Request):
    """Return usage statistics: global totals and per-project breakdown."""
    import os
    from crabcode_core.session.meta_db import SessionMetaStore

    sessions: dict = request.app.state.sessions
    default_id = request.app.state.default_session_id
    cwd = os.getcwd()
    if default_id and default_id in sessions:
        cwd = getattr(sessions[default_id], "cwd", cwd)

    try:
        store = SessionMetaStore()
        global_stats = store.stats_global()
        project_stats = store.stats_by_project(cwd)
        model_stats = store.stats_by_model()
        store.close()
    except Exception as exc:
        from fastapi import HTTPException
        raise HTTPException(status_code=500, detail=str(exc))

    return {
        "global": global_stats,
        "project": {**project_stats, "cwd": cwd},
        "by_model": model_stats,
    }
