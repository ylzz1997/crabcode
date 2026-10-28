"""Bundled, read-only harness-compatible prompt templates.

Text snapshots and small, checked replacements keep adaptation reviewable.
Clients receive only the resulting text, never snapshot metadata.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from crabcode_core.types.config import PromptTemplateConfig

_NAMES = {
    "codex": "Codex", "claude-code": "Claude Code", "dsh": "DSH",
    "zcode": "ZCode", "opencode": "OpenCode", "cursor": "Cursor",
    "qoder": "Qoder", "kimi-code": "Kimi Code",
}


def is_builtin_id(template_id: str) -> bool:
    return template_id.startswith("builtin:")


@lru_cache(maxsize=8)
def _text(key: str) -> str:
    snapshot = json.loads((Path(__file__).with_name("presets") / f"{key}.json").read_text(encoding="utf-8"))
    text = snapshot["text"]
    for patch in snapshot["patches"]:
        if text.count(patch["before"]) != patch["count"]:
            raise ValueError(f"Invalid prompt patch: {key}")
        text = text.replace(patch["before"], patch["after"])
    return text


def builtin_templates() -> list[PromptTemplateConfig]:
    return [PromptTemplateConfig(id=f"builtin:{key}", name=name,
                                sections={"native_system": _text(key)})
            for key, name in _NAMES.items()]


def get_builtin_template(template_id: str) -> PromptTemplateConfig | None:
    key = template_id.removeprefix("builtin:")
    if not is_builtin_id(template_id) or key not in _NAMES:
        return None
    return PromptTemplateConfig(id=template_id, name=_NAMES[key], sections={"native_system": _text(key)})
