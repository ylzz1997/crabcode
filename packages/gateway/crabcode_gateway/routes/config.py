"""Configuration and context routes — /config/*, /context, /tools, /skills."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import tempfile
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
    ModelSettingsEntry,
    ModelSettingsMutationRequest,
    ModelSettingsResponse,
    ModelSettingsSource,
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
    for name in settings.models:
        cfg = settings.get_api_config(name)
        parts = []
        if cfg.provider:
            parts.append(cfg.provider)
        if cfg.model:
            parts.append(cfg.model)
        desc = "/".join(parts) if parts else "(no model set)"
        result.append(
            ModelInfo(
                name=name,
                description=desc,
                group=cfg.group or "default",
            )
        )
    return result


_MODEL_SETTING_KEYS = ("default_model", "groups", "models")
_SENSITIVE_CONFIG_KEYS = (
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
)


def _merge_model_settings(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge model settings with the same nested-object semantics as ConfigManager."""
    result = dict(base)
    for key, value in override.items():
        normalized_key = str(key).lower().replace("-", "_")
        if (
            value == "[redacted]"
            and not normalized_key.endswith("_env")
            and not normalized_key.endswith("_path")
            and any(part in normalized_key for part in _SENSITIVE_CONFIG_KEYS)
        ):
            # GET responses redact secrets. Treating that marker as an update
            # would destroy the original credential during an otherwise
            # unrelated edit, including nested headers/extra_body objects.
            continue
        current = result.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            result[key] = _merge_model_settings(current, value)
        elif isinstance(current, list) and isinstance(value, list):
            merged: list[Any] = []
            seen: set[str] = set()
            for item in current + value:
                marker = (
                    json.dumps(item, sort_keys=True)
                    if isinstance(item, dict)
                    else str(item)
                )
                if marker not in seen:
                    seen.add(marker)
                    merged.append(item)
            result[key] = merged
        else:
            result[key] = value
    return result


def _redact_model_settings(value: Any, key: str = "") -> Any:
    """Keep useful configuration visible without echoing embedded credentials."""
    normalized_key = key.lower().replace("-", "_")
    is_reference = normalized_key.endswith("_env") or normalized_key.endswith("_path")
    if key and not is_reference and any(part in normalized_key for part in _SENSITIVE_CONFIG_KEYS):
        return "[redacted]"
    if isinstance(value, dict):
        return {
            child_key: _redact_model_settings(child, str(child_key))
            for child_key, child in value.items()
        }
    if isinstance(value, list):
        return [_redact_model_settings(child) for child in value]
    return value


def _is_writable_settings_path(path: Path) -> bool:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return os.access(candidate, os.W_OK)


def _model_settings_from_files(cwd: str) -> ModelSettingsResponse:
    """Read raw model settings by layer, then resolve their effective values."""
    from crabcode_core.config.manager import SETTING_SOURCES
    from crabcode_core.types.config import CrabCodeSettings
    from pydantic import ValidationError

    manager = ConfigManager(cwd=cwd)
    merged: dict[str, Any] = {}
    sources: list[str] = []
    model_sources: dict[str, list[str]] = {}

    for source_name in SETTING_SOURCES:
        raw = manager.get_settings_for_source(source_name)
        if raw is None:
            continue
        relevant = {key: raw[key] for key in _MODEL_SETTING_KEYS if key in raw}
        if not relevant:
            continue
        source_path = manager.settings_file_paths.get(source_name)
        if source_path:
            sources.append(source_path)
            raw_models = raw.get("models")
            if isinstance(raw_models, dict):
                for model_name in raw_models:
                    model_sources.setdefault(str(model_name), []).append(source_path)
        merged = _merge_model_settings(merged, relevant)

    try:
        settings = CrabCodeSettings.model_validate(merged)
    except ValidationError as exc:
        messages = []
        for error in exc.errors(include_input=False, include_url=False)[:5]:
            location = ".".join(str(part) for part in error.get("loc", ()))
            message = str(error.get("msg", "invalid value"))
            messages.append(f"{location}: {message}" if location else message)
        raise HTTPException(
            status_code=422,
            detail="模型配置无效：" + "; ".join(messages),
        ) from exc

    raw_groups = merged.get("groups") if isinstance(merged.get("groups"), dict) else {}
    raw_models = merged.get("models") if isinstance(merged.get("models"), dict) else {}
    warnings: list[str] = []
    entries: list[ModelSettingsEntry] = []

    for name, configured_value in raw_models.items():
        configured = configured_value if isinstance(configured_value, dict) else {}
        group = configured.get("group") if isinstance(configured.get("group"), str) else None
        if group and group not in raw_groups:
            warnings.append(f"模型“{name}”引用了不存在的配置组“{group}”")
        effective = settings.get_api_config(str(name)).model_dump(exclude_none=True)
        entries.append(
            ModelSettingsEntry(
                name=str(name),
                group=group,
                is_default=str(name) == settings.default_model,
                configured=_redact_model_settings(configured),
                effective=_redact_model_settings(effective),
                overridden_fields=[str(field) for field in configured if field != "group"],
                sources=model_sources.get(str(name), []),
            )
        )

    if settings.default_model and settings.default_model not in raw_models:
        warnings.append(f"默认模型“{settings.default_model}”不存在")

    editable_sources = []
    for source_name, label in (
        ("userSettings", "用户配置"),
        ("projectSettings", "项目配置"),
        ("localSettings", "项目本地配置"),
    ):
        source_path = manager.settings_file_paths.get(source_name)
        if not source_path:
            continue
        path = Path(source_path)
        editable_sources.append(
            ModelSettingsSource(
                id=source_name,
                label=label,
                path=str(path),
                exists=path.is_file(),
                writable=_is_writable_settings_path(path),
            )
        )

    return ModelSettingsResponse(
        cwd=cwd,
        default_model=settings.default_model,
        sources=sources,
        groups=_redact_model_settings(raw_groups),
        models=entries,
        warnings=warnings,
        editable_sources=editable_sources,
    )


def _resolve_model_settings_cwd(request: Request, cwd: str | None) -> str:
    resolved_cwd = os.getcwd()
    if cwd:
        from crabcode_gateway.routes.workspace import _resolve_directory, _workspace_roots

        resolved_cwd = str(_resolve_directory(cwd, _workspace_roots(request)))
    return resolved_cwd


def _validate_model_settings_name(name: str | None) -> str:
    if not name or not name.strip():
        raise HTTPException(status_code=400, detail="name is required")
    value = name.strip()
    if value in {".", ".."} or any(char in value for char in "/\\"):
        raise HTTPException(status_code=400, detail="name must be a simple configuration name")
    if len(value) > 120:
        raise HTTPException(status_code=400, detail="name is too long")
    return value


def _settings_mutation_path(cwd: str, source: str) -> Path:
    manager = ConfigManager(cwd=cwd)
    path_str = manager.settings_file_paths.get(source)
    if not path_str or source in {"flagSettings", "policySettings"}:
        raise HTTPException(status_code=400, detail="settings source is not writable")
    path = Path(path_str)
    # Project layers must remain inside the selected workspace. User settings
    # intentionally live in the Gateway user's home directory.
    if source in {"projectSettings", "localSettings"}:
        expected_parent = (Path(cwd).resolve() / ".crabcode").resolve()
        try:
            path.parent.resolve().relative_to(expected_parent)
        except ValueError as exc:
            raise HTTPException(status_code=403, detail="settings source is outside the workspace") from exc
    return path


def _read_settings_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(errors="replace"))
    except (json.JSONDecodeError, OSError) as exc:
        raise HTTPException(status_code=422, detail="settings file is not valid JSON") from exc
    if not isinstance(value, dict):
        raise HTTPException(status_code=422, detail="settings file must contain a JSON object")
    return value


def _atomic_write_settings(path: Path, value: dict[str, Any]) -> None:
    payload = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            temporary.chmod(stat.S_IMODE(path.stat().st_mode))
        os.replace(temporary, path)
    except OSError as exc:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        raise HTTPException(status_code=403, detail="settings file is not writable") from exc


def _mutate_model_settings(request: Request, req: ModelSettingsMutationRequest) -> ModelSettingsResponse:
    cwd = _resolve_model_settings_cwd(request, req.cwd)
    path = _settings_mutation_path(cwd, req.source)
    current = _read_settings_object(path)
    action = req.action

    if action in {"upsert_model", "delete_model"}:
        name = _validate_model_settings_name(req.name)
        models = current.get("models")
        if models is None:
            models = {}
            current["models"] = models
        if not isinstance(models, dict):
            raise HTTPException(status_code=422, detail="models must be a JSON object")
        if action == "delete_model":
            models.pop(name, None)
            if not models:
                current.pop("models", None)
            if current.get("default_model") == name:
                current["default_model"] = None
        else:
            config = req.config or {}
            if not isinstance(config, dict):
                raise HTTPException(status_code=422, detail="model config must be a JSON object")
            existing = models.get(name, {})
            if not isinstance(existing, dict):
                existing = {}
            merged = _merge_model_settings(existing, config)
            for field_name in req.remove_fields:
                if isinstance(field_name, str):
                    merged.pop(field_name, None)
            try:
                from crabcode_core.types.config import ApiConfig

                ApiConfig.model_validate(merged)
            except Exception as exc:
                raise HTTPException(status_code=422, detail=f"模型配置无效：{exc}") from exc
            models[name] = merged
            if req.previous_name and req.previous_name.strip() != name:
                models.pop(_validate_model_settings_name(req.previous_name), None)
    elif action in {"upsert_group", "delete_group"}:
        name = _validate_model_settings_name(req.name)
        groups = current.get("groups")
        if groups is None:
            groups = {}
            current["groups"] = groups
        if not isinstance(groups, dict):
            raise HTTPException(status_code=422, detail="groups must be a JSON object")
        if action == "delete_group":
            groups.pop(name, None)
            if not groups:
                current.pop("groups", None)
        else:
            config = req.config or {}
            existing = groups.get(name, {})
            if not isinstance(existing, dict):
                existing = {}
            merged = _merge_model_settings(existing, config)
            for field_name in req.remove_fields:
                if isinstance(field_name, str):
                    merged.pop(field_name, None)
            try:
                from crabcode_core.types.config import ApiConfig

                ApiConfig.model_validate(merged)
            except Exception as exc:
                raise HTTPException(status_code=422, detail=f"配置组无效：{exc}") from exc
            groups[name] = merged
            if req.previous_name and req.previous_name.strip() != name:
                previous_name = _validate_model_settings_name(req.previous_name)
                groups.pop(previous_name, None)
                models = current.get("models")
                if isinstance(models, dict):
                    for model_config in models.values():
                        if isinstance(model_config, dict) and model_config.get("group") == previous_name:
                            model_config["group"] = name
    elif action == "set_default_model":
        name = _validate_model_settings_name(req.name)
        preview = _model_settings_from_files(cwd)
        if not any(model.name == name for model in preview.models):
            raise HTTPException(status_code=400, detail=f"模型“{name}”不存在")
        current["default_model"] = name
    elif action == "clear_default_model":
        # Keep an explicit null in this layer so a lower-priority default does
        # not silently become active again after the user clears it here.
        current["default_model"] = None

    _atomic_write_settings(path, current)
    ConfigManager(cwd=cwd).reset_cache()
    return _model_settings_from_files(cwd)


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
        session_settings = getattr(session, "settings", None)
        return [
            ModelInfo(
                name=name,
                description=desc,
                group=(
                    getattr(session_settings.get_api_config(name), "group", None)
                    if session_settings is not None
                    and hasattr(session_settings, "get_api_config")
                    else None
                )
                or "default",
            )
            for name, desc in models.items()
        ]
    return _list_models_from_settings()


@router.get("/config/model-settings", response_model=ModelSettingsResponse)
async def get_model_settings(
    request: Request,
    cwd: str | None = None,
) -> ModelSettingsResponse:
    """Inspect named model settings and available mutation layers."""
    return _model_settings_from_files(_resolve_model_settings_cwd(request, cwd))


@router.post("/config/model-settings", response_model=ModelSettingsResponse)
async def mutate_model_settings(
    req: ModelSettingsMutationRequest,
    request: Request,
) -> ModelSettingsResponse:
    """Create, update, or remove a named model/group configuration."""
    lock = getattr(request.app.state, "model_settings_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        request.app.state.model_settings_lock = lock
    async with lock:
        return _mutate_model_settings(request, req)


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
    cwd: str | None = None,
) -> list[ToolInfo]:
    """List enabled tools for the active session or a workspace."""
    async with get_session_lock(request.app.state):
        session = _get_session(request, session_id)
        if session_id is not None and session is None:
            raise HTTPException(status_code=404, detail="Session not found")

    if session:
        async def _list_initialized_tools() -> list[ToolInfo]:
            initializer = getattr(session, "initialize", None)
            if callable(initializer):
                await initializer()
            return [
                ToolInfo(
                    name=t.name,
                    description=t.description or "",
                    is_read_only=t.is_read_only,
                    is_enabled=t.is_enabled,
                )
                for t in session.tools
                if bool(getattr(t, "is_enabled", True))
            ]

        try:
            return await run_session_operation(
                request.app.state,
                session,
                _list_initialized_tools,
            )
        except SessionOperationRejected as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    # Plugin discovery is also used before the first chat is opened. Return
    # the built-in registry in that case; session initialization can still add
    # MCP and project-specific tools later.
    from crabcode_core.tools import get_default_tools

    return [
        ToolInfo(
            name=t.name,
            description=t.description or "",
            is_read_only=t.is_read_only,
            is_enabled=t.is_enabled,
        )
        for t in get_default_tools()
        if bool(getattr(t, "is_enabled", True))
    ]


@router.get("/skills", response_model=list[SkillInfo])
async def list_skills(
    request: Request,
    session_id: str | None = None,
    cwd: str | None = None,
) -> list[SkillInfo]:
    """List all skills visible from the current working directory."""
    async with get_session_lock(request.app.state):
        session = _get_session(request, session_id)
        if session_id is not None and session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        session_cwd = getattr(session, "cwd", None) if session else None
        if not cwd and session and hasattr(session, "skills") and session.skills:
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

    skill_cwd = cwd or session_cwd or os.getcwd()
    if cwd:
        from crabcode_gateway.routes.workspace import _resolve_directory, _workspace_roots

        skill_cwd = str(_resolve_directory(cwd, _workspace_roots(request)))
    skills = load_skills(skill_cwd)
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
