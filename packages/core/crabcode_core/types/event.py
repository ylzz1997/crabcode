"""Event types for Core <-> Frontend communication.

The Core emits these events as an async stream. Frontends (CLI, Web UI, SDK)
consume them to render output, handle permission prompts, etc.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Union


@dataclass
class StreamTextEvent:
    """A chunk of streamed text from the model."""
    text: str


@dataclass
class ThinkingEvent:
    """A chunk of thinking/reasoning from the model."""
    text: str


@dataclass
class ToolUseEvent:
    """The model wants to use a tool."""
    tool_name: str
    tool_input: dict[str, Any]
    tool_use_id: str


@dataclass
class ToolResultEvent:
    """Result from a tool execution."""
    tool_use_id: str
    tool_name: str
    result: str
    is_error: bool = False
    result_for_display: str | None = None


@dataclass
class PermissionRequestEvent:
    """Core is requesting permission from the frontend to use a tool."""
    tool_name: str
    tool_input: dict[str, Any]
    tool_use_id: str


@dataclass
class PermissionResponseEvent:
    """Frontend's response to a permission request."""
    tool_use_id: str
    allowed: bool
    always_allow: bool = False


@dataclass
class CompactEvent:
    """Conversation was compacted."""
    summary: str
    messages_before: int = 0
    messages_after: int = 0


@dataclass
class ErrorEvent:
    """An error occurred."""
    message: str
    recoverable: bool = True
    error_type: str = ""


@dataclass
class TurnCompleteEvent:
    """An agentic turn completed."""
    reason: str = "end_turn"
    turn_count: int = 0
    usage: dict[str, Any] = field(default_factory=dict)


@dataclass
class StreamModeEvent:
    """Signals a phase transition in the streaming lifecycle.

    Modes:
      - "requesting": API request sent, waiting for first chunk
      - "thinking": model entered extended thinking
      - "responding": model is streaming text output
    """
    mode: str


CoreEvent = Union[
    StreamTextEvent,
    ThinkingEvent,
    ToolUseEvent,
    ToolResultEvent,
    PermissionRequestEvent,
    CompactEvent,
    ErrorEvent,
    TurnCompleteEvent,
    StreamModeEvent,
]
