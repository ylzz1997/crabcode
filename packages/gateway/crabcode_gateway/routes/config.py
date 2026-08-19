"""Configuration and context routes — /config/*, /context, /tools, /skills."""

from __future__ import annotations

import asyncio
import json
import os
import stat
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from crabcode_core.config.manager import ConfigManager
from crabcode_core.skills.loader import load_skills
from crabcode_gateway.session_registry import get_session_lock
from crabcode_gateway.schemas import (
    ContextPushRequest,
    GoalRequest,
    GoalState,
    ModelInfo,
    SetPermissionModeRequest,
    SetReasoningEffortRequest,
    SetUltraModeRequest,
    SkillExpandRequest,
    SkillExpansion,
    SkillInfo,
    LogsResponse,
    SwitchModeRequest,
    SwitchModelRequest,
    ToolInfo,
)
from crabcode_gateway.task_registry import SessionOperationRejected, run_session_operation

router = APIRouter(tags=["config"])


async def _switch_model(session: Any, name: str) -> bool:
    return bool(session.switch_model(name))


async def _switch_mode(session: Any, mode: str) -> bool:
    return bool(session.switch_mode(mode))


async def _set_reasoning_effort(session: Any, effort: str) -> bool:
    await session.initialize()
    return bool(session.set_reasoning_effort(effort))


async def _set_ultra_mode(session: Any, enabled: bool | None) -> bool:
    await session.initialize()
    return bool(session.set_ultra_mode(enabled))


async def _set_permission_mode(session: Any, mode: str) -> bool:
    await session.initialize()
    return bool(session.set_client_permission_mode(mode))


async def _manage_goal(session: Any, req: GoalRequest) -> dict[str, Any] | None:
    action = req.action
    if action in {"set", "edit"}:
        if not req.objective or not req.objective.strip():
            raise ValueError("objective is required for set/edit")
        if action == "set":
            goal = session.create_goal(
                req.objective,
                token_budget=req.token_budget,
            )
        elif "token_budget" in req.model_fields_set:
            goal = session.edit_goal(
                req.objective,
                token_budget=req.token_budget,
            )
        else:
            goal = session.edit_goal(req.objective)
        return goal.to_dict()
    if action == "clear":
        session.clear_goal()
        return None
    status = {
        "pause": "paused",
        "resume": "active",
    }.get(action, action)
    return session.update_goal(status).to_dict()


async def _store_context(request: Request, session: Any, req: ContextPushRequest) -> None:
    contexts: dict = request.app.state.client_contexts
    contexts[session.session_id] = req.model_dump()


def _get_session(request: Request, session_id: str | None = None):
    sessions: dict = request.app.state.sessions
    # ``None`` means the caller omitted a selector and may use the legacy
    # process default.  An explicitly supplied (even malformed) id is
    # authoritative and must never fall through to another conversation.
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


def _list_models_from_settings() -> list[ModelInfo]:
    """Read model list directly from settings (works without a session)."""
    settings = ConfigManager().get()
    result: list[ModelInfo] = []
    for name, cfg in settings.models.items():
        parts = []
        if cfg.provider:
            parts.append(cfg.provider)
        if cfg.model:
            parts.append(cfg.model)
        desc = "/".join(parts) if parts else "(no model set)"
        result.append(ModelInfo(name=name, description=desc))
    return result


@router.get("/config/models", response_model=list[ModelInfo])
async def list_models(
    request: Request,
    session_id: str | None = None,
) -> list[ModelInfo]:
    """List available named models.

    Tries the active session first; falls back to reading settings
    directly so the endpoint works even before a session is created.
    """
    async with get_session_lock(request.app.state):
        session = _get_session(request, session_id)
        if session_id is not None and session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        if session:
            models = dict(session.list_models())
        else:
            models = None
    if models is not None:
        return [
            ModelInfo(name=name, description=desc)
            for name, desc in models.items()
        ]
    return _list_models_from_settings()


@router.post("/config/switch-model")
async def switch_model(req: SwitchModelRequest, request: Request):
    """Switch to a named model."""
    session = _get_session(request, req.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        ok = await run_session_operation(
            request.app.state,
            session,
            lambda: _switch_model(session, req.name),
        )
    except SessionOperationRejected as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not ok:
        raise HTTPException(status_code=400, detail=f"Model '{req.name}' not found")
    return {"status": "ok"}


@router.post("/config/switch-mode")
async def switch_mode(req: SwitchModeRequest, request: Request):
    """Switch between agent and plan mode."""
    session = _get_session(request, req.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        ok = await run_session_operation(
            request.app.state,
            session,
            lambda: _switch_mode(session, req.mode),
        )
    except SessionOperationRejected as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not ok:
        raise HTTPException(status_code=400, detail=f"Invalid mode '{req.mode}'")
    return {"status": "ok", "mode": req.mode}


@router.post("/config/reasoning-effort")
async def set_reasoning_effort(req: SetReasoningEffortRequest, request: Request):
    """Set the active session's reasoning effort for subsequent requests."""
    session = _get_session(request, req.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        ok = await run_session_operation(
            request.app.state,
            session,
            lambda: _set_reasoning_effort(session, req.effort),
        )
    except SessionOperationRejected as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not ok:
        raise HTTPException(status_code=400, detail=f"Invalid reasoning effort '{req.effort}'")
    return {"status": "ok", "reasoning_effort": session.reasoning_effort}


@router.post("/config/ultra-mode")
async def set_ultra_mode(req: SetUltraModeRequest, request: Request):
    """Set ultra mode, or toggle it when ``enabled`` is omitted."""
    session = _get_session(request, req.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        enabled = await run_session_operation(
            request.app.state,
            session,
            lambda: _set_ultra_mode(session, req.enabled),
        )
    except SessionOperationRejected as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"status": "ok", "ultra_mode": enabled}


@router.post("/config/permission-mode")
async def set_permission_mode(req: SetPermissionModeRequest, request: Request):
    """Set the per-client tool permission override for a session."""
    session = _get_session(request, req.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        ok = await run_session_operation(
            request.app.state,
            session,
            lambda: _set_permission_mode(session, req.mode),
        )
    except SessionOperationRejected as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not ok:
        raise HTTPException(status_code=400, detail=f"Invalid permission mode '{req.mode}'")
    return {"status": "ok", "permission_mode": getattr(session, "client_permission_mode", req.mode)}


@router.get("/config/goal", response_model=GoalState)
async def get_goal(request: Request, session_id: str | None = None) -> GoalState:
    """Return the current session goal."""
    async with get_session_lock(request.app.state):
        session = _get_session(request, session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        goal = session.get_goal()
        data = goal.to_dict() if goal is not None else None
    return GoalState(goal=data)


@router.post("/config/goal", response_model=GoalState)
async def manage_goal(req: GoalRequest, request: Request) -> GoalState:
    """Set, edit, pause, resume, finish, block, or clear a session goal."""
    session = _get_session(request, req.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        data = await run_session_operation(
            request.app.state,
            session,
            lambda: _manage_goal(session, req),
        )
    except SessionOperationRejected as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return GoalState(goal=data)


@router.get("/tools", response_model=list[ToolInfo])
async def list_tools(
    request: Request,
    session_id: str | None = None,
) -> list[ToolInfo]:
    """List all available tools."""
    async with get_session_lock(request.app.state):
        session = _get_session(request, session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        tools = [
            ToolInfo(
                name=t.name,
                description=t.description or "",
                is_read_only=t.is_read_only,
                is_enabled=t.is_enabled,
            )
            for t in session.tools
        ]
    return tools


@router.get("/skills", response_model=list[SkillInfo])
async def list_skills(
    request: Request,
    session_id: str | None = None,
) -> list[SkillInfo]:
    """List all skills visible from the current working directory."""
    async with get_session_lock(request.app.state):
        session = _get_session(request, session_id)
        if session_id is not None and session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        if session and hasattr(session, "skills") and session.skills:
            skills = [
                SkillInfo(name=s.name, description=s.description or "")
                for s in session.skills
            ]
        else:
            skills = None
    if skills is not None:
        return skills
    # Fallback: load from cwd when no session is active yet
    import os

    cwd = os.getcwd()
    skills = load_skills(cwd)
    return [SkillInfo(name=s.name, description=s.description or "") for s in skills]


@router.post("/skills/expand", response_model=SkillExpansion)
async def expand_skill(req: SkillExpandRequest, request: Request) -> SkillExpansion:
    """Expand a slash-invoked skill deterministically, matching the CLI."""
    async with get_session_lock(request.app.state):
        session = _get_session(request, req.session_id)
        if req.session_id is not None and session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        skills = list(getattr(session, "skills", ())) if session else []
        cwd = getattr(session, "cwd", None) if session else None

    if not skills:
        import os

        skills = load_skills(cwd or os.getcwd())
    skill = next((item for item in skills if item.name == req.name), None)
    if skill is None:
        raise HTTPException(status_code=404, detail=f"Skill {req.name} not found")

    prompt = skill.content
    user_input = req.user_input.strip()
    if user_input:
        if "$USER_INPUT" in prompt:
            prompt = prompt.replace("$USER_INPUT", user_input)
        else:
            prompt = f"{prompt}\n\nUser input: {user_input}"
    return SkillExpansion(name=skill.name, prompt=prompt)


@router.post("/context")
async def push_context(req: ContextPushRequest, request: Request):
    """Push workspace context from a client (e.g. VSCode extension).

    The gateway stores this per-session so that it can be injected
    into the system prompt or tool context as needed.
    """
    session = _get_session(request, req.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        await run_session_operation(
            request.app.state,
            session,
            lambda: _store_context(request, session, req),
        )
    except SessionOperationRejected as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"status": "ok"}


@router.get("/context/{session_id}")
async def get_context(session_id: str, request: Request):
    """Retrieve the current client-pushed context for a session."""
    async with get_session_lock(request.app.state):
        contexts: dict = request.app.state.client_contexts
        sessions: dict = request.app.state.sessions
        if (
            session_id not in sessions
            or session_id in getattr(request.app.state, "closing_sessions", set())
        ):
            raise HTTPException(status_code=404, detail="Session not found")
        context = contexts.get(session_id)
        if context is not None:
            # Context payloads are plain dictionaries but may contain nested
            # client-owned lists; copy the outer mapping so archive/updates do
            # not mutate the response while it is serialized.
            context = dict(context)
    if context is None:
        return {"active_file": None, "selected_text": None, "open_files": []}
    return context


@router.get("/config/plan-status")
async def plan_status(request: Request):
    """Return the current plan mode status and plan content if available."""
    async with get_session_lock(request.app.state):
        session = _get_session(request, request.query_params.get("session_id"))
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        mode = getattr(session, "agent_mode", getattr(session, "mode", "agent"))
        plan = getattr(session, "current_plan", None)
        if isinstance(plan, dict):
            plan = dict(plan)
    return {
        "mode": mode,
        "in_plan_mode": mode == "plan",
        "plan": plan,
    }


def _logs_cwd(request: Request, session_id: str | None) -> Path:
    sessions = getattr(request.app.state, "sessions", {})
    sid = getattr(request.app.state, "default_session_id", None) if session_id is None else session_id
    session = sessions.get(sid) if sid else None
    if (
        session is not None
        and sid not in getattr(request.app.state, "closing_sessions", set())
        and not getattr(request.app.state, "gateway_closing", False)
    ):
        return Path(getattr(session, "cwd", os.getcwd())).resolve()
    if session_id is not None:
        raise HTTPException(status_code=404, detail="Session not found")
    return Path(os.getcwd()).resolve()


def _discover_logs(cwd: Path) -> dict[str, Path]:
    """Read the shared log index used by core and background tools."""
    result: dict[str, Path] = {}
    lexical_root = cwd / ".crabcode" / "logs"
    try:
        logs_root = lexical_root.resolve()
        # A repository-controlled symlink must not turn the log index into a
        # capability for files outside the dedicated project log directory.
        root_is_safe = logs_root == lexical_root.absolute()
    except OSError:
        logs_root = lexical_root
        root_is_safe = False

    raw: Any = {}
    if root_is_safe:
        index_path = logs_root / "index.json"
        try:
            with _open_regular_log(index_path, os.O_RDONLY) as handle:
                raw = json.load(handle)
        except (OSError, json.JSONDecodeError):
            raw = {}
    if isinstance(raw, dict):
        for name, value in raw.items():
            if not (
                isinstance(name, str)
                and 0 < len(name) <= 64
                and all(char.isalnum() or char in "._-" for char in name)
                and isinstance(value, str)
            ):
                continue
            candidate = Path(value).expanduser()
            if not candidate.is_absolute():
                candidate = logs_root / candidate
            try:
                resolved = candidate.resolve(strict=True)
                if resolved.parent != logs_root or candidate.is_symlink():
                    continue
                with _open_regular_log(resolved, os.O_RDONLY):
                    pass
            except OSError:
                continue
            result[name] = resolved
    # Keep compatibility with older search versions that wrote this path
    # without registering it in the shared index.
    legacy = cwd / ".crabcode" / "search" / "background.log"
    safe_legacy = _known_log_path(legacy)
    if safe_legacy is not None:
        result.setdefault("search", safe_legacy)
    # Gateway startup logs are useful even before a CoreSession exists.
    for candidate in (Path.home() / ".crabcode" / "gateway.log", Path("/tmp/crabcode-gateway.log")):
        safe_candidate = _known_log_path(candidate)
        if safe_candidate is not None:
            result.setdefault("gateway", safe_candidate)
    return result


def _open_regular_log(path: Path, flags: int):
    """Open one regular, single-link log without following a final symlink."""
    open_flags = flags | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, open_flags)
    try:
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink != 1:
            raise OSError("Log path is not a single-link regular file")
        mode = "r" if flags == os.O_RDONLY else "w"
        return os.fdopen(fd, mode, encoding="utf-8", errors="replace")
    except BaseException:
        os.close(fd)
        raise


def _known_log_path(path: Path) -> Path | None:
    """Validate a fixed, application-owned log location."""
    try:
        if path.is_symlink():
            return None
        resolved = path.resolve(strict=True)
        with _open_regular_log(resolved, os.O_RDONLY):
            pass
        return resolved
    except OSError:
        return None


def _tail_log(path: Path, count: int) -> tuple[list[str], bool]:
    try:
        with _open_regular_log(path, os.O_RDONLY) as handle:
            all_lines = handle.read().splitlines()
    except OSError:
        return [], False
    return all_lines[-count:], len(all_lines) > count


def _clear_log(path: Path) -> None:
    # Open without O_TRUNC, validate the descriptor, then truncate that exact
    # inode.  This avoids truncating a swapped symlink before validation.
    with _open_regular_log(path, os.O_WRONLY) as handle:
        os.ftruncate(handle.fileno(), 0)


@router.get("/logs", response_model=LogsResponse)
async def get_logs(
    request: Request,
    lines: int = 100,
    tail: int | None = None,
    name: str | None = None,
    clear: bool = False,
    session_id: str | None = None,
) -> LogsResponse:
    """List logs or read/clear a named log, matching the CLI surface."""
    cwd = _logs_cwd(request, session_id)
    logs = _discover_logs(cwd)
    if not name:
        if clear:
            raise HTTPException(status_code=400, detail="name is required when clear=true")
        entries = []
        for key, path in sorted(logs.items()):
            try:
                updated = datetime.fromtimestamp(path.stat().st_mtime).isoformat()
            except OSError:
                updated = None
            state = None
            if key == "search":
                status_path = cwd / ".crabcode" / "search" / "background-status.json"
                try:
                    raw_status = json.loads(status_path.read_text(encoding="utf-8"))
                    state = raw_status.get("state") if isinstance(raw_status, dict) else None
                except (OSError, json.JSONDecodeError):
                    pass
            entries.append({"name": key, "path": str(path), "updated_at": updated, "state": state})
        return LogsResponse(logs=entries)

    path = logs.get(name)
    if path is None:
        raise HTTPException(status_code=404, detail=f"Unknown log: {name}")
    if clear:
        try:
            _clear_log(path)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"Failed to clear log: {exc}") from exc
    count = max(1, min(10_000, int(tail if tail is not None else lines)))
    body, truncated = _tail_log(path, count)
    return LogsResponse(
        name=name,
        path=str(path),
        lines=body,
        truncated=truncated,
        note="Log is empty" if not body else None,
    )


@router.get("/logs/follow")
async def follow_log(
    request: Request,
    name: str,
    session_id: str | None = None,
) -> StreamingResponse:
    """Stream appended lines from a named log as server-sent events."""
    cwd = _logs_cwd(request, session_id)
    path = _discover_logs(cwd).get(name)
    if path is None:
        raise HTTPException(status_code=404, detail=f"Unknown log: {name}")

    async def _generate():
        try:
            with _open_regular_log(path, os.O_RDONLY) as handle:
                position = os.fstat(handle.fileno()).st_size
        except OSError:
            position = 0
        while True:
            if await request.is_disconnected():
                break
            try:
                with _open_regular_log(path, os.O_RDONLY) as handle:
                    current_size = os.fstat(handle.fileno()).st_size
                    if current_size < position:
                        # The file was cleared or rotated while following it.
                        position = 0
                    handle.seek(position)
                    chunk = handle.readlines()
                    position = handle.tell()
            except OSError:
                chunk = []
            for line in chunk:
                yield f"data: {json.dumps(line.rstrip(chr(10)), ensure_ascii=False)}\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(_generate(), media_type="text/event-stream")
