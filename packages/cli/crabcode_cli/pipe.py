"""Pipe mode — reads from stdin, sends to Core, writes to stdout."""

from __future__ import annotations

import asyncio
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


async def run_pipe(
    prompt: str,
    settings: CrabCodeSettings | None = None,
    cwd: str = ".",
) -> None:
    """Run a single prompt through the core and print the response."""
    session = CoreSession(cwd=cwd, settings=settings)
    try:
        async for event in session.send_message(prompt):
            if isinstance(event, StreamTextEvent):
                sys.stdout.write(safe_utf8_str(event.text))
                sys.stdout.flush()
            elif isinstance(event, ThinkingEvent):
                pass
            elif isinstance(event, ToolUseEvent):
                sys.stderr.write(f"\n[Tool: {event.tool_name}]\n")
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
                pass
    finally:
        await session.close()

    sys.stdout.write("\n")
    sys.stdout.flush()
