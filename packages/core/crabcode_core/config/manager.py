"""Configuration manager — 5-layer settings merge with Pydantic validation."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from crabcode_core.logging_utils import get_logger
from crabcode_core.types.config import (
    CrabCodeSettings,
    GatewaySecuritySettings,
    GatewayWorkspaceSettings,
)

logger = get_logger(__name__)


SETTING_SOURCES = [
    "userSettings",
    "projectSettings",
    "localSettings",
    "flagSettings",
    "policySettings",
]


def _merge_settings(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deep merge two settings dicts. Arrays are concatenated and deduped."""
    result = deepcopy(base)
    for key, value in override.items():
        if key in result:
            if isinstance(result[key], list) and isinstance(value, list):
                seen: set[str] = set()
                merged: list[Any] = []
                for item in result[key] + value:
                    item_key = json.dumps(item, sort_keys=True) if isinstance(item, dict) else str(item)
                    if item_key not in seen:
                        seen.add(item_key)
                        merged.append(item)
                result[key] = merged
            elif isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = _merge_settings(result[key], value)
            else:
                result[key] = deepcopy(value)
        else:
            result[key] = deepcopy(value)
    return result


class ConfigManager:
    """Manages CrabCode settings from multiple sources.

    Merge order (later overrides earlier):
        userSettings -> projectSettings -> localSettings -> flagSettings -> policySettings
    """

    def __init__(
        self,
        cwd: str = ".",
        flag_settings_path: str | None = None,
    ):
        self._cwd = cwd
        self._flag_settings_path = flag_settings_path
        self._cache: CrabCodeSettings | None = None

    @property
    def settings_file_paths(self) -> dict[str, str | None]:
        home = Path.home() / ".crabcode"
        project = Path(self._cwd).resolve()
        return {
            "userSettings": str(home / "settings.json"),
            "projectSettings": str(project / ".crabcode" / "settings.json"),
            "localSettings": str(project / ".crabcode" / "settings.local.json"),
            "flagSettings": self._flag_settings_path,
            "policySettings": str(home / "managed-settings.json"),
        }

    def load(self) -> CrabCodeSettings:
        """Load and merge all settings layers."""
        merged: dict[str, Any] = {}

        for source in SETTING_SOURCES:
            path_str = self.settings_file_paths.get(source)
            if not path_str:
                continue

            path = Path(path_str)
            if not path.exists():
                continue

            try:
                raw = json.loads(path.read_text(errors="replace"))
                if isinstance(raw, dict):
                    merged = _merge_settings(merged, raw)
            except (json.JSONDecodeError, OSError):
                logger.warning("Failed to load settings source: %s", path, exc_info=True)
                continue

        try:
            self._cache = CrabCodeSettings.model_validate(merged)
        except Exception:
            logger.exception("Failed to validate merged settings; using defaults")
            self._cache = CrabCodeSettings()

        return self._cache

    def get(self) -> CrabCodeSettings:
        """Get cached settings or load from disk."""
        if self._cache is None:
            return self.load()
        return self._cache

    def reset_cache(self) -> None:
        """Clear the cached settings."""
        self._cache = None

    def load_gateway_security(
        self,
        overrides: dict[str, Any] | None = None,
    ) -> GatewaySecuritySettings:
        """Load process-level gateway security without project overrides.

        A repository must not be able to weaken authentication for a gateway
        launched from that repository. CLI overrides sit between user and
        managed policy settings, matching the normal precedence model.
        """
        merged: dict[str, Any] = {}
        for source in ("userSettings",):
            raw = self.get_settings_for_source(source) or {}
            gateway = raw.get("gateway", {})
            security = gateway.get("security", {}) if isinstance(gateway, dict) else {}
            if isinstance(security, dict):
                merged = _merge_settings(merged, security)
        if overrides:
            merged = _merge_settings(merged, overrides)
        raw_policy = self.get_settings_for_source("policySettings") or {}
        policy_gateway = raw_policy.get("gateway", {})
        policy_security = (
            policy_gateway.get("security", {})
            if isinstance(policy_gateway, dict)
            else {}
        )
        if isinstance(policy_security, dict):
            merged = _merge_settings(merged, policy_security)
        return GatewaySecuritySettings.model_validate(merged)

    def load_gateway_workspace(self) -> GatewayWorkspaceSettings:
        """Load filesystem discovery policy without project overrides."""
        merged: dict[str, Any] = {}
        raw_user = self.get_settings_for_source("userSettings") or {}
        user_gateway = raw_user.get("gateway", {})
        user_workspace = (
            user_gateway.get("workspace", {})
            if isinstance(user_gateway, dict)
            else {}
        )
        if isinstance(user_workspace, dict):
            merged = _merge_settings(merged, user_workspace)

        raw_policy = self.get_settings_for_source("policySettings") or {}
        policy_gateway = raw_policy.get("gateway", {})
        policy_workspace = (
            policy_gateway.get("workspace", {})
            if isinstance(policy_gateway, dict)
            else {}
        )
        if isinstance(policy_workspace, dict):
            merged = _merge_settings(merged, policy_workspace)
        return GatewayWorkspaceSettings.model_validate(merged)

    def get_settings_for_source(self, source: str) -> dict[str, Any] | None:
        """Get raw settings from a single source."""
        path_str = self.settings_file_paths.get(source)
        if not path_str:
            return None

        path = Path(path_str)
        if not path.exists():
            return None

        try:
            raw = json.loads(path.read_text(errors="replace"))
            return raw if isinstance(raw, dict) else None
        except (json.JSONDecodeError, OSError):
            logger.warning("Failed to read settings source: %s", path, exc_info=True)
            return None

    def update_settings(
        self,
        source: str,
        settings: dict[str, Any],
    ) -> None:
        """Update settings for a given source."""
        if source in ("policySettings", "flagSettings"):
            return

        path_str = self.settings_file_paths.get(source)
        if not path_str:
            return

        path = Path(path_str)
        path.parent.mkdir(parents=True, exist_ok=True)

        existing: dict[str, Any] = {}
        if path.exists():
            try:
                existing = json.loads(path.read_text(errors="replace"))
            except (json.JSONDecodeError, OSError):
                logger.warning("Failed to read existing settings before update: %s", path, exc_info=True)

        merged = _merge_settings(existing, settings)
        path.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n")

        self.reset_cache()
