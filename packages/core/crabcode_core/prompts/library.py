"""Named prompt templates and prompts appended to user input.

A template section that is missing or blank keeps the built-in default.
Selecting no template leaves the legacy ``prompt_profile`` in effect, and
otherwise uses the built-in prompt.
"""

from __future__ import annotations

from typing import TypeVar

from crabcode_core.prompts.profile import PromptProfile
from crabcode_core.types.config import (
    CrabCodeSettings,
    PromptTemplateConfig,
    UserAppendPromptConfig,
)

PROMPT_SECTIONS: tuple[tuple[str, str], ...] = (
    ("prefix", "身份前缀"),
    ("intro", "介绍"),
    ("system", "系统规则"),
    ("doing_tasks", "任务执行"),
    ("actions", "授权与安全"),
    ("git_safety", "Git 安全"),
    ("using_tools", "工具使用"),
    ("tone_and_style", "语气与风格"),
    ("output_efficiency", "输出效率"),
    ("session_guidance", "会话指引"),
    ("agent_prompt", "子代理提示词"),
    ("compact_prompt", "上下文压缩提示词"),
    ("extra", "额外段落"),
)

PROMPT_SECTION_KEYS = frozenset(key for key, _label in PROMPT_SECTIONS)
_PROFILE_KEYS = PROMPT_SECTION_KEYS - {"extra"}

_T = TypeVar("_T", PromptTemplateConfig, UserAppendPromptConfig)


def last_by_id(items: list[_T]) -> list[_T]:
    """Keep one item per id. A later settings layer overrides an earlier one."""
    order: list[str] = []
    by_id: dict[str, _T] = {}
    for item in items:
        if item.id not in by_id:
            order.append(item.id)
        by_id[item.id] = item
    return [by_id[item_id] for item_id in order]


def profile_from_sections(sections: dict[str, str]) -> PromptProfile:
    """Build a profile. Blank text is omitted so that section stays default."""
    kwargs: dict[str, object] = {}
    extra: list[str] = []
    for key, raw in sections.items():
        if not isinstance(raw, str):
            continue
        text = raw.strip()
        if not text or key not in PROMPT_SECTION_KEYS:
            continue
        if key == "extra":
            extra.append(text)
        elif key in _PROFILE_KEYS:
            kwargs[key] = text
    if extra:
        kwargs["extra_sections"] = extra
    return PromptProfile(**kwargs)


def resolve_prompt_profile(settings: CrabCodeSettings) -> PromptProfile | None:
    """Return the active template, otherwise a legacy prompt_profile."""
    active = settings.active_prompt_template
    if active:
        match = next(
            (item for item in last_by_id(settings.prompt_templates) if item.id == active),
            None,
        )
        if match is not None:
            return profile_from_sections(match.sections)
    if settings.prompt_profile:
        return PromptProfile(**settings.prompt_profile)
    return None


def enabled_user_append_texts(settings: CrabCodeSettings) -> list[str]:
    """Return checked prompts in list order, after later layers override ids."""
    texts: list[str] = []
    for item in last_by_id(settings.user_append_prompts):
        if item.enabled and item.text.strip():
            texts.append(item.text.strip())
    return texts
