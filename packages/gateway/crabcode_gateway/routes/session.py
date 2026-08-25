"""Session management routes — /session/*."""

from __future__ import annotations

import os
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from crabcode_gateway.schemas import (
    ArchiveSessionRequest,
    ClearSessionRequest,
    CompactRequest,
    ExportSessionRequest,
    ForkSessionRequest,
    InterruptRequest,
    NewSessionRequest,
    PruneSessionsRequest,
    ResumeSessionRequest,
    SearchSessionsRequest,
    SendMessageRequest,
    SessionInfo,
    SessionRuntimeStatus,
    SearchIndexStatus,
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
    unmark_session_closing,
)

router = APIRouter(prefix="/session", tags=["session"])

_SESSION_OVERRIDE_FIELDS = (
    "model",
    "provider",
    "base_url",
    "api_format",
    "model_profile",
)


def _session_time(value: Any) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return str(value)
    return str(value or "")


def _session_info_from_row(row: dict[str, Any]) -> SessionInfo:
    session_id = str(row.get("session_id") or row.get("id") or "")
    return SessionInfo(
        session_id=session_id,
        message_count=int(row.get("message_count") or 0),
        model=str(row.get("model") or ""),
        provider=str(row.get("provider") or ""),
        created_at=_session_time(
            row.get("modified") or row.get("updated_at") or row.get("created_at")
        ),
        title=str(row.get("title") or ""),
        cwd=str(row.get("cwd") or ""),
        tokens_used=int(row.get("tokens_used") or 0),
        preview=str(row.get("preview") or row.get("first_user_message") or ""),
        forked_from_session_id=(
            str(row["forked_from_session_id"])
            if row.get("forked_from_session_id") else None
        ),
        forked_from_message_uuid=(
            str(row["forked_from_message_uuid"])
            if row.get("forked_from_message_uuid") else None
        ),
        forked_from_title=(
            str(row["forked_from_title"])
            if row.get("forked_from_title") else None
        ),
    )


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


def _resolve_session_selector(
    selector: str,
    cwd: str,
    *,
    loaded_sessions: dict[str, Any] | None = None,
) -> tuple[str, str] | None:
    """Resolve exact IDs, unique prefixes, or a local one-based index."""
    from crabcode_core.session.meta_db import SessionMetaStore
    from crabcode_core.session.storage import SessionStorage

    value = str(selector or "").strip()
    if not value:
        return None

    try:
        local_rows = SessionStorage.list_sessions(cwd)
    except Exception:
        local_rows = []

    local_ids = [
        str(row.get("session_id") or "")
        for row in local_rows
        if row.get("session_id")
    ]
    if value in local_ids:
        return value, cwd
    local_matches = [session_id for session_id in local_ids if session_id.startswith(value)]
    if len(local_matches) == 1:
        return local_matches[0], cwd
    if len(local_matches) > 1:
        return None
    try:
        index = int(value) - 1
    except ValueError:
        index = -1
    if 0 <= index < len(local_ids):
        return local_ids[index], cwd

    loaded = loaded_sessions or {}
    if value in loaded:
        session = loaded[value]
        return value, str(getattr(session, "cwd", cwd))
    loaded_matches = [session_id for session_id in loaded if session_id.startswith(value)]
    if len(loaded_matches) == 1:
        session_id = loaded_matches[0]
        return session_id, str(getattr(loaded[session_id], "cwd", cwd))
    if len(loaded_matches) > 1:
        return None

    store = SessionMetaStore()
    try:
        exact = store.get(value)
        if exact and not exact.get("is_archived"):
            return str(exact["id"]), str(exact.get("cwd") or cwd)
        matches = store.find_active_by_prefix(value, limit=2)
    finally:
        store.close()
    if len(matches) != 1:
        return None
    return str(matches[0]["id"]), str(matches[0].get("cwd") or cwd)


def _attach_event_bus(session, event_bus: EventBus) -> None:
    async def _publish(event) -> None:
        await event_bus.publish_background(
            session.session_id,
            event,
            source=session,
        )

    session.set_background_event_sink(_publish)


def _has_session_overrides(req: NewSessionRequest | ResumeSessionRequest) -> bool:
    return any(getattr(req, field) is not None for field in _SESSION_OVERRIDE_FIELDS)


def _build_session_settings(
    req: NewSessionRequest | ResumeSessionRequest,
    cwd: str,
):
    """Build caller overrides without shadowing the target project's config."""
    from crabcode_core.config.manager import ConfigManager
    from crabcode_core.types.config import CrabCodeSettings

    explicit = (
        CrabCodeSettings(
            permissions={
                "additional_directories": list(req.additional_directories),
            },
        )
        if req.additional_directories
        else CrabCodeSettings()
    )
    if req.model is not None:
        explicit.api.model = req.model
    if req.provider is not None:
        explicit.api.provider = req.provider
    if req.base_url is not None:
        explicit.api.base_url = req.base_url
    if req.api_format is not None:
        explicit.api.format = req.api_format
    if req.model_profile is not None:
        configured = ConfigManager(cwd=cwd).load().models
        if req.model_profile not in configured:
            available = ", ".join(sorted(configured)) or "none"
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Unknown model profile '{req.model_profile}'. "
                    f"Available profiles: {available}"
                ),
            )
        explicit.default_model = req.model_profile

    settings = CrabCodeSettings()
    settings._crabcode_explicit_settings = explicit
    return settings


def _session_model_provider(session) -> tuple[str, str]:
    settings = getattr(session, "settings", None)
    if settings is None:
        return "", ""
    api = settings.get_api_config() if hasattr(settings, "get_api_config") else None
    return str(getattr(api, "model", None) or ""), str(
        getattr(api, "provider", None) or ""
    )


@router.post("/new", response_model=SessionInfo)
async def new_session(req: NewSessionRequest, request: Request) -> SessionInfo:
    """Create a new CrabCode session."""
    import os
    from crabcode_core.session import CoreSession

    if getattr(request.app.state, "gateway_closing", False):
        raise HTTPException(status_code=503, detail="Gateway is shutting down")

    cwd = req.cwd or os.getcwd()

    settings = _build_session_settings(req, cwd)
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

    model, provider = _session_model_provider(session)
    meta = getattr(getattr(session, "_session_storage", None), "meta", {}) or {}
    return SessionInfo(
        session_id=session.session_id,
        message_count=0,
        model=model,
        provider=provider,
        title=str(meta.get("title") or ""),
        cwd=str(getattr(session, "cwd", "") or ""),
        forked_from_session_id=meta.get("forked_from_session_id"),
        forked_from_message_uuid=meta.get("forked_from_message_uuid"),
        forked_from_title=meta.get("forked_from_title"),
    )


@router.post("/resume", response_model=SessionInfo)
async def resume_session(req: ResumeSessionRequest, request: Request) -> SessionInfo:
    """Resume a session selected by ID, unique prefix, or local index."""
    import os
    from crabcode_core.session import CoreSession

    if not req.session_id:
        raise HTTPException(status_code=404, detail="Session not found")
    if getattr(request.app.state, "gateway_closing", False):
        raise HTTPException(status_code=503, detail="Gateway is shutting down")

    async with get_session_lock(request.app.state):
        loaded = dict(request.app.state.sessions)
        default_id = request.app.state.default_session_id
        selector_cwd = os.getcwd()
        if default_id and default_id in loaded:
            selector_cwd = str(getattr(loaded[default_id], "cwd", selector_cwd))
    try:
        resolved = _resolve_session_selector(
            req.session_id,
            selector_cwd,
            loaded_sessions=loaded,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if resolved is None:
        raise HTTPException(
            status_code=404,
            detail="Session not found or selector is ambiguous",
        )
    session_id, resolved_cwd = resolved
    _validate_session_id(session_id)

    # Fast path for an already loaded session.  The registry lock is held only
    # for in-memory state; provider/LSP initialization happens below without
    # blocking archive/send/stop.
    async with get_session_lock(request.app.state):
        sessions: dict = request.app.state.sessions
        if getattr(request.app.state, "gateway_closing", False):
            raise HTTPException(status_code=503, detail="Gateway is shutting down")
        if session_id in getattr(request.app.state, "closing_sessions", set()):
            raise HTTPException(status_code=503, detail="Session is shutting down")
        session = sessions.get(session_id)
        if session is not None:
            if _has_session_overrides(req):
                raise HTTPException(
                    status_code=409,
                    detail="Cannot apply model overrides to an already loaded session",
                )
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
                if session_id in getattr(request.app.state, "closing_sessions", set()):
                    raise HTTPException(status_code=503, detail="Session is shutting down")
                session = sessions.get(session_id)
                if session is not None and _has_session_overrides(req):
                    raise HTTPException(
                        status_code=409,
                        detail="Cannot apply model overrides to an already loaded session",
                    )

            if session is None:
                candidate = CoreSession(
                    cwd=resolved_cwd,
                    settings=_build_session_settings(req, resolved_cwd),
                )
                _attach_event_bus(candidate, request.app.state.event_bus)
                try:
                    await candidate.initialize()
                    ok = await candidate.resume(session_id)
                except BaseException:
                    try:
                        await shielded_cleanup_session(
                            request.app.state,
                            session_id,
                            candidate,
                            owns_registry=False,
                        )
                    except Exception:
                        pass
                    raise
                if not ok:
                    await shielded_cleanup_session(
                        request.app.state,
                        session_id,
                        candidate,
                        owns_registry=False,
                    )
                    raise HTTPException(
                        status_code=404,
                        detail=f"Session {session_id} not found",
                    )

                # Reacquire the registry lock only for the atomic install.  A
                # concurrent archive/stop may have fenced the id while the
                # candidate was loading; in that case close it and reject.
                discard_candidate = False
                reject_candidate = False
                async with get_session_lock(request.app.state):
                    if (
                        getattr(request.app.state, "gateway_closing", False)
                        or session_id in getattr(request.app.state, "closing_sessions", set())
                    ):
                        discard_candidate = True
                        reject_candidate = True
                    else:
                        existing = request.app.state.sessions.get(session_id)
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
                            session_id,
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

    model, provider = _session_model_provider(session)
    meta = getattr(getattr(session, "_session_storage", None), "meta", {}) or {}
    return SessionInfo(
        session_id=session.session_id,
        message_count=message_count,
        model=model,
        provider=provider,
        title=str(meta.get("title") or ""),
        cwd=str(getattr(session, "cwd", "") or ""),
        forked_from_session_id=meta.get("forked_from_session_id"),
        forked_from_message_uuid=meta.get("forked_from_message_uuid"),
        forked_from_title=meta.get("forked_from_title"),
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


@router.post("/fork", response_model=SessionInfo)
async def fork_session(req: ForkSessionRequest, request: Request) -> SessionInfo:
    """Fork a durable session from any completed assistant reply."""
    if getattr(request.app.state, "gateway_closing", False):
        raise HTTPException(status_code=503, detail="Gateway is shutting down")
    from crabcode_core.session.storage import SessionStorage
    async with get_session_lock(request.app.state):
        loaded = dict(request.app.state.sessions)
        default_id = request.app.state.default_session_id
        selector_cwd = os.getcwd()
        if default_id and default_id in loaded:
            selector_cwd = str(getattr(loaded[default_id], "cwd", selector_cwd))
        resolved = _resolve_session_selector(
            req.session_id,
            selector_cwd,
            loaded_sessions=loaded,
        )
        if resolved is None:
            raise HTTPException(status_code=404, detail="Session not found or selector is ambiguous")
        source_id, source_cwd = resolved
        source = loaded.get(source_id)
        if source is not None and (
            bool(getattr(source, "_foreground_turn_active", False))
            or bool(getattr(getattr(source, "_turn_lock", None), "locked", lambda: False)())
        ):
            raise HTTPException(status_code=409, detail="Cannot fork a session while it is running")
        try:
            forked = SessionStorage.fork_from(
                source_cwd,
                source_id,
                req.message_uuid,
                title=req.title,
            )
        except ValueError as exc:
            detail = str(exc)
            status = 404 if "not found" in detail.lower() else 400
            raise HTTPException(status_code=status, detail=detail) from exc
    return _session_info_from_row({**forked.meta, "session_id": forked.session_id})


@router.get("/list", response_model=list[SessionInfo])
async def list_sessions(
    request: Request,
    cwd: str | None = None,
) -> list[SessionInfo]:
    """List all persisted sessions for the current working directory.

    Uses SessionStorage.list_sessions (disk-based) so sub-agent sessions
    are never included — only top-level user conversations appear here.
    """
    import os
    from crabcode_core.session.storage import SessionStorage

    # Preserve the existing default-session behavior when cwd is omitted.
    selected_cwd = cwd
    if selected_cwd is None:
        async with get_session_lock(request.app.state):
            sessions: dict = request.app.state.sessions
            default_id = request.app.state.default_session_id
            selected_cwd = os.getcwd()
            if default_id and default_id in sessions:
                selected_cwd = getattr(sessions[default_id], "cwd", selected_cwd)
    else:
        candidate = Path(selected_cwd).expanduser()
        try:
            candidate = candidate.resolve(strict=True)
        except OSError as exc:
            raise HTTPException(status_code=404, detail="Working directory not found") from exc
        if not candidate.is_dir():
            raise HTTPException(status_code=400, detail="cwd must be a directory")
        selected_cwd = str(candidate)

    try:
        stored = SessionStorage.list_sessions(selected_cwd)
    except Exception:
        stored = []

    return [_session_info_from_row({**s, "cwd": selected_cwd}) for s in stored]


@router.get("/resolve", response_model=SessionInfo)
async def resolve_session(selector: str, request: Request) -> SessionInfo:
    """Resolve a session ID, unique prefix, or local list index."""
    from crabcode_core.session.meta_db import SessionMetaStore
    from crabcode_core.session.storage import SessionStorage

    async with get_session_lock(request.app.state):
        loaded = dict(request.app.state.sessions)
        default_id = request.app.state.default_session_id
        cwd = os.getcwd()
        if default_id and default_id in loaded:
            cwd = str(getattr(loaded[default_id], "cwd", cwd))
    try:
        resolved = _resolve_session_selector(selector, cwd, loaded_sessions=loaded)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if resolved is None:
        raise HTTPException(status_code=404, detail="Session not found or selector is ambiguous")
    session_id, resolved_cwd = resolved

    session = loaded.get(session_id)
    if session is not None:
        current_name = getattr(session, "_current_model_name", None)
        settings = getattr(session, "settings", None)
        active_config = (
            settings.get_api_config(current_name)
            if settings is not None and hasattr(settings, "get_api_config")
            else None
        )
        session_meta = getattr(getattr(session, "_session_storage", None), "meta", {}) or {}
        return SessionInfo(
            session_id=session_id,
            message_count=len(getattr(session, "messages", ())),
            model=str(getattr(active_config, "model", "") or ""),
            provider=str(getattr(active_config, "provider", "") or ""),
            cwd=resolved_cwd,
            title=str(session_meta.get("title") or ""),
            forked_from_session_id=session_meta.get("forked_from_session_id"),
            forked_from_message_uuid=session_meta.get("forked_from_message_uuid"),
            forked_from_title=session_meta.get("forked_from_title"),
        )

    store = SessionMetaStore()
    try:
        row = store.get(session_id)
    finally:
        store.close()
    if row is not None:
        return _session_info_from_row(row)
    local = next(
        (
            item
            for item in SessionStorage.list_sessions(resolved_cwd)
            if item.get("session_id") == session_id
        ),
        None,
    )
    if local is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return _session_info_from_row({**local, "cwd": resolved_cwd})


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
            from crabcode_core.types.event import TurnCompleteEvent

            images = [img.model_dump() for img in req.images] if req.images else None
            pending_terminal: TurnCompleteEvent | None = None
            async for event in session.send_message(req.text, max_turns=req.max_turns, images=images):
                if isinstance(event, TurnCompleteEvent):
                    # A steering message can extend the same Core generator
                    # after query_loop produced a candidate terminal.  Publish
                    # only the candidate that remains when the generator ends.
                    pending_terminal = event
                    continue
                pending_terminal = None
                await event_bus.publish(
                    session.session_id,
                    event,
                    source=session,
                    operation_id=operation_id,
                    operation_scope="foreground",
                )
            if pending_terminal is None:
                # A transport adapter may finish without Core's explicit
                # terminal event (for example after yielding a recoverable
                # error).  Synthesize one so HTTP/SSE and WebSocket clients
                # share the same foreground lifecycle contract.
                pending_terminal = TurnCompleteEvent(reason="error")
            await event_bus.publish(
                session.session_id,
                pending_terminal,
                source=session,
                operation_id=operation_id,
                operation_scope="foreground",
            )
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
            from crabcode_core.types.event import TurnCompleteEvent
            await event_bus.publish(
                session.session_id,
                TurnCompleteEvent(reason="error"),
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


@router.post("/clear")
async def clear_session(req: ClearSessionRequest, request: Request):
    """Clear and durably persist the active conversation projection."""
    session = _get_session(request, req.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        cleared = await run_session_operation(
            request.app.state,
            session,
            session.clear_history,
        )
    except SessionOperationRejected as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"status": "ok", "messages_cleared": cleared}


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


@router.get("/status", response_model=SessionRuntimeStatus)
async def session_status(
    request: Request,
    session_id: str | None = None,
) -> SessionRuntimeStatus:
    """Return the complete non-secret runtime status for one session."""
    async with get_session_lock(request.app.state):
        session = _get_session(request, session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        messages = list(getattr(session, "messages", ()))
        initialized = bool(getattr(session, "_initialized", False))
        current_name = getattr(session, "_current_model_name", None)
        settings = getattr(session, "settings", None)
        active_config = None
        if settings is not None and hasattr(settings, "get_api_config"):
            active_config = settings.get_api_config(current_name)
            model = active_config.model or ""
            provider = active_config.provider or ""
        else:
            # Retain compatibility with lightweight integration doubles and
            # older session implementations that expose flat attributes.
            model = getattr(session, "model", "")
            provider = getattr(session, "provider", "")
        mode = getattr(session, "agent_mode", getattr(session, "mode", "agent"))
        reasoning_effort = getattr(session, "reasoning_effort", None)
        ultra_mode = bool(getattr(session, "ultra_mode", False))
        permission_mode = getattr(session, "client_permission_mode", "default")
        sid = session.session_id

        enabled_tools = None
        if initialized:
            enabled_tools = sum(
                1 for tool in getattr(session, "tools", ())
                if bool(getattr(tool, "is_enabled", True))
            )

        try:
            agents = list(session.list_agents())
        except (AttributeError, RuntimeError):
            agents = []
        try:
            monitors = list(session.list_monitor_tasks())
        except (AttributeError, RuntimeError):
            monitors = []

        used = max(0, int(getattr(session, "last_context_used_tokens", 0) or 0))
        window = max(0, int(getattr(session, "last_context_window_tokens", 0) or 0))
        if not used and messages:
            from crabcode_core.compact.compact import estimate_token_count

            used = max(0, int(estimate_token_count(messages)))
        if not window and settings is not None:
            from crabcode_core.api.model_info import (
                DEFAULT_CONTEXT_WINDOW,
                lookup_context_window,
            )

            window = max(
                0,
                int(
                    getattr(settings, "max_context_length", None)
                    or getattr(active_config, "context_window", None)
                    or lookup_context_window(getattr(active_config, "model", None))
                    or DEFAULT_CONTEXT_WINDOW
                ),
            )

        search_index = None
        extra_tools = list(getattr(settings, "extra_tools", ()) or ())
        if "crabcode_search.CodebaseSearchTool" in extra_tools:
            try:
                from crabcode_search.background import read_background_status

                raw_search = read_background_status(getattr(session, "cwd", os.getcwd()))
            except Exception:
                raw_search = None
            if isinstance(raw_search, dict):
                def _optional_int(key: str) -> int | None:
                    value = raw_search.get(key)
                    return value if isinstance(value, int) and not isinstance(value, bool) else None

                search_index = SearchIndexStatus(
                    state=str(raw_search.get("state") or "unknown"),
                    chunks=_optional_int("chunks"),
                    files=_optional_int("files"),
                    done=_optional_int("done"),
                    total=_optional_int("total"),
                )
            else:
                search_index = SearchIndexStatus(state="waiting")

        agent_settings = getattr(settings, "agent", None)
        result = SessionRuntimeStatus(
            session_id=sid,
            cwd=str(getattr(session, "cwd", "") or ""),
            initialized=initialized,
            message_count=len(messages),
            model=model,
            model_profile=current_name,
            provider=provider,
            mode=mode if mode in {"agent", "plan"} else "agent",
            reasoning_effort=reasoning_effort,
            ultra_mode=ultra_mode,
            permission_mode=permission_mode,
            context_used_tokens=used,
            context_window_tokens=window,
            context_remaining_tokens=max(0, window - used),
            context_used_percent=round(used / window * 100, 1) if window else 0.0,
            compact_count=max(0, int(getattr(session, "compact_count", 0) or 0)),
            auto_compact_enabled=bool(
                getattr(settings, "auto_compact_enabled", True)
            ),
            thinking_enabled=bool(getattr(active_config, "thinking_enabled", False)),
            max_tokens=max(0, int(getattr(active_config, "max_tokens", 0) or 0)),
            tool_count=enabled_tools,
            agent_total=len(agents),
            agent_active=sum(
                1 for item in agents
                if getattr(item, "status", "") in {"queued", "running"}
            ),
            agent_failed=sum(
                1 for item in agents if getattr(item, "status", "") == "failed"
            ),
            agent_pending_callbacks=sum(
                1 for item in agents
                if bool(getattr(item, "callback_enabled", False))
                and getattr(item, "callback_state", "") in {"pending", "injected"}
            ),
            agent_max_concurrency=max(
                0,
                int(getattr(agent_settings, "max_concurrency", 0) or 0),
            ),
            monitor_total=len(monitors),
            monitor_active=sum(
                1 for item in monitors if getattr(item, "status", "") == "running"
            ),
            monitor_failed=sum(
                1 for item in monitors if getattr(item, "status", "") == "failed"
            ),
            search_index=search_index,
        )
    return result


@router.get("/recent", response_model=list[SessionInfo])
async def recent_sessions(limit: int = 10, request: Request = None):
    """List recently updated sessions across all projects."""
    from crabcode_core.session.meta_db import SessionMetaStore

    try:
        store = SessionMetaStore()
        try:
            rows = store.list_recent(limit=max(1, min(200, limit)))
        finally:
            store.close()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return [_session_info_from_row(row) for row in rows]


@router.post("/search", response_model=list[SessionInfo])
async def search_sessions(req: SearchSessionsRequest, request: Request):
    """Search sessions by title or first message content."""
    from crabcode_core.session.storage import SessionStorage

    try:
        rows = SessionStorage.search_sessions(req.query, limit=req.limit)
    except Exception:
        rows = []

    return [_session_info_from_row(row) for row in rows]


@router.post("/archive")
async def archive_session(req: ArchiveSessionRequest, request: Request):
    """Archive a loaded or persisted session selected by ID/prefix/index."""
    from crabcode_core.session.meta_db import SessionMetaStore
    from crabcode_core.session.storage import SessionStorage

    if not req.session_id:
        raise HTTPException(status_code=404, detail="Session not found")

    session = None
    resolved_session_id = ""
    async with get_session_load_lock(request.app.state):
        async with get_session_lock(request.app.state):
            sessions: dict = request.app.state.sessions
            loaded = dict(sessions)
            default_id = request.app.state.default_session_id
            cwd = os.getcwd()
            if default_id and default_id in sessions:
                cwd = str(getattr(sessions[default_id], "cwd", cwd))

        try:
            resolved = _resolve_session_selector(
                req.session_id,
                cwd,
                loaded_sessions=loaded,
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        if resolved is None:
            raise HTTPException(
                status_code=404,
                detail="Session not found or selector is ambiguous",
            )
        resolved_session_id, resolved_cwd = resolved
        _validate_session_id(resolved_session_id)

        async with get_session_lock(request.app.state):
            if resolved_session_id in getattr(
                request.app.state,
                "closing_sessions",
                set(),
            ):
                raise HTTPException(status_code=503, detail="Session is shutting down")
            session = request.app.state.sessions.get(resolved_session_id)
            mark_session_closing(request.app.state, resolved_session_id)

        archive_committed = False
        try:
            store = SessionMetaStore()
            try:
                row = store.get(resolved_session_id)
                if row is not None:
                    store.archive(resolved_session_id)
                else:
                    SessionStorage(
                        resolved_cwd,
                        resolved_session_id,
                    ).persist_archive_marker()
            finally:
                store.close()
            archive_committed = True
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        finally:
            if not archive_committed:
                async with get_session_lock(request.app.state):
                    unmark_session_closing(
                        request.app.state,
                        resolved_session_id,
                    )

        async with get_session_lock(request.app.state):
            sessions = request.app.state.sessions
            session = sessions.get(resolved_session_id)
            if session is not None:
                close_session_events = getattr(
                    request.app.state.event_bus,
                    "close_session",
                    None,
                )
                if callable(close_session_events):
                    close_session_events(resolved_session_id, session)
                sessions.pop(resolved_session_id, None)
            request.app.state.client_contexts.pop(resolved_session_id, None)
            if request.app.state.default_session_id == resolved_session_id:
                request.app.state.default_session_id = next(iter(sessions), None)

    if session is not None:
        try:
            await shielded_cleanup_session(
                request.app.state,
                resolved_session_id,
                session,
            )
        except Exception:
            from crabcode_core.logging_utils import get_logger
            get_logger(__name__).warning(
                "Failed to close archived session %s",
                resolved_session_id,
                exc_info=True,
            )
    else:
        unmark_session_closing(request.app.state, resolved_session_id)
    return {"status": "ok", "session_id": resolved_session_id}


@router.post("/prune")
async def prune_sessions(req: PruneSessionsRequest, request: Request):
    """Archive stale inactive sessions and optionally purge their artifacts.

    Loaded sessions are always excluded.  The load lock prevents a concurrent
    resume from installing one of the selected sessions between discovery and
    its durable archive/purge transition.
    """
    from crabcode_core.session.meta_db import SessionMetaStore
    from crabcode_core.session.storage import purge_session_artifacts

    if getattr(request.app.state, "gateway_closing", False):
        raise HTTPException(status_code=503, detail="Gateway is shutting down")

    async with get_session_load_lock(request.app.state):
        async with get_session_lock(request.app.state):
            if getattr(request.app.state, "gateway_closing", False):
                raise HTTPException(status_code=503, detail="Gateway is shutting down")
            loaded_ids = set(request.app.state.sessions)

        store = SessionMetaStore()
        try:
            archived = store.auto_archive(
                days=req.days,
                exclude_ids=loaded_ids,
            )
            purged = 0
            failed: list[str] = []
            if req.delete_files:
                candidates = store.purge_archived(
                    delete_rows=False,
                    exclude_ids=loaded_ids,
                )
                for entry in candidates:
                    session_id = str(entry.get("id") or "")
                    cwd = str(entry.get("cwd") or "")
                    if not session_id:
                        continue
                    try:
                        if cwd:
                            purge_session_artifacts(cwd, session_id)
                        store.delete(session_id)
                        purged += 1
                    except (OSError, ValueError):
                        failed.append(session_id)
        finally:
            store.close()

    return {
        "archived": archived,
        "purged": purged,
        "failed": failed,
        "skipped_loaded": len(loaded_ids),
    }


@router.post("/export")
async def export_session(req: ExportSessionRequest, request: Request):
    """Export a session selected by ID, unique prefix, or local index."""
    from crabcode_core.session.export import export_json, export_markdown
    from crabcode_core.session.meta_db import SessionMetaStore
    from crabcode_core.session.storage import SessionStorage, get_transcript_path

    if not req.session_id:
        raise HTTPException(status_code=404, detail="Session not found")

    async with get_session_lock(request.app.state):
        sessions: dict = request.app.state.sessions
        loaded = dict(sessions)
        default_id = request.app.state.default_session_id
        selector_cwd = os.getcwd()
        if default_id and default_id in sessions:
            selector_cwd = str(getattr(sessions[default_id], "cwd", selector_cwd))

    try:
        resolved_selector = _resolve_session_selector(
            req.session_id,
            selector_cwd,
            loaded_sessions=loaded,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if resolved_selector is None:
        # Archived sessions are omitted from normal selectors, but a caller
        # holding the exact ID must still be able to export the retained audit
        # transcript.  Prefixes and indexes intentionally remain active-only.
        exact_id = req.session_id.strip()
        try:
            _validate_session_id(exact_id)
        except HTTPException:
            raise
        store = SessionMetaStore()
        try:
            archived_row = store.get(exact_id)
        finally:
            store.close()
        archived_cwd = (
            str(archived_row.get("cwd") or selector_cwd)
            if archived_row is not None and archived_row.get("is_archived")
            else selector_cwd
        )
        transcript = get_transcript_path(archived_cwd, exact_id)
        if not transcript.exists() or (
            archived_row is not None and not archived_row.get("is_archived")
        ):
            raise HTTPException(
                status_code=404,
                detail="Session not found or selector is ambiguous",
            )
        resolved_selector = (exact_id, archived_cwd)
    session_id, cwd = resolved_selector
    _validate_session_id(session_id)

    async with get_session_lock(request.app.state):
        active_session = _get_session(request, session_id)
        if active_session is not None:
            cwd = str(getattr(active_session, "cwd", cwd))

    if active_session is None:
        # Prefer the persisted project recorded in SQLite.  This matters when
        # the requested session belongs to another project than the active one.
        resolved_storage = SessionStorage.from_session_id(session_id)
        if resolved_storage is not None:
            cwd = resolved_storage.cwd

        # SQLite may be unavailable while the JSONL transcript is still
        # present.  Check the local candidate before declaring the id missing.
        try:
            transcript = get_transcript_path(cwd, session_id)
            if resolved_storage is None and not transcript.exists():
                raise HTTPException(status_code=404, detail="Session not found")
        except HTTPException:
            raise
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    async def _render_export():
        if req.format == "json":
            return (
                export_json(session_id, cwd),
                "application/json",
                f"session-{session_id[:8]}.json",
            )
        return (
            export_markdown(session_id, cwd),
            "text/markdown",
            f"session-{session_id[:8]}.md",
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
