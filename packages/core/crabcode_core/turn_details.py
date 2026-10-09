"""Per-input statistics, independent of client connections and compaction."""

from datetime import datetime, timezone
from time import monotonic
from typing import Any

from crabcode_core.types.event import (
    CompactEvent, CoreEvent, StreamModeEvent, StreamRetryEvent, StreamTextEvent,
    ThinkingEvent, ToolUseEvent, TurnCompleteEvent,
)


class TurnDetailsTracker:
    def __init__(self) -> None:
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.started_clock = monotonic()
        self.tool_ids: set[str] = set()
        self.thinking_count = 0
        self.request_count = 0
        self.retry_count = 0
        self.compact_count = 0
        self.thinking = False

    def observe(self, event: CoreEvent) -> None:
        # Managed-agent activity has its own transcript and statistics.
        if getattr(event, "agent_id", None) is not None:
            return
        if isinstance(event, ThinkingEvent) and event.text:
            if not self.thinking:
                self.thinking_count += 1
            self.thinking = True
        elif isinstance(event, (StreamTextEvent, ToolUseEvent)):
            self.thinking = False
            if isinstance(event, ToolUseEvent):
                self.tool_ids.add(event.tool_use_id)
        elif isinstance(event, StreamModeEvent):
            if event.mode != "thinking":
                self.thinking = False
            if event.mode == "requesting":
                self.request_count += 1
        elif isinstance(event, StreamRetryEvent):
            self.retry_count += 1
            self.thinking = False
        elif isinstance(event, CompactEvent):
            self.compact_count += 1
            self.thinking = False

    def finish(self, event: TurnCompleteEvent, *, session_id: str, model: str, provider: str) -> dict[str, Any]:
        return {
            "session_id": session_id,
            "started_at": self.started_at,
            "ended_at": datetime.now(timezone.utc).isoformat(),
            "duration_ms": max(0, round((monotonic() - self.started_clock) * 1000)),
            "tool_call_count": len(self.tool_ids),
            "thinking_count": self.thinking_count,
            "request_count": self.request_count,
            "retry_count": self.retry_count,
            "compact_count": self.compact_count,
            "model": model,
            "provider": provider,
            "reason": event.reason,
            "usage": dict(event.usage),
            "source": "recorded",
        }
