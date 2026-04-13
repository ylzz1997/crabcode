"""Helpers for normalizing model-provided tool arguments."""

from __future__ import annotations

from typing import Any

from crabcode_core.types.message import Message, MessageRole, TextBlock


def first_non_empty_str(tool_input: dict[str, Any], keys: tuple[str, ...]) -> str:
    """Return the first stripped non-empty string among the given keys."""
    for key in keys:
        raw = tool_input.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return ""


def latest_user_text_for_agent_fallback(messages: list[Message]) -> str:
    """Text from the most recent user message that contains user-typed text (TextBlock).

    Skips user messages that only carry tool results, so a missing ``prompt`` on
    Agent can still map to the user's actual question when the model omits args.
    """
    for msg in reversed(messages):
        if msg.role != MessageRole.USER:
            continue
        if isinstance(msg.content, str):
            t = msg.content.strip()
            if t:
                return t
        parts: list[str] = []
        for block in msg.content:
            if isinstance(block, TextBlock):
                parts.append(block.text)
        text = "".join(parts).strip()
        if text:
            return text
    return ""
