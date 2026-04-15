"""Conversation compaction — summarizes old messages to free context space."""

from __future__ import annotations

import json
from typing import Any

from crabcode_core.api.base import APIAdapter, ModelConfig
from crabcode_core.logging_utils import get_logger
from crabcode_core.types.message import (
    Message,
    MessageRole,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    create_user_message,
    create_assistant_message,
)

logger = get_logger(__name__)


COMPACT_PROMPT = """Summarize the conversation so far concisely. Focus on:
1. What the user asked for
2. What actions were taken (files edited, commands run, tools used)
3. Current state of the task (what's done, what's pending)
4. Key findings or decisions made

Be concise but preserve all information needed to continue the conversation."""

DEFAULT_COMPACT_THRESHOLD = 100_000
AUTOCOMPACT_BUFFER_TOKENS = 13_000


def _estimate_block_chars(block: Any) -> int:
    """Estimate character count for a single content block."""
    if isinstance(block, TextBlock):
        return len(block.text)
    if isinstance(block, ToolUseBlock):
        return len(block.name) + len(json.dumps(block.input, ensure_ascii=False))
    if isinstance(block, ToolResultBlock):
        return len(block.content)
    if isinstance(block, ThinkingBlock):
        return len(block.thinking)
    if hasattr(block, "content"):
        return len(getattr(block, "content", ""))
    return 0


def _estimate_tokens_for_text(text: str) -> int:
    """Estimate token count using UTF-8 byte length as a fast proxy.

    ASCII: ratio=1.0 → ~0.25 tokens/char; CJK: ratio=3.0 → ~1.5 tokens/char.
    Linear interpolation between these extremes based on byte/char ratio.
    """
    total_chars = len(text)
    if total_chars == 0:
        return 0
    byte_len = len(text.encode("utf-8"))
    ratio = byte_len / total_chars
    tokens_per_char = 0.25 + (ratio - 1.0) * 0.625
    return max(1, int(total_chars * tokens_per_char))


def estimate_token_count(messages: list[Message], system: list[str] | None = None) -> int:
    """Estimate token count for messages and optional system prompt.

    Uses UTF-8 byte length heuristic for fast estimation without char-by-char
    iteration. Covers TextBlock, ToolUseBlock, ToolResultBlock, ThinkingBlock.
    """
    total_bytes = 0
    total_chars = 0

    def _account(s: object) -> None:
        nonlocal total_bytes, total_chars
        if not isinstance(s, str):
            s = str(s) if s is not None else ""
        total_chars += len(s)
        total_bytes += len(s.encode("utf-8"))

    if system:
        for s in system:
            _account(s)
    for msg in messages:
        if isinstance(msg.content, str):
            _account(msg.content)
        elif msg.content is None:
            continue
        else:
            for block in msg.content:
                if isinstance(block, TextBlock):
                    _account(block.text)
                elif isinstance(block, ToolResultBlock):
                    _account(block.content)
                elif isinstance(block, ThinkingBlock):
                    _account(block.thinking)
                elif isinstance(block, ToolUseBlock):
                    _account(json.dumps(block.input, ensure_ascii=False))

    if total_chars == 0:
        return 0
    ratio = total_bytes / total_chars
    tokens_per_char = 0.25 + (ratio - 1.0) * 0.625
    return max(1, int(total_chars * tokens_per_char))


def should_auto_compact(
    messages: list[Message],
    threshold: int = DEFAULT_COMPACT_THRESHOLD,
) -> bool:
    """Check if conversation should be auto-compacted."""
    if len(messages) < 4:
        return False
    estimated_tokens = estimate_token_count(messages)
    return estimated_tokens > (threshold - AUTOCOMPACT_BUFFER_TOKENS)


async def compact_conversation(
    messages: list[Message],
    api_adapter: APIAdapter | None = None,
    custom_summary: str | None = None,
) -> list[Message] | None:
    """Compact a conversation by summarizing old messages.

    Returns new message list with summary, or None if compaction failed.
    """
    if len(messages) < 4:
        return None

    if custom_summary:
        summary = custom_summary
    elif api_adapter:
        summary = await _generate_summary(messages, api_adapter)
    else:
        summary = _fallback_summary(messages)

    if not summary:
        return None

    summary_msg = create_user_message(content=f"[Conversation summary: {summary}]")
    summary_msg.is_compact_summary = True

    recent_count = min(4, len(messages))
    recent = messages[-recent_count:]

    return [summary_msg, *recent]


async def _generate_summary(
    messages: list[Message],
    api_adapter: APIAdapter,
) -> str:
    """Use the API to generate a conversation summary."""
    try:
        conversation_text = ""
        for msg in messages:
            role = msg.role.value
            text = msg.text_content[:2000] if msg.text_content else ""
            if text:
                conversation_text += f"{role}: {text}\n\n"

        if len(conversation_text) > 50_000:
            conversation_text = conversation_text[:50_000] + "\n... (truncated)"

        summary_messages = [
            create_user_message(
                content=f"{COMPACT_PROMPT}\n\nConversation:\n{conversation_text}"
            ),
        ]

        adapter_model = ""
        if hasattr(api_adapter, "config"):
            adapter_model = getattr(api_adapter.config, "model", "") or ""
        config = ModelConfig(
            model=adapter_model,
            max_tokens=2048,
            thinking_enabled=False,
        )

        summary_parts: list[str] = []
        async for chunk in api_adapter.stream_message(
            messages=summary_messages,
            system=["You are a helpful assistant that summarizes conversations concisely."],
            tools=[],
            config=config,
        ):
            if chunk.type == "text":
                summary_parts.append(chunk.text)

        return "".join(summary_parts)

    except Exception:
        logger.warning("Conversation compaction via API failed; using fallback summary", exc_info=True)
        return _fallback_summary(messages)


def _fallback_summary(messages: list[Message]) -> str:
    """Generate a simple summary without API access."""
    parts: list[str] = []

    user_msgs = [m for m in messages if m.role == MessageRole.USER and m.text_content]
    if user_msgs:
        first = user_msgs[0].text_content[:200]
        parts.append(f"User initially asked: {first}")

    tool_names: set[str] = set()
    for msg in messages:
        if not isinstance(msg.content, str):
            for block in msg.content:
                if hasattr(block, "name"):
                    tool_names.add(getattr(block, "name"))

    if tool_names:
        parts.append(f"Tools used: {', '.join(sorted(tool_names))}")

    parts.append(f"Total messages: {len(messages)}")

    return " | ".join(parts)
