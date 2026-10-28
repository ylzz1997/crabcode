"""Safe local mutations for the named model and group catalog."""

from __future__ import annotations

import codecs
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from crabcode_core.api.model_discovery import model_import_config
from crabcode_core.config.manager import ConfigManager, SETTING_SOURCES
from crabcode_core.filesystem import replace_with_retry
from crabcode_core.text_io import normalize_newlines, read_utf8_text
from crabcode_core.types.config import ApiConfig


CatalogKind = Literal["model", "group"]
WritableSettingsSource = Literal["userSettings", "projectSettings", "localSettings"]

WRITABLE_SETTINGS_SOURCES: tuple[WritableSettingsSource, ...] = (
    "projectSettings",
    "localSettings",
    "userSettings",
)

SETTINGS_SOURCE_LABELS: dict[str, str] = {
    "userSettings": "User · global",
    "projectSettings": "Project · shared",
    "localSettings": "Project · local",
    "flagSettings": "CLI flags · read-only",
    "policySettings": "Managed policy · read-only",
}


class ModelCatalogError(ValueError):
    """A catalog mutation could not be completed safely."""


@dataclass(frozen=True)
class CatalogDefinition:
    kind: CatalogKind
    name: str
    source: str
    path: Path
    config: dict[str, Any]


@dataclass(frozen=True)
class CatalogMutationResult:
    action: Literal["add", "delete"]
    kind: CatalogKind
    name: str
    source: WritableSettingsSource
    path: Path
    cleared_default: bool = False


@dataclass(frozen=True)
class CatalogImportResult:
    group: str
    names: tuple[str, ...]
    source: WritableSettingsSource
    path: Path


def group_model_entries(
    group_name: str,
    group: ApiConfig,
    model_ids: list[str],
    model_metadata: dict[str, dict[str, Any]],
    *,
    existing_ids: set[str],
    used_names: set[str],
) -> dict[str, dict[str, Any]]:
    """Prepare a batch with the same inheritance and aliases for every client."""
    if not model_ids or len(model_ids) > 1000:
        raise ModelCatalogError("Select between 1 and 1000 models to import.")
    if any(
        not isinstance(value, str) or not value.strip() or len(value) > 512
        or any(ord(char) < 32 for char in value)
        for value in model_ids
    ):
        raise ModelCatalogError("The model list contains an invalid model ID.")
    entries: dict[str, dict[str, Any]] = {}
    used_names = set(used_names)
    for model_id in dict.fromkeys(value.strip() for value in model_ids):
        if model_id in existing_ids:
            continue
        # Profile names cannot contain slashes; the API ID remains exact.
        stem = re.sub(r"[/\\\x00-\x1f]", "-", model_id)[:120].strip()
        if stem in {".", ".."}:
            stem = "model"
        candidate = stem
        if candidate in used_names:
            stem = f"{group_name} - {stem}"[:120]
            candidate = stem
        suffix = 2
        while candidate in used_names:
            tail = f" ({suffix})"
            candidate = stem[:120 - len(tail)] + tail
            suffix += 1
        settings = model_import_config(model_metadata.get(model_id, {}), group)
        entries[candidate] = {"group": group_name, "model": model_id, **settings}
        used_names.add(candidate)
    return entries


def import_group_models(
    cwd: str,
    source: WritableSettingsSource,
    group_name: str,
    model_ids: list[str],
    model_metadata: dict[str, dict[str, Any]],
) -> CatalogImportResult:
    """Save a whole batch atomically, preserving other layers and defaults."""
    group_name = validate_catalog_name(group_name)
    settings = ConfigManager(cwd=cwd).load()
    group = settings.groups.get(group_name)
    if group is None:
        raise ModelCatalogError("The configuration group no longer exists. Fetch the list again.")
    path = settings_source_path(cwd, source)
    current = _read_settings_object(path)
    models = current.setdefault("models", {})
    if not isinstance(models, dict):
        raise ModelCatalogError(f'"models" in {path} must be a JSON object.')
    entries = group_model_entries(
        group_name, group, model_ids, model_metadata,
        existing_ids={config.model for config in settings.models.values() if config.group == group_name and config.model},
        used_names=catalog_names(cwd, "model") | set(models),
    )
    if entries:
        models.update(entries)
        _atomic_write_settings(path, current)
    return CatalogImportResult(group_name, tuple(entries), source, path)


def validate_catalog_name(name: str) -> str:
    """Validate the simple names accepted by the Gateway settings editor."""
    value = name.strip()
    if not value:
        raise ModelCatalogError("Name is required.")
    if value in {".", ".."} or any(char in value for char in "/\\"):
        raise ModelCatalogError("Name must be a simple configuration name without / or \\.")
    if len(value) > 120:
        raise ModelCatalogError("Name must be 120 characters or fewer.")
    return value


def settings_source_path(cwd: str, source: WritableSettingsSource) -> Path:
    manager = ConfigManager(cwd=cwd)
    path_value = manager.settings_file_paths.get(source)
    if not path_value:
        raise ModelCatalogError(f"Settings source is unavailable: {source}")
    path = Path(path_value)
    if source in {"projectSettings", "localSettings"}:
        expected_parent = (Path(cwd).resolve() / ".crabcode").resolve()
        try:
            path.parent.resolve().relative_to(expected_parent)
        except ValueError as exc:
            raise ModelCatalogError("Project settings path is outside the workspace.") from exc
    return path


def list_catalog_definitions(
    cwd: str,
    kind: CatalogKind | None = None,
    *,
    writable_only: bool = False,
) -> list[CatalogDefinition]:
    """Return raw definitions, retaining the layer each definition belongs to."""
    manager = ConfigManager(cwd=cwd)
    sources = (
        list(WRITABLE_SETTINGS_SOURCES)
        if writable_only
        else list(reversed(SETTING_SOURCES))
    )
    definitions: list[CatalogDefinition] = []
    kinds: tuple[CatalogKind, ...] = (kind,) if kind else ("model", "group")
    for source in sources:
        raw = manager.get_settings_for_source(source)
        path_value = manager.settings_file_paths.get(source)
        if raw is None or not path_value:
            continue
        for entry_kind in kinds:
            values = raw.get("models" if entry_kind == "model" else "groups")
            if not isinstance(values, dict):
                continue
            for name, config in values.items():
                if not isinstance(config, dict):
                    continue
                definitions.append(
                    CatalogDefinition(
                        kind=entry_kind,
                        name=str(name),
                        source=source,
                        path=Path(path_value),
                        config=dict(config),
                    )
                )
    return definitions


def catalog_names(cwd: str, kind: CatalogKind) -> set[str]:
    return {definition.name for definition in list_catalog_definitions(cwd, kind)}


def referencing_models(cwd: str, group_name: str) -> list[str]:
    """List effective named models that still point at a group."""
    settings = ConfigManager(cwd=cwd).load()
    return sorted(name for name, config in settings.models.items() if config.group == group_name)


def add_catalog_entry(
    cwd: str,
    source: WritableSettingsSource,
    kind: CatalogKind,
    name: str,
    config: dict[str, Any],
) -> CatalogMutationResult:
    """Add a new named entry without overwriting any existing layer."""
    name = validate_catalog_name(name)
    if name in catalog_names(cwd, kind):
        raise ModelCatalogError(
            f'{kind.title()} "{name}" already exists in the merged configuration.'
        )
    if not isinstance(config, dict):
        raise ModelCatalogError("Configuration must be a JSON object.")
    if kind == "group" and "group" in config:
        raise ModelCatalogError("A group configuration cannot inherit from another group.")
    try:
        ApiConfig.model_validate(config)
    except Exception as exc:
        raise ModelCatalogError(f"Invalid {kind} configuration: {exc}") from exc

    path = settings_source_path(cwd, source)
    current = _read_settings_object(path)
    key = "models" if kind == "model" else "groups"
    values = current.get(key)
    if values is None:
        values = {}
        current[key] = values
    if not isinstance(values, dict):
        raise ModelCatalogError(f'"{key}" in {path} must be a JSON object.')
    if name in values:
        raise ModelCatalogError(f'{kind.title()} "{name}" already exists in {path}.')
    values[name] = dict(config)
    _atomic_write_settings(path, current)
    return CatalogMutationResult("add", kind, name, source, path)


def delete_catalog_entry(
    cwd: str,
    source: WritableSettingsSource,
    kind: CatalogKind,
    name: str,
) -> CatalogMutationResult:
    """Delete exactly one raw definition from one writable settings layer."""
    name = validate_catalog_name(name)
    path = settings_source_path(cwd, source)
    current = _read_settings_object(path)
    key = "models" if kind == "model" else "groups"
    values = current.get(key)
    if not isinstance(values, dict) or name not in values:
        raise ModelCatalogError(f'{kind.title()} "{name}" is not defined in {path}.')
    values.pop(name)
    if not values:
        current.pop(key, None)

    cleared_default = kind == "model" and current.get("default_model") == name
    if cleared_default:
        # An explicit null prevents a lower-precedence default from silently
        # becoming active when its selected model may no longer exist.
        current["default_model"] = None

    _atomic_write_settings(path, current)
    return CatalogMutationResult(
        "delete", kind, name, source, path, cleared_default=cleared_default
    )


def _read_settings_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(read_utf8_text(path).text)
    except (json.JSONDecodeError, OSError, UnicodeError) as exc:
        raise ModelCatalogError(f"Settings file is not valid UTF-8 JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ModelCatalogError(f"Settings file must contain a JSON object: {path}")
    return value


def _atomic_write_settings(path: Path, value: dict[str, Any]) -> None:
    payload = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
    newline = "\n"
    has_bom = False
    if path.exists():
        try:
            source = read_utf8_text(path)
        except (OSError, UnicodeError) as exc:
            raise ModelCatalogError(f"Could not read settings file: {path}") from exc
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
        raise ModelCatalogError(f"Settings file is not writable: {path}") from exc
