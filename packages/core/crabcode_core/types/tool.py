"""Tool system types for CrabCode."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable, Coroutine

if TYPE_CHECKING:
    from crabcode_core.types.message import AssistantMessage, Message


class PermissionBehavior(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


ToolEventCallback = Callable[[str, str, dict[str, Any]], None]
"""Signature: (tool_name, event_type, data)"""


@dataclass
class PermissionResult:
    behavior: PermissionBehavior = PermissionBehavior.ALLOW
    reason: str | None = None
    updated_input: dict[str, Any] | None = None
    permission_key: str | None = None


@dataclass
class ToolResult:
    """Result from executing a tool."""

    data: Any = None
    result_for_model: str = ""
    result_for_display: str | None = None
    is_error: bool = False


@dataclass
class ToolContext:
    """Context passed to tool execution."""

    cwd: str = "."
    messages: list[Message] = field(default_factory=list)
    abort_signal: Any | None = None
    session_id: str = ""
    env: dict[str, str] = field(default_factory=dict)
    on_event: ToolEventCallback | None = None
    tool_config: dict[str, Any] = field(default_factory=dict)
    choice_queue: Any | None = None  # asyncio.Queue[ChoiceResponseEvent]
    tool_event_queue: Any | None = None  # asyncio.Queue[CoreEvent] — for tools to emit events mid-execution
    agent_id: str | None = None
    agent_depth: int = 0
    agent_manager: Any | None = None
    lsp_manager: Any | None = None  # LSPManager — session-scoped, set during CoreSession.initialize()
    team_id: str | None = None  # Team ID the current agent belongs to
    team_manager: Any | None = None  # TeamManager — session-scoped
    schedule_manager: Any | None = None  # ScheduleManager — session-scoped
    session: Any | None = None  # CoreSession — for checkpoint/revert operations


CanUseToolFn = Callable[
    ["Tool", dict[str, Any], "ToolContext"],
    Coroutine[Any, Any, PermissionResult],
]


class Tool(ABC):
    """Base class for all CrabCode tools."""

    name: str = ""
    description: str = ""
    input_schema: dict[str, Any] = {}
    is_read_only: bool = False
    is_concurrency_safe: bool = False
    is_enabled: bool = True
    uses_tool_permission_policy: bool = False

    _cached_prompt: str | None = None
    _background_task: asyncio.Task[Any] | None = None
    _setup_context: ToolContext | None = None

    @abstractmethod
    async def call(
        self,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        """Execute the tool with given input."""
        ...

    async def setup(self, context: ToolContext) -> None:
        """Called once during session init. Override for heavy initialization."""
        self._setup_context = context

    async def close(self) -> None:
        """Called during session shutdown to release resources."""
        return None

    def emit_event(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        """Emit a tool event (progress, ready, error, etc.)."""
        if self._setup_context and self._setup_context.on_event:
            self._setup_context.on_event(self.name, event_type, data or {})

    async def wait_ready(self) -> None:
        """Wait for background initialization to complete."""
        if self._background_task and not self._background_task.done():
            await self._background_task

    async def get_prompt(self, **kwargs: Any) -> str:
        """Return the tool description for the system prompt / API tools list."""
        return self.description

    async def validate_input(
        self, tool_input: dict[str, Any]
    ) -> str | None:
        """Validate input. Return error string or None if valid."""
        return None

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> PermissionResult:
        """Check if the tool can be used with the given input."""
        return PermissionResult()

    def get_permission_key(self, tool_input: dict[str, Any]) -> str:
        """Return the permission key used for session-scoped allow rules."""
        return self.name

    def to_api_schema(self) -> dict[str, Any]:
        """Convert to API tool schema for model requests.

        Uses the detailed prompt from get_prompt() when available,
        falling back to the short description.
        """
        return {
            "name": self.name,
            "description": self._cached_prompt or self.description,
            "input_schema": self.input_schema,
        }

    async def resolve_prompt(self, **kwargs: Any) -> None:
        """Pre-resolve the detailed prompt for use in to_api_schema().

        Call this once during setup so to_api_schema() can include the
        full tool description without being async.
        """
        prompt = await self.get_prompt(**kwargs)
        if prompt and prompt != self.description:
            self._cached_prompt = prompt
        else:
            self._cached_prompt = None
