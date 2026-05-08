"""Configuration and context routes — /config/*, /context, /tools, /skills."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from crabcode_core.config.manager import ConfigManager
from crabcode_core.skills.loader import load_skills
from crabcode_gateway.schemas import (
    ContextPushRequest,
    ModelInfo,
    SkillInfo,
    SwitchModeRequest,
    SwitchModelRequest,
    ToolInfo,
)

router = APIRouter(tags=["config"])


def _get_session(request: Request, session_id: str | None = None):
    sessions: dict = request.app.state.sessions
    sid = session_id or request.app.state.default_session_id
    if not sid or sid not in sessions:
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
async def list_models(request: Request) -> list[ModelInfo]:
    """List available named models.

    Tries the active session first; falls back to reading settings
    directly so the endpoint works even before a session is created.
    """
    session = _get_session(request)
    if session:
        return [
            ModelInfo(name=name, description=desc)
            for name, desc in session.list_models().items()
        ]
    return _list_models_from_settings()


@router.post("/config/switch-model")
async def switch_model(req: SwitchModelRequest, request: Request):
    """Switch to a named model."""
    session = _get_session(request)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    ok = session.switch_model(req.name)
    if not ok:
        raise HTTPException(status_code=400, detail=f"Model '{req.name}' not found")
    return {"status": "ok"}


@router.post("/config/switch-mode")
async def switch_mode(req: SwitchModeRequest, request: Request):
    """Switch between agent and plan mode."""
    session = _get_session(request)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    ok = session.switch_mode(req.mode)
    if not ok:
        raise HTTPException(status_code=400, detail=f"Invalid mode '{req.mode}'")
    return {"status": "ok", "mode": req.mode}


@router.get("/tools", response_model=list[ToolInfo])
async def list_tools(request: Request) -> list[ToolInfo]:
    """List all available tools."""
    session = _get_session(request)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    return [
        ToolInfo(
            name=t.name,
            description=t.description or "",
            is_read_only=t.is_read_only,
            is_enabled=t.is_enabled,
        )
        for t in session.tools
    ]


@router.get("/skills", response_model=list[SkillInfo])
async def list_skills(request: Request) -> list[SkillInfo]:
    """List all skills visible from the current working directory."""
    session = _get_session(request)
    if session and hasattr(session, "skills") and session.skills:
        return [
            SkillInfo(name=s.name, description=s.description or "")
            for s in session.skills
        ]
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
    contexts: dict = request.app.state.client_contexts
    contexts[req.session_id] = req.model_dump()
    return {"status": "ok"}


@router.get("/context/{session_id}")
async def get_context(session_id: str, request: Request):
    """Retrieve the current client-pushed context for a session."""
    contexts: dict = request.app.state.client_contexts
    if session_id not in contexts:
        return {"active_file": None, "selected_text": None, "open_files": []}
    return contexts[session_id]
