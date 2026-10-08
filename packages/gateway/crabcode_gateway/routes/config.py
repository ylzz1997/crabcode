"""Configuration and context routes — /config/*, /context, /tools, /skills."""

from __future__ import annotations

import asyncio
import codecs
import json
import os
import stat
import tempfile
import inspect
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from crabcode_core.config.manager import ConfigManager
from crabcode_core.filesystem import replace_with_retry
from crabcode_core.paths import get_config_home
from crabcode_core.skills.loader import load_skills
from crabcode_core.text_io import normalize_newlines, read_utf8_text
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
    PromptSectionInfo,
    PromptSettingsMutationRequest,
    PromptSettingsResponse,
    PromptTemplateView,
    RuntimeSettingsMutationRequest,
    RuntimeSettingsResponse,
    UserAppendPromptView,
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


class ModelTestRequest(BaseModel):
    name: str
    cwd: str | None = None


def _model_test_error(exc: Exception) -> str:
    # Do not return raw provider messages: they may contain keys, headers or URLs.
    from crabcode_core.api.network import certificate_failure
    if certificate_failure(exc):
        return "证书校验失败，请检查证书或网络拦截；未自动重试"
    status = getattr(exc, "status_code", None)
    if status:
        reason = {
            400: "请求参数或 API 格式不兼容",
            401: "认证失败，请检查 API Key",
            403: "没有访问权限，请检查密钥权限或服务区域限制",
            404: "模型或 API 地址不存在，请检查模型 ID 和 Base URL",
            429: "请求限流或额度不足，请检查服务商额度",
        }.get(status, "模型服务返回错误，请检查服务商状态与配置")
        return f"HTTP {status}：{reason}"
    name = type(exc).__name__
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)) or "Timeout" in name:
        return "测试超时（最多 30 秒），请检查网络、代理和模型服务状态"
    if "Connection" in name or isinstance(exc, OSError):
        return "连接失败，请检查 Base URL、网络、代理和 TLS 证书"
    if "credential" in str(exc).lower() or "api_key" in str(exc).lower():
        return "缺少认证信息，请检查 API Key 环境变量是否已配置，并重启 Gateway"
    if str(exc) == "Empty model response":
        return "连接已建立，但模型没有返回文本，请检查模型能力和 API 格式"
    return f"模型初始化或响应失败（{name}），请检查 Provider、API 格式和模型配置"


async def _probe_model(config: Any, cwd: str | None = None) -> None:
    from crabcode_core.api import ModelConfig, create_adapter
    from crabcode_core.usage import tracked_stream_message
    from crabcode_core.types.message import create_user_message

    adapter = None
    stream = None
    try:
        config.request_max_retries = 0
        config.max_retries = 0
        config.unbounded_connection_retries = False
        config.timeout = 30
        adapter = create_adapter(config)
        client = getattr(adapter, "client", None)
        if callable(getattr(client, "with_options", None)):
            adapter.client = client.with_options(max_retries=0, timeout=30)
        stream = tracked_stream_message(
            adapter,
            messages=[create_user_message("Reply with OK.")],
            system=[], tools=[],
            config=ModelConfig(model=config.model, max_tokens=256,
                               thinking_enabled=False, thinking_budget=0, timeout=30),
            cwd=cwd, purpose="model_test",
        )
        async for chunk in stream:
            if chunk.type == "error":
                raise RuntimeError(chunk.error or "Model stream error")
            if chunk.type == "text" and chunk.text.strip():
                return
        raise RuntimeError("Empty model response")
    finally:
        for resource in (stream, getattr(adapter, "client", None)):
            close = getattr(resource, "aclose", None) or getattr(resource, "close", None)
            if callable(close):
                try:
                    result = close()
                    if inspect.isawaitable(result):
                        await asyncio.wait_for(result, timeout=2)
                except Exception:
                    pass


@router.post("/config/test-model")
async def test_model(req: ModelTestRequest, request: Request) -> dict[str, Any]:
    cwd = _resolve_model_settings_cwd(request, req.cwd)
    settings = ConfigManager(cwd=cwd).load()
    if req.name not in settings.models:
        raise HTTPException(status_code=404, detail="模型配置不存在，请刷新模型目录")
    config = settings.get_api_config(req.name).model_copy(deep=True)
    if not config.model:
        return {"ok": False, "message": "未配置模型 ID"}
    started = time.monotonic()
    try:
        await asyncio.wait_for(_probe_model(config, cwd), timeout=30)
    except Exception as exc:
        return {"ok": False, "message": _model_test_error(exc)}
    return {"ok": True, "message": "连接成功，已收到模型回复",
            "elapsed_ms": round((time.monotonic() - started) * 1000)}


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


def _list_models_from_settings(cwd: str = ".") -> list[ModelInfo]:
    """Read model list directly from settings (works without a session)."""
    settings = ConfigManager(cwd=cwd).load()
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
    group_sources: dict[str, list[str]] = {}

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
            raw_groups_for_source = raw.get("groups")
            if isinstance(raw_groups_for_source, dict):
                for group_name in raw_groups_for_source:
                    group_sources.setdefault(str(group_name), []).append(source_path)
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
        group_sources=group_sources,
        models=entries,
        warnings=warnings,
        editable_sources=editable_sources,
    )


def _runtime_settings_from_files(
    cwd: str, pending: tuple[str, dict[str, Any]] | None = None,
) -> RuntimeSettingsResponse:
    """Read effective runtime settings by configuration layer."""
    from crabcode_core.config.manager import SETTING_SOURCES, _merge_settings
    from crabcode_core.types.config import CrabCodeSettings
    from pydantic import ValidationError

    manager = ConfigManager(cwd=cwd)
    merged: dict[str, Any] = {}
    sources: list[str] = []
    extra_tools_by_source: dict[str, list[str]] = {}

    for source_name in SETTING_SOURCES:
        raw = (
            pending[1] if pending is not None and pending[0] == source_name
            else manager.get_settings_for_source(source_name)
        )
        if raw is None:
            continue
        relevant = {
            key: raw[key]
            for key in (
                "snapshot", "computer_use", "extra_tools",
                "auto_compact_enabled", "compact_buffer_tokens", "max_context_length",
            )
            if key in raw
        }
        if not relevant:
            continue
        source_path = manager.settings_file_paths.get(source_name)
        if source_path:
            sources.append(source_path)
        raw_tools = raw.get("extra_tools")
        if isinstance(raw_tools, list):
            extra_tools_by_source[source_name] = [
                item for item in raw_tools if isinstance(item, str)
            ]
        merged = _merge_settings(merged, relevant)

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
            detail="运行与工具配置无效：" + "; ".join(messages),
        ) from exc

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

    return RuntimeSettingsResponse(
        cwd=cwd,
        snapshot_enabled=settings.snapshot.enabled,
        snapshot_max_size_mb=settings.snapshot.max_size_mb,
        auto_compact_enabled=settings.auto_compact_enabled,
        compact_buffer_tokens=settings.compact_buffer_tokens,
        max_context_length=settings.max_context_length,
        computer_use_mode=settings.computer_use.mode,
        computer_use_target_scope=settings.computer_use.target_scope,
        computer_use_delivery_policy=settings.computer_use.delivery_policy,
        extra_tools=list(settings.extra_tools),
        extra_tools_by_source=extra_tools_by_source,
        sources=sources,
        warnings=[],
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
        value = json.loads(read_utf8_text(path).text)
    except (json.JSONDecodeError, OSError, UnicodeError) as exc:
        raise HTTPException(status_code=422, detail="settings file is not valid JSON") from exc
    if not isinstance(value, dict):
        raise HTTPException(status_code=422, detail="settings file must contain a JSON object")
    return value


def _atomic_write_settings(path: Path, value: dict[str, Any]) -> None:
    payload = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
    newline = "\n"
    has_bom = False
    if path.exists():
        source = read_utf8_text(path)
        newline = source.newline or newline
        has_bom = source.has_bom
    payload = normalize_newlines(payload, newline)
    raw_payload = payload.encode("utf-8")
    if has_bom:
        raw_payload = codecs.BOM_UTF8 + raw_payload
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(raw_payload)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            temporary.chmod(stat.S_IMODE(path.stat().st_mode))
        replace_with_retry(temporary, path)
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
            if name not in models:
                raise HTTPException(status_code=404, detail=f"模型“{name}”不在所选配置层中")
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
            if name not in groups:
                raise HTTPException(status_code=404, detail=f"配置组“{name}”不在所选配置层中")
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


def _validate_extra_tool_path(tool_path: str | None) -> str:
    if not tool_path or not tool_path.strip():
        raise HTTPException(status_code=400, detail="tool_path is required")
    value = tool_path.strip()
    if any(char.isspace() for char in value) or len(value) > 240:
        raise HTTPException(status_code=400, detail="tool_path must be a compact import path")
    return value


def _mutate_runtime_settings(
    request: Request,
    req: RuntimeSettingsMutationRequest,
) -> RuntimeSettingsResponse:
    cwd = _resolve_model_settings_cwd(request, req.cwd)
    path = _settings_mutation_path(cwd, req.source)
    current = _read_settings_object(path)

    if req.action == "set_compaction":
        if req.auto_compact_enabled is not None:
            current["auto_compact_enabled"] = req.auto_compact_enabled
        if req.compact_buffer_tokens is not None:
            current["compact_buffer_tokens"] = req.compact_buffer_tokens
        if "max_context_length" in req.model_fields_set:
            # Explicit null clears the earlier threshold, including an inherited one.
            current["max_context_length"] = req.max_context_length
        _runtime_settings_from_files(cwd, pending=(req.source, current))
    elif req.action == "set_snapshot":
        snapshot = current.get("snapshot")
        if snapshot is None:
            snapshot = {}
            current["snapshot"] = snapshot
        if not isinstance(snapshot, dict):
            raise HTTPException(status_code=422, detail="snapshot must be a JSON object")
        if req.snapshot_enabled is not None:
            snapshot["enabled"] = req.snapshot_enabled
        if req.snapshot_max_size_mb is not None:
            snapshot["max_size_mb"] = req.snapshot_max_size_mb
        try:
            from crabcode_core.types.config import SnapshotSettings

            SnapshotSettings.model_validate(snapshot)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"快照配置无效：{exc}") from exc
    elif req.action in {"set_computer_use_mode", "set_computer_use_options"}:
        computer_use = current.get("computer_use")
        if computer_use is None:
            computer_use = {}
            current["computer_use"] = computer_use
        if not isinstance(computer_use, dict):
            raise HTTPException(status_code=422, detail="computer_use must be a JSON object")
        if req.computer_use_mode is not None:
            computer_use["target_scope"] = (
                "desktop" if req.computer_use_mode == "foreground_desktop" else "app_window"
            )
        if req.computer_use_target_scope is not None:
            computer_use["target_scope"] = req.computer_use_target_scope
        if req.computer_use_delivery_policy is not None:
            computer_use["delivery_policy"] = req.computer_use_delivery_policy
        if "mode" in computer_use:
            computer_use.setdefault(
                "target_scope", "desktop" if computer_use["mode"] == "foreground_desktop" else "app_window"
            )
            computer_use.pop("mode")
        try:
            from crabcode_core.types.config import ComputerUseSettings

            ComputerUseSettings.model_validate(computer_use)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"Invalid Computer Use configuration: {exc}") from exc
    else:
        tool_path = _validate_extra_tool_path(req.tool_path)
        extra_tools = current.get("extra_tools")
        if extra_tools is None:
            extra_tools = []
            current["extra_tools"] = extra_tools
        if (
            not isinstance(extra_tools, list)
            or any(not isinstance(item, str) for item in extra_tools)
        ):
            raise HTTPException(status_code=422, detail="extra_tools must be a list of import paths")
        if req.action == "add_extra_tool":
            if tool_path not in extra_tools:
                extra_tools.append(tool_path)
        else:
            extra_tools[:] = [item for item in extra_tools if item != tool_path]
            if not extra_tools:
                current.pop("extra_tools", None)

    if req.action in {"set_computer_use_mode", "set_computer_use_options"}:
        effective = _runtime_settings_from_files(cwd, pending=(req.source, current))
        if (
            effective.computer_use_target_scope == "desktop"
            and effective.computer_use_delivery_policy == "strict_background"
        ):
            raise HTTPException(
                status_code=422,
                detail="Desktop scope requires allow_foreground; strict_background supports app-window scope only.",
            )
    _atomic_write_settings(path, current)
    ConfigManager(cwd=cwd).reset_cache()
    return _runtime_settings_from_files(cwd)


def _prompt_source_map(manager: ConfigManager, key: str) -> dict[str, str]:
    from crabcode_core.config.manager import SETTING_SOURCES

    sources: dict[str, str] = {}
    for source_name in SETTING_SOURCES:
        raw = manager.get_settings_for_source(source_name) or {}
        items = raw.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"].strip():
                sources[item["id"].strip()] = source_name
    return sources


def _editable_prompt_sources(cwd: str) -> list[ModelSettingsSource]:
    manager = ConfigManager(cwd=cwd)
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
    return editable_sources


def _prompt_settings_from_files(cwd: str) -> PromptSettingsResponse:
    from crabcode_core.prompts.library import PROMPT_SECTIONS, last_by_id
    from crabcode_core.prompts.templates import DEFAULT_COMPACT_PROMPT

    manager = ConfigManager(cwd=cwd)
    settings = manager.load()
    templates = last_by_id(list(settings.prompt_templates))
    user_prompts = last_by_id(list(settings.user_append_prompts))
    template_sources = _prompt_source_map(manager, "prompt_templates")
    prompt_sources = _prompt_source_map(manager, "user_append_prompts")
    template_ids = {item.id for item in templates}
    active = settings.active_prompt_template
    warnings: list[str] = []
    if active and active not in template_ids:
        warnings.append("已选择的提示词模版不存在，已回退为默认。")
        active = None
    return PromptSettingsResponse(
        cwd=cwd,
        active_template_id=active,
        templates=[
            PromptTemplateView(
                id=item.id,
                name=item.name,
                sections=dict(item.sections),
                source=template_sources.get(item.id, "userSettings"),
            )
            for item in templates
        ],
        user_prompts=[
            UserAppendPromptView(
                id=item.id,
                text=item.text,
                enabled=item.enabled,
                source=prompt_sources.get(item.id, "userSettings"),
            )
            for item in user_prompts
        ],
        sections=[
            PromptSectionInfo(
                key=key,
                label=label,
                description=(
                    "用于手动和自动压缩，随模版生效。留空使用内置默认；/compact 的临时要求会继续追加。"
                    if key == "compact_prompt" else None
                ),
                default_text=DEFAULT_COMPACT_PROMPT if key == "compact_prompt" else None,
            )
            for key, label in PROMPT_SECTIONS
        ],
        warnings=warnings,
        editable_sources=_editable_prompt_sources(cwd),
    )


def _stored_prompt_items(current: dict[str, Any], key: str) -> list[dict[str, Any]]:
    items = current.get(key)
    if items is None:
        items = []
        current[key] = items
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise HTTPException(status_code=422, detail=f"{key} must be a list of objects")
    return items


def _clean_prompt_sections(sections: dict[str, str] | None) -> dict[str, str]:
    from crabcode_core.prompts.library import PROMPT_SECTION_KEYS

    cleaned: dict[str, str] = {}
    for key, value in (sections or {}).items():
        if key not in PROMPT_SECTION_KEYS or not isinstance(value, str):
            continue
        text = value.strip()
        if not text:
            continue
        if len(text) > 20000:
            raise HTTPException(status_code=422, detail=f"{key} is too long")
        cleaned[key] = text
    return cleaned


_PROMPT_LAYERS = ("localSettings", "projectSettings", "userSettings")


def _layers_containing_prompt(cwd: str, key: str, item_id: str) -> list[tuple[str, Path, dict[str, Any], list[Any]]]:
    found = []
    for source in _PROMPT_LAYERS:
        path = _settings_mutation_path(cwd, source)
        if not path.is_file():
            continue
        current = _read_settings_object(path)
        items = current.get(key)
        if not isinstance(items, list):
            continue
        if any(isinstance(item, dict) and item.get("id") == item_id for item in items):
            found.append((source, path, current, items))
    return found


def _remove_prompt_id(cwd: str, key: str, item_id: str, *, clear_active: bool) -> None:
    layers = _layers_containing_prompt(cwd, key, item_id)
    if not layers:
        label = "模版" if key == "prompt_templates" else "用户提示"
        raise HTTPException(status_code=404, detail=f"找不到要删除的{label}")
    for _source, path, current, items in layers:
        kept = [item for item in items if not (isinstance(item, dict) and item.get("id") == item_id)]
        if kept:
            current[key] = kept
        else:
            current.pop(key, None)
        if clear_active and current.get("active_prompt_template") == item_id:
            current["active_prompt_template"] = None
        _atomic_write_settings(path, current)


def _set_user_prompt_enabled(cwd: str, prompt_id: str, enabled: bool) -> None:
    layers = _layers_containing_prompt(cwd, "user_append_prompts", prompt_id)
    if not layers:
        raise HTTPException(status_code=404, detail="找不到要更新的用户提示")
    for _source, path, current, items in layers:
        for item in items:
            if isinstance(item, dict) and item.get("id") == prompt_id:
                item["enabled"] = enabled
        current["user_append_prompts"] = items
        _atomic_write_settings(path, current)


def _mutate_prompt_settings(
    request: Request,
    req: PromptSettingsMutationRequest,
) -> PromptSettingsResponse:
    import uuid

    from crabcode_core.types.config import PromptTemplateConfig, UserAppendPromptConfig

    cwd = _resolve_model_settings_cwd(request, req.cwd)
    path = _settings_mutation_path(cwd, req.source)
    current = _read_settings_object(path)

    if req.action == "save_template":
        name = req.template_name or ""
        if name == "默认":
            raise HTTPException(status_code=400, detail="模版名称不能是「默认」")
        sections = _clean_prompt_sections(req.sections)
        template_id = (req.template_id or "").strip() or uuid.uuid4().hex
        preview = _prompt_settings_from_files(cwd)
        if any(item.name == name and item.id != template_id for item in preview.templates):
            raise HTTPException(status_code=409, detail=f"模版「{name}」已存在")
        try:
            stored = PromptTemplateConfig(id=template_id, name=name, sections=sections).model_dump()
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"提示词模版无效：{exc}") from exc
        templates = _stored_prompt_items(current, "prompt_templates")
        if len(templates) >= 40 and all(item.get("id") != template_id for item in templates):
            raise HTTPException(status_code=422, detail="提示词模版数量已达上限")
        replaced = False
        for index, item in enumerate(templates):
            if item.get("id") == template_id:
                templates[index] = stored
                replaced = True
                break
        if not replaced:
            templates.append(stored)
        current["active_prompt_template"] = template_id
    elif req.action == "delete_template":
        _remove_prompt_id(cwd, "prompt_templates", req.template_id or "", clear_active=True)
        ConfigManager(cwd=cwd).reset_cache()
        return _prompt_settings_from_files(cwd)
    elif req.action == "set_active_template":
        template_id = req.template_id
        if template_id:
            preview = _prompt_settings_from_files(cwd)
            if template_id not in {item.id for item in preview.templates}:
                raise HTTPException(status_code=400, detail="提示词模版不存在")
        current["active_prompt_template"] = template_id
    elif req.action == "add_user_prompt":
        prompts = _stored_prompt_items(current, "user_append_prompts")
        if len(prompts) >= 80:
            raise HTTPException(status_code=422, detail="用户提示数量已达上限")
        try:
            stored = UserAppendPromptConfig(
                id=uuid.uuid4().hex,
                text=req.prompt_text or "",
                enabled=True,
            ).model_dump()
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"用户提示无效：{exc}") from exc
        prompts.append(stored)
    elif req.action == "set_user_prompt_enabled":
        _set_user_prompt_enabled(cwd, req.prompt_id or "", bool(req.enabled))
        ConfigManager(cwd=cwd).reset_cache()
        return _prompt_settings_from_files(cwd)
    else:
        _remove_prompt_id(cwd, "user_append_prompts", req.prompt_id or "", clear_active=False)
        ConfigManager(cwd=cwd).reset_cache()
        return _prompt_settings_from_files(cwd)

    _atomic_write_settings(path, current)
    ConfigManager(cwd=cwd).reset_cache()
    return _prompt_settings_from_files(cwd)


def _reload_prompt_settings_for_source(request: Request, cwd: str, source: str) -> None:
    sessions = getattr(request.app.state, "sessions", None)
    if not isinstance(sessions, dict):
        return
    changed_path = _settings_mutation_path(cwd, source).resolve()
    for session in sessions.values():
        session_cwd = getattr(session, "cwd", None)
        if not isinstance(session_cwd, str) or not session_cwd:
            continue
        session_path = ConfigManager(cwd=session_cwd).settings_file_paths.get(source)
        if not session_path or Path(session_path).resolve() != changed_path:
            continue
        reload_prompt = getattr(session, "reload_prompt_settings", None)
        if callable(reload_prompt):
            reload_prompt()


@router.get("/config/prompt-settings", response_model=PromptSettingsResponse)
async def get_prompt_settings(
    request: Request,
    cwd: str | None = None,
) -> PromptSettingsResponse:
    """Inspect prompt templates and prompts that can be appended to user input."""
    return _prompt_settings_from_files(_resolve_model_settings_cwd(request, cwd))


@router.post("/config/prompt-settings", response_model=PromptSettingsResponse)
async def mutate_prompt_settings(
    req: PromptSettingsMutationRequest,
    request: Request,
) -> PromptSettingsResponse:
    """Save a prompt template or change which user prompts are appended."""
    lock = getattr(request.app.state, "model_settings_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        request.app.state.model_settings_lock = lock
    async with lock:
        async with get_session_lock(request.app.state):
            result = _mutate_prompt_settings(request, req)
            _reload_prompt_settings_for_source(request, result.cwd, req.source)
            return result


@router.get("/config/models", response_model=list[ModelInfo])
async def list_models(
    request: Request,
    session_id: str | None = None,
    cwd: str | None = None,
) -> list[ModelInfo]:
    """List available named models.

    An explicit cwd reads that project's catalog independently of sessions.
    Legacy callers try the active session first, then process-cwd settings.
    """
    if cwd is not None:
        if session_id is not None:
            raise HTTPException(status_code=400, detail="Specify cwd or session_id, not both")
        return _list_models_from_settings(_resolve_model_settings_cwd(request, cwd))
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


@router.get("/config/runtime-settings", response_model=RuntimeSettingsResponse)
async def get_runtime_settings(
    request: Request,
    cwd: str | None = None,
) -> RuntimeSettingsResponse:
    """Inspect runtime settings and available mutation layers."""
    return _runtime_settings_from_files(_resolve_model_settings_cwd(request, cwd))


@router.post("/config/runtime-settings", response_model=RuntimeSettingsResponse)
async def mutate_runtime_settings(
    req: RuntimeSettingsMutationRequest,
    request: Request,
) -> RuntimeSettingsResponse:
    """Update runtime settings in a selected layer."""
    lock = getattr(request.app.state, "model_settings_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        request.app.state.model_settings_lock = lock
    async with lock:
        if req.action not in {"set_compaction", "set_computer_use_mode", "set_computer_use_options"}:
            return _mutate_runtime_settings(request, req)

        # Session registration uses this lock too. Keep the file write and
        # in-memory refresh together so a newly registered session cannot
        # retain settings read just before the write.
        async with get_session_lock(request.app.state):
            result = _mutate_runtime_settings(request, req)
            changed_path = _settings_mutation_path(result.cwd, req.source).resolve()
            for session in request.app.state.sessions.values():
                session_path = ConfigManager(cwd=session.cwd).settings_file_paths.get(req.source)
                if session_path and Path(session_path).resolve() == changed_path:
                    if req.action == "set_compaction":
                        session.reload_compaction_settings()
                    else:
                        session.reload_computer_use_settings()
            return result


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
                    is_loaded=(t.name in session.last_prompt_budget.get("loaded_names", [])
                               if getattr(session, "last_prompt_budget", None) else None),
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
    candidates = [get_config_home() / "gateway.log"]
    if os.name != "nt":
        candidates.append(Path("/tmp/crabcode-gateway.log"))
    for candidate in candidates:
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
