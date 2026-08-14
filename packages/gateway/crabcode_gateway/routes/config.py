"""Configuration and context routes — /config/*, /context, /tools, /skills."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from crabcode_core.config.manager import ConfigManager
from crabcode_core.skills.loader import load_skills
from crabcode_gateway.session_registry import get_session_lock
from crabcode_gateway.schemas import (
    ContextPushRequest,
    GoalRequest,
    GoalState,
    ModelInfo,
    SetReasoningEffortRequest,
    SetUltraModeRequest,
    SkillInfo,
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
        mode = getattr(session, "mode", "agent")
        plan = getattr(session, "current_plan", None)
        if isinstance(plan, dict):
            plan = dict(plan)
    return {
        "mode": mode,
        "in_plan_mode": mode == "plan",
        "plan": plan,
    }


@router.get("/logs")
async def get_logs(lines: int = 100, request: Request = None):
    """Return recent gateway log lines."""
    import logging

    for h in logging.getLogger().handlers:
        if hasattr(h, "buffer") or hasattr(h, "stream"):
            break

    # Try to read from crabcode log file if available
    import os
    log_candidates = [
        os.path.expanduser("~/.crabcode/gateway.log"),
        "/tmp/crabcode-gateway.log",
    ]
    for path in log_candidates:
        if os.path.exists(path):
            try:
                with open(path) as f:
                    all_lines = f.readlines()
                return {"lines": [line.rstrip() for line in all_lines[-lines:]]}
            except Exception:
                pass

    return {"lines": [], "note": "No log file found. Logs are written to stderr."}
