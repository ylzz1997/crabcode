"""Pipe mode — reads from stdin, sends to Core, writes to stdout."""

from __future__ import annotations

import json
import os
import sys

from crabcode_core.events import CoreSession
from crabcode_core.utf8_sanitize import safe_utf8_str
from crabcode_core.types.config import CrabCodeSettings
from crabcode_core.types.event import (
    ChoiceRequestEvent,
    ChoiceResponseEvent,
    ErrorEvent,
    PermissionRequestEvent,
    PermissionResponseEvent,
    StreamTextEvent,
    ThinkingEvent,
    ToolResultEvent,
    ToolUseEvent,
    TurnCompleteEvent,
)

_DEBUG_TOOL_PAYLOAD = os.getenv("CRABCODE_DEBUG_TOOL_PAYLOAD", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


def _format_token_count(tokens: int) -> str:
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:.1f}M"
    if tokens >= 1_000:
        return f"{tokens // 1_000}k"
    return str(tokens)


def _format_percent(percent: float) -> str:
    rounded = round(percent)
    if abs(percent - rounded) < 0.05:
        return f"{rounded}%"
    return f"{percent:.1f}%"


def _format_context_usage(event: TurnCompleteEvent) -> str | None:
    used = max(0, int(getattr(event, "context_used_tokens", 0) or 0))
    window = max(0, int(getattr(event, "context_window_tokens", 0) or 0))
    parts: list[str] = []
    if window:
        used_percent = max(0.0, float(getattr(event, "context_used_percent", 0.0) or 0.0))
        remaining_percent = max(0.0, 100.0 - used_percent)
        parts.append(
            f"Context: {_format_percent(used_percent)} used "
            f"({_format_percent(remaining_percent)} remaining) · "
            f"{_format_token_count(used)} tokens used of {_format_token_count(window)}"
        )
    elif used:
        parts.append(f"Context: {_format_token_count(used)} tokens used (window unknown)")

    usage = event.usage
    if "cache_read_tokens" in usage or "cache_write_tokens" in usage:
        cache_read = max(0, int(usage.get("cache_read_tokens", 0) or 0))
        cache_write = max(0, int(usage.get("cache_write_tokens", 0) or 0))
        total_input = max(
            0,
            int(usage.get("total_input_tokens", usage.get("input_tokens", 0)) or 0),
        )
        hit_rate = cache_read / total_input * 100 if total_input else 0.0
        cache_parts = [
            f"Cache: {_format_percent(hit_rate)} hit",
            f"read {_format_token_count(cache_read)}",
        ]
        if "cache_write_tokens" in usage:
            cache_parts.append(f"write {_format_token_count(cache_write)}")
        parts.append(" · ".join(cache_parts))
    return " · ".join(parts) or None


async def run_pipe(
    prompt: str,
    settings: CrabCodeSettings | None = None,
    cwd: str = ".",
    images: list[dict[str, str]] | None = None,
) -> None:
    """Run a single prompt through the core and print the response."""
    session = CoreSession(cwd=cwd, settings=settings)
    try:
        async for event in session.send_message(prompt, images=images):
            if isinstance(event, StreamTextEvent):
                sys.stdout.write(safe_utf8_str(event.text))
                sys.stdout.flush()
            elif isinstance(event, ThinkingEvent):
                pass
            elif isinstance(event, ToolUseEvent):
                sys.stderr.write(f"\n[Tool: {event.tool_name}]\n")
                if _DEBUG_TOOL_PAYLOAD:
                    pretty = json.dumps(event.tool_input, ensure_ascii=False, indent=2)
                    sys.stderr.write(f"{pretty}\n")
                sys.stderr.flush()
            elif isinstance(event, PermissionRequestEvent):
                await session.respond_permission(
                    PermissionResponseEvent(
                        tool_use_id=event.tool_use_id, allowed=True
                    )
                )
            elif isinstance(event, ChoiceRequestEvent):
                selected = [event.options[0]] if event.options else []
                await session.respond_choice(
                    ChoiceResponseEvent(
                        tool_use_id=event.tool_use_id,
                        selected=selected,
                        cancelled=not bool(selected),
                    )
                )
            elif isinstance(event, ToolResultEvent):
                if event.is_error:
                    sys.stderr.write(f"\n[Error: {event.result}]\n")
                    sys.stderr.flush()
            elif isinstance(event, ErrorEvent):
                sys.stderr.write(f"\nError: {safe_utf8_str(event.message)}\n")
                sys.stderr.flush()
                if not event.recoverable:
                    sys.exit(1)
            elif isinstance(event, TurnCompleteEvent):
                usage_text = _format_context_usage(event)
                if usage_text:
                    sys.stderr.write(f"\n{usage_text}\n")
                    sys.stderr.flush()
    finally:
        await session.close()

    sys.stdout.write("\n")
    sys.stdout.flush()
