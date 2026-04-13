"""Conversation compaction — summarizes old messages to free context space."""

from __future__ import annotations

from typing import Any

from crabcode_core.api.base import APIAdapter, ModelConfig
from crabcode_core.logging_utils import get_logger
from crabcode_core.types.message import (
    Message,
    MessageRole,
    TextBlock,
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


def estimate_token_count(messages: list[Message]) -> int:
    """Rough token estimate: ~4 chars per token."""
    total = 0
    for msg in messages:
        if isinstance(msg.content, str):
            total += len(msg.content)
        else:
            for block in msg.content:
                if isinstance(block, TextBlock):
                    total += len(block.text)
                elif hasattr(block, "content"):
                    total += len(getattr(block, "content", ""))
    return total // 4


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

        config = ModelConfig(
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
