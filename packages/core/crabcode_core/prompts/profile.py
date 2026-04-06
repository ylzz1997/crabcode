"""Prompt profile — configurable prompt templates for different agent personas.

Each section field controls one part of the system prompt:
  - None   → use the built-in default for that section
  - ""     → disable (skip) the section entirely
  - "..."  → override with custom text
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from crabcode_core.prompts.templates import DEFAULT_AGENT_PROMPT, DEFAULT_PREFIX


class PromptProfile(BaseModel):
    """Configurable prompt profile that separates identity/behavior from engine logic."""

    # Agent identity line — used to build the default intro section.
    prefix: str = DEFAULT_PREFIX

    # ── Behavioral sections (domain-specific, the main things to swap) ──

    intro: str | None = None
    system: str | None = None
    doing_tasks: str | None = None
    actions: str | None = None
    git_safety: str | None = None
    using_tools: str | None = None
    tone_and_style: str | None = None
    output_efficiency: str | None = None
    session_guidance: str | None = None

    # Sub-agent system prompt.
    agent_prompt: str | None = None

    # Extra sections appended after all built-in sections
    # (before the dynamic env / language / mcp blocks).
    extra_sections: list[str] = Field(default_factory=list)

    model_config = {"extra": "allow"}


def default_profile() -> PromptProfile:
    """The default coding-assistant profile (all built-in defaults)."""
    return PromptProfile()


def minimal_profile() -> PromptProfile:
    """A minimal profile with coding-specific sections stripped out.

    Useful as a starting point for non-coding agents.
    """
    return PromptProfile(
        prefix="You are a helpful AI assistant.",
        doing_tasks="",
        actions="",
        git_safety="",
    )


def resolve_agent_prompt(profile: PromptProfile | None) -> str:
    """Return the sub-agent system prompt, respecting profile overrides."""
    if profile and profile.agent_prompt is not None:
        return profile.agent_prompt
    return DEFAULT_AGENT_PROMPT
