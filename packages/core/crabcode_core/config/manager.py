"""Configuration manager — 5-layer settings merge with Pydantic validation."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from crabcode_core.types.config import CrabCodeSettings


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
                continue

        try:
            self._cache = CrabCodeSettings.model_validate(merged)
        except Exception:
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
                pass

        merged = _merge_settings(existing, settings)
        path.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n")

        self.reset_cache()
