"""Session management routes — /session/*."""

from __future__ import annotations

import os
import uuid
from copy import deepcopy
from typing import Any

from fastapi import APIRouter, HTTPException, Request

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
from crabcode_gateway.session_registry import get_session_load_lock, get_session_lock
from crabcode_gateway.task_registry import (
    cancel_operation_task,
    mark_session_closing,
    operation_is_registered,
    release_operation_claim,
    run_session_operation,
    SessionOperationRejected,
    shielded_cleanup_session,
    track_task,
)

router = APIRouter(prefix="/session", tags=["session"])


def _validate_session_id(session_id: str) -> None:
    """Reject identifiers that cannot safely address a session transcript."""
    from crabcode_core.session.storage import get_transcript_path

    try:
        # Path construction performs the storage-boundary validation without
        # touching disk.  Keep malformed client input a 400 instead of letting
        # it escape from CoreSession as an internal server error.
        get_transcript_path(os.getcwd(), session_id)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _get_session(request: Request, session_id: str | None = None):
    """Retrieve a CoreSession from app state."""
    sessions: dict = request.app.state.sessions
    # ``None`` means the selector was omitted.  An explicitly supplied empty
    # or malformed id must never silently fall back to another conversation.
    sid = (
        request.app.state.default_session_id
        if session_id is None
        else session_id
    )
    if not sid or sid not in sessions:
        return None
    if sid in getattr(request.app.state, "closing_sessions", set()):
        return None
    return sessions[sid]


def _attach_event_bus(session, event_bus: EventBus) -> None:
    async def _publish(event) -> None:
        await event_bus.publish_background(
            session.session_id,
            event,
            source=session,
        )

    session.set_background_event_sink(_publish)


@router.post("/new", response_model=SessionInfo)
async def new_session(req: NewSessionRequest, request: Request) -> SessionInfo:
    """Create a new CrabCode session."""
    import os
    from crabcode_core.session import CoreSession
    from crabcode_core.types.config import CrabCodeSettings

    if getattr(request.app.state, "gateway_closing", False):
        raise HTTPException(status_code=503, detail="Gateway is shutting down")

    cwd = req.cwd or os.getcwd()

    settings = CrabCodeSettings()
    session = None
    registered_session_id: str | None = None
    cleanup_owns_registry = False
    try:
        session = CoreSession(cwd=cwd, settings=settings)
        _attach_event_bus(session, request.app.state.event_bus)
        await session.initialize()
        session.new_session()

        async with get_session_lock(request.app.state):
            if getattr(request.app.state, "gateway_closing", False):
                raise HTTPException(status_code=503, detail="Gateway is shutting down")
            sessions: dict = request.app.state.sessions
            request.app.state.event_bus.register_session(session.session_id, session)
            sessions[session.session_id] = session
            request.app.state.default_session_id = session.session_id
            registered_session_id = session.session_id
    except BaseException:
        if registered_session_id is not None:
            async with get_session_lock(request.app.state):
                sessions = request.app.state.sessions
                if sessions.get(registered_session_id) is session:
                    cleanup_owns_registry = True
                    mark_session_closing(request.app.state, registered_session_id)
                    close_session_events = getattr(request.app.state.event_bus, "close_session", None)
                    if callable(close_session_events):
                        close_session_events(registered_session_id, session)
                    sessions.pop(registered_session_id, None)
                    if request.app.state.default_session_id == registered_session_id:
                        request.app.state.default_session_id = next(iter(sessions), None)
        if session is not None:
            try:
                await shielded_cleanup_session(
                    request.app.state,
                    getattr(session, "session_id", "") or f"unregistered:{id(session)}",
                    session,
                    owns_registry=cleanup_owns_registry,
                )
            except Exception:
                pass
        raise

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

    from crabcode_core.session.storage import SessionStorage

    if not req.session_id:
        raise HTTPException(status_code=404, detail="Session not found")
    _validate_session_id(req.session_id)
    if getattr(request.app.state, "gateway_closing", False):
        raise HTTPException(status_code=503, detail="Gateway is shutting down")

    # Fast path for an already loaded session.  The registry lock is held only
    # for in-memory state; provider/LSP initialization happens below without
    # blocking archive/send/stop.
    async with get_session_lock(request.app.state):
        sessions: dict = request.app.state.sessions
        if getattr(request.app.state, "gateway_closing", False):
            raise HTTPException(status_code=503, detail="Gateway is shutting down")
        if req.session_id in getattr(request.app.state, "closing_sessions", set()):
            raise HTTPException(status_code=503, detail="Session is shutting down")
        session = sessions.get(req.session_id)
        if session is not None:
            request.app.state.default_session_id = session.session_id

    if session is None:
        # Serialize disk-backed loads so concurrent resume calls cannot create
        # duplicate CoreSession instances for the same id.  This lock is
        # deliberately distinct from the short registry lock above.
        async with get_session_load_lock(request.app.state):
            async with get_session_lock(request.app.state):
                sessions = request.app.state.sessions
                if getattr(request.app.state, "gateway_closing", False):
                    raise HTTPException(status_code=503, detail="Gateway is shutting down")
                if req.session_id in getattr(request.app.state, "closing_sessions", set()):
                    raise HTTPException(status_code=503, detail="Session is shutting down")
                session = sessions.get(req.session_id)

            if session is None:
                resolved = SessionStorage.from_session_id(req.session_id)
                cwd = resolved.cwd if resolved is not None else os.getcwd()
                candidate = CoreSession(cwd=cwd, settings=CrabCodeSettings())
                _attach_event_bus(candidate, request.app.state.event_bus)
                try:
                    await candidate.initialize()
                    ok = await candidate.resume(req.session_id)
                except BaseException:
                    try:
                        await shielded_cleanup_session(
                            request.app.state,
                            req.session_id,
                            candidate,
                            owns_registry=False,
                        )
                    except Exception:
                        pass
                    raise
                if not ok:
                    await shielded_cleanup_session(
                        request.app.state,
                        req.session_id,
                        candidate,
                        owns_registry=False,
                    )
                    raise HTTPException(
                        status_code=404,
                        detail=f"Session {req.session_id} not found",
                    )

                # Reacquire the registry lock only for the atomic install.  A
                # concurrent archive/stop may have fenced the id while the
                # candidate was loading; in that case close it and reject.
                discard_candidate = False
                reject_candidate = False
                async with get_session_lock(request.app.state):
                    if (
                        getattr(request.app.state, "gateway_closing", False)
                        or req.session_id in getattr(request.app.state, "closing_sessions", set())
                    ):
                        discard_candidate = True
                        reject_candidate = True
                    else:
                        existing = request.app.state.sessions.get(req.session_id)
                        if existing is None:
                            request.app.state.event_bus.register_session(
                                candidate.session_id,
                                candidate,
                            )
                            request.app.state.sessions[candidate.session_id] = candidate
                            session = candidate
                        else:
                            session = existing
                            # Another resume/new request won the install race;
                            # this fully initialized candidate is no longer
                            # owned by the registry and must be closed without
                            # fencing the winner's tasks.
                            discard_candidate = True
                        request.app.state.default_session_id = session.session_id
                if discard_candidate:
                    try:
                        await shielded_cleanup_session(
                            request.app.state,
                            req.session_id,
                            candidate,
                            owns_registry=False,
                        )
                    except Exception:
                        pass
                    if reject_candidate:
                        raise HTTPException(status_code=503, detail="Gateway is shutting down")

            else:
                async with get_session_lock(request.app.state):
                    request.app.state.default_session_id = session.session_id

    # Capture the primitive count while the installed object is still fenced
    # by the registry.  Returning ``len(session.messages)`` after releasing the
    # lock could observe a replacement/close or a concurrently appended turn.
    async with get_session_lock(request.app.state):
        if (
            request.app.state.sessions.get(session.session_id) is not session
            or session.session_id in getattr(request.app.state, "closing_sessions", set())
        ):
            raise HTTPException(status_code=503, detail="Session is changing")
        message_count = len(getattr(session, "messages", ()))

    return SessionInfo(
        session_id=session.session_id,
        message_count=message_count,
        model="",
        provider="",
    )


@router.get("/messages")
async def session_messages(
    request: Request,
    session_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return the active message projection for one session.

    The selector is intentionally session-scoped: an omitted selector uses the
    legacy default session, while an explicit unknown or empty selector is a
    404 and never falls through to another conversation.  Copy the list while
    holding the registry lock so archive/resume cannot replace it halfway
    through serialization.
    """
    async with get_session_lock(request.app.state):
        session = _get_session(request, session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        # Copy message objects, not only the list container.  A query loop can
        # update an assistant message in place while it streams; serializing
        # the original objects after releasing the registry lock could return
        # a mixed/partially-mutated response.
        messages = []
        for message in getattr(session, "messages", ()):
            try:
                copier = getattr(message, "model_copy", None)
                messages.append(
                    copier(deep=True) if callable(copier) else deepcopy(message)
                )
            except Exception:
                # Lightweight integrations may expose non-copyable message
                # doubles. Keep the endpoint usable for those callers.
                messages.append(message)

    result: list[dict[str, Any]] = []
    for message in messages:
        try:
            payload = message.model_dump(mode="json")
        except (AttributeError, TypeError, ValueError):
            # Keep the endpoint useful for lightweight CoreSession doubles used
            # by integrations and tests.
            payload = {
                key: getattr(message, key)
                for key in ("uuid", "parent_uuid", "role", "content", "timestamp")
                if hasattr(message, key)
            }
        if not isinstance(payload, dict):
            continue
        role = payload.get("role")
        if hasattr(role, "value"):
            payload["role"] = role.value
        result.append(payload)
    return result


@router.get("/list", response_model=list[SessionInfo])
async def list_sessions(request: Request) -> list[SessionInfo]:
    """List all persisted sessions for the current working directory.

    Uses SessionStorage.list_sessions (disk-based) so sub-agent sessions
    are never included — only top-level user conversations appear here.
    """
    import os
    from crabcode_core.session.storage import SessionStorage

    # Determine cwd from the active session, or fall back to process cwd
    async with get_session_lock(request.app.state):
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
    event_bus: EventBus = request.app.state.event_bus

    # Fire-and-forget: run the query loop, publish events to the bus
    import asyncio

    async def _run():
        try:
            from crabcode_core.types.event import ErrorEvent, TurnCompleteEvent

            images = [img.model_dump() for img in req.images] if req.images else None
            async for event in session.send_message(req.text, max_turns=req.max_turns, images=images):
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
        except asyncio.CancelledError:
            raise
        except Exception as exc:
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

    # Resolve and register under the same app lock used by archive/stop.  This
    # closes the race where a request captured a session just before it was
    # removed and then started a query after CoreSession.close().
    async with get_session_lock(request.app.state):
        if getattr(request.app.state, "gateway_closing", False):
            raise HTTPException(status_code=503, detail="Gateway is shutting down")
        session = _get_session(request, req.session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        operation_id = req.operation_id or uuid.uuid4().hex
        if req.operation_id is None:
            while operation_is_registered(
                request.app.state,
                session.session_id,
                operation_id,
            ):
                operation_id = uuid.uuid4().hex
        elif operation_is_registered(
            request.app.state,
            session.session_id,
            operation_id,
        ):
            raise HTTPException(
                status_code=409,
                detail=f"Operation already active: {operation_id}",
            )
        task = asyncio.create_task(_run())
        track_task(
            request.app.state,
            session.session_id,
            task,
            operation_id=operation_id,
            operation_scope="foreground",
        )

    return {
        "status": "started",
        "session_id": session.session_id,
        "operation_id": operation_id,
    }


@router.post("/compact")
async def compact_session(req: CompactRequest, request: Request):
    """Manually trigger conversation compaction."""
    session = _get_session(request, req.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        accepted = await run_session_operation(
            request.app.state,
            session,
            lambda: session.compact(req.custom_instructions),
        )
    except SessionOperationRejected as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"status": "ok" if accepted else "not_compacted"}


@router.post("/interrupt")
async def interrupt_session(req: InterruptRequest, request: Request):
    """Interrupt the current query loop."""
    session = _get_session(request, req.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if req.operation_id is not None:
        cancelled = await cancel_operation_task(
            request.app.state,
            session.session_id,
            req.operation_id,
            expected_session=session,
        )
        if cancelled is None:
            raise HTTPException(status_code=404, detail="Operation not found")
        task = cancelled.task
        try:
            if not getattr(task, "_crabcode_terminal_published", False):
                from crabcode_core.types.event import TurnCompleteEvent

                await request.app.state.event_bus.publish(
                    session.session_id,
                    TurnCompleteEvent(reason="interrupted"),
                    source=session,
                    operation_id=req.operation_id,
                    operation_scope=(
                        getattr(task, "_crabcode_operation_scope", None)
                        or "foreground"
                    ),
                )
                setattr(task, "_crabcode_terminal_published", True)
        finally:
            release_operation_claim(
                request.app.state,
                session.session_id,
                req.operation_id,
                cancelled.claim,
            )
        return {
            "status": "ok",
            "operation_id": req.operation_id,
        }

    try:
        await run_session_operation(
            request.app.state,
            session,
            session.interrupt,
        )
    except SessionOperationRejected as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"status": "ok"}


@router.get("/status")
async def session_status(session_id: str | None = None, request: Request = None):
    """Return status information for the active (or specified) session."""
    async with get_session_lock(request.app.state):
        session = _get_session(request, session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        used = getattr(session, "last_context_used_tokens", 0) or 0
        window = getattr(session, "last_context_window_tokens", 0) or 0
        message_count = len(getattr(session, "messages", ()))
        model = getattr(session, "model", "")
        provider = getattr(session, "provider", "")
        mode = getattr(session, "mode", "agent")
        sid = session.session_id
    percent = round(used / window * 100, 1) if window else 0.0
    return {
        "session_id": sid,
        "message_count": message_count,
        "model": model,
        "provider": provider,
        "mode": mode,
        "context_used_tokens": used,
        "context_window_tokens": window,
        "context_used_percent": percent,
    }


@router.get("/recent", response_model=list[SessionInfo])
async def recent_sessions(limit: int = 10, request: Request = None):
    """List recently updated sessions across all projects."""
    import os
    from crabcode_core.session.storage import SessionStorage

    async with get_session_lock(request.app.state):
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
    # Serialize against disk-backed resume loads.  Otherwise archive could
    # observe an id just before resume installs it and then allow the candidate
    # to reappear after the archive operation returns.
    async with get_session_load_lock(request.app.state):
        async with get_session_lock(request.app.state):
            if not req.session_id:
                raise HTTPException(status_code=404, detail="Session not found")
            sessions: dict = request.app.state.sessions
            session = sessions.get(req.session_id)
            if session is None:
                raise HTTPException(status_code=404, detail="Session not found")
            try:
                from crabcode_core.session.meta_db import SessionMetaStore
                store = SessionMetaStore()
                try:
                    store.archive(req.session_id)
                finally:
                    store.close()
            except Exception as exc:
                raise HTTPException(status_code=500, detail=str(exc))

            # Fence the id before removing it.  Resume/send operations use this
            # same lock and therefore cannot recreate or target the session
            # while its detached tasks and CoreSession resources are drained.
            mark_session_closing(request.app.state, req.session_id)
            close_session_events = getattr(request.app.state.event_bus, "close_session", None)
            if callable(close_session_events):
                close_session_events(req.session_id, session)
            session = sessions.pop(req.session_id, None)
            request.app.state.client_contexts.pop(req.session_id, None)
            if request.app.state.default_session_id == req.session_id:
                request.app.state.default_session_id = next(iter(sessions), None)

    try:
        await shielded_cleanup_session(request.app.state, req.session_id, session)
    except Exception:
        from crabcode_core.logging_utils import get_logger
        get_logger(__name__).warning(
            "Failed to close archived session %s",
            req.session_id,
            exc_info=True,
        )
    return {"status": "ok", "session_id": req.session_id}


@router.post("/export")
async def export_session(req: ExportSessionRequest, request: Request):
    """Export a session transcript as Markdown or JSON."""
    from crabcode_core.session.export import export_json, export_markdown
    from crabcode_core.session.storage import SessionStorage, get_transcript_path

    if not req.session_id:
        raise HTTPException(status_code=404, detail="Session not found")
    _validate_session_id(req.session_id)

    async with get_session_lock(request.app.state):
        sessions: dict = request.app.state.sessions
        default_id = request.app.state.default_session_id
        active_session = _get_session(request, req.session_id)
        cwd = os.getcwd()
        default_cwd = cwd
        if default_id and default_id in sessions:
            default_cwd = getattr(sessions[default_id], "cwd", cwd)
        if active_session is not None:
            cwd = getattr(active_session, "cwd", cwd)
        else:
            active_session = None

    if active_session is None:
        # Prefer the persisted project recorded in SQLite.  This matters when
        # the requested session belongs to another project than the active one.
        resolved = SessionStorage.from_session_id(req.session_id)
        if resolved is not None:
            cwd = resolved.cwd
        elif default_id:
            cwd = default_cwd

        # SQLite may be unavailable while the JSONL transcript is still
        # present.  Check the local candidate before declaring the id missing.
        try:
            transcript = get_transcript_path(cwd, req.session_id)
            if resolved is None and not transcript.exists():
                raise HTTPException(status_code=404, detail="Session not found")
        except HTTPException:
            raise
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    async def _render_export():
        if req.format == "json":
            return (
                export_json(req.session_id, cwd),
                "application/json",
                f"session-{req.session_id[:8]}.json",
            )
        return (
            export_markdown(req.session_id, cwd),
            "text/markdown",
            f"session-{req.session_id[:8]}.md",
        )

    try:
        if active_session is not None:
            rendered = await run_session_operation(
                request.app.state,
                active_session,
                _render_export,
            )
        else:
            rendered = await _render_export()
        content, media_type, filename = rendered
    except SessionOperationRejected as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
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

    async with get_session_lock(request.app.state):
        sessions: dict = request.app.state.sessions
        default_id = request.app.state.default_session_id
        cwd = os.getcwd()
        if default_id and default_id in sessions:
            cwd = getattr(sessions[default_id], "cwd", cwd)

    try:
        store = SessionMetaStore()
        try:
            global_stats = store.stats_global()
            project_stats = store.stats_by_project(cwd)
            model_stats = store.stats_by_model()
        finally:
            store.close()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return {
        "global": global_stats,
        "project": {**project_stats, "cwd": cwd},
        "by_model": model_stats,
    }
