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
    agent_id: str | None = None


@dataclass
class ToolResultEvent:
    """Result from a tool execution."""
    tool_use_id: str
    tool_name: str
    result: str
    is_error: bool = False
    result_for_display: str | None = None
    agent_id: str | None = None


@dataclass
class PermissionRequestEvent:
    """Core is requesting permission from the frontend to use a tool."""
    tool_name: str
    tool_input: dict[str, Any]
    tool_use_id: str
    reason: str | None = None
    permission_key: str | None = None
    agent_id: str | None = None


@dataclass
class PermissionResponseEvent:
    """Frontend's response to a permission request."""
    tool_use_id: str
    allowed: bool
    always_allow: bool = False
    agent_id: str | None = None


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
class ChoiceRequestEvent:
    """Core is requesting the user to make a choice from a list of options."""
    tool_use_id: str
    question: str
    options: list[str]
    multiple: bool = False
    agent_id: str | None = None


@dataclass
class ChoiceResponseEvent:
    """Frontend's response to a choice request."""
    tool_use_id: str
    selected: list[str]
    cancelled: bool = False
    agent_id: str | None = None


@dataclass
class StreamModeEvent:
    """Signals a phase transition in the streaming lifecycle.

    Modes:
      - "requesting": API request sent, waiting for first chunk
      - "thinking": model entered extended thinking
      - "responding": model is streaming text output
    """
    mode: str
    agent_id: str | None = None


@dataclass
class AgentStateEvent:
    """Lifecycle update for a managed sub-agent."""
    agent_id: str
    parent_agent_id: str | None
    status: str
    subagent_type: str
    title: str
    message: str = ""
    usage: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentOutputEvent:
    """Streamed output or tool activity from a managed sub-agent."""
    agent_id: str
    stream: str
    text: str
    tool_name: str | None = None


@dataclass
class ModeChangeEvent:
    """Agent mode has changed (e.g. plan <-> agent)."""
    mode: str  # "plan" | "agent"
    reason: str = ""


@dataclass
class PlanReadyEvent:
    """A structured execution plan has been produced by plan mode."""
    plan: dict[str, Any]


@dataclass
class TeamMessageEvent:
    """A message was sent between teammates."""
    team_id: str
    from_agent: str
    to_agent: str
    text: str
    msg_type: str = "text"
    message_id: str = ""


@dataclass
class TeamStateEvent:
    """A teammate's state changed within a team."""
    team_id: str
    agent_id: str
    old_state: str
    new_state: str
    role: str = ""


@dataclass
class TaskUpdateEvent:
    """A task on the shared task board was updated."""
    team_id: str
    task_id: str
    status: str
    assignee: str | None = None
    description: str = ""


CoreEvent = Union[
    StreamTextEvent,
    ThinkingEvent,
    ToolUseEvent,
    ToolResultEvent,
    PermissionRequestEvent,
    ChoiceRequestEvent,
    CompactEvent,
    ErrorEvent,
    TurnCompleteEvent,
    StreamModeEvent,
    AgentStateEvent,
    AgentOutputEvent,
    ModeChangeEvent,
    PlanReadyEvent,
    TeamMessageEvent,
    TeamStateEvent,
    TaskUpdateEvent,
]
