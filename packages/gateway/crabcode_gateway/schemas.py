"""Pydantic request/response schemas for the gateway API.

Maps crabcode_core CoreEvent dataclasses to serializable Pydantic models
for HTTP JSON and SSE transport.
"""

from __future__ import annotations

from typing import Any, Literal, Union

from pydantic import BaseModel, Field


# ── Request schemas ──────────────────────────────────────────────


class SendMessageRequest(BaseModel):
    text: str
    max_turns: int = 0
    session_id: str | None = None


class NewSessionRequest(BaseModel):
    cwd: str | None = None


class ResumeSessionRequest(BaseModel):
    session_id: str


class CompactRequest(BaseModel):
    session_id: str


class InterruptRequest(BaseModel):
    session_id: str


class PermissionResponseRequest(BaseModel):
    tool_use_id: str
    allowed: bool
    always_allow: bool = False
    agent_id: str | None = None


class ChoiceResponseRequest(BaseModel):
    tool_use_id: str
    selected: list[str]
    cancelled: bool = False
    agent_id: str | None = None


class SpawnAgentRequest(BaseModel):
    prompt: str
    subagent_type: str = "generalPurpose"
    name: str | None = None
    model_profile: str | None = None


class AgentInputRequest(BaseModel):
    prompt: str
    interrupt: bool = False


class WaitAgentRequest(BaseModel):
    agent_id: str | list[str]
    timeout_ms: int | None = None


class SwitchModelRequest(BaseModel):
    name: str


class SwitchModeRequest(BaseModel):
    mode: Literal["agent", "plan"]


class ContextPushRequest(BaseModel):
    """Client pushes workspace context to the server.

    Used by VSCode extension to inform the gateway about the current
    editor state (active file, selection, cursor position, etc.).
    """

    session_id: str
    active_file: str | None = None
    selected_text: str | None = None
    cursor_line: int | None = None
    cursor_column: int | None = None
    open_files: list[str] = Field(default_factory=list)
    language_id: str | None = None


class CheckpointRequest(BaseModel):
    """Create a checkpoint with file snapshot."""
    session_id: str
    label: str = ""


class RevertRequest(BaseModel):
    """Revert files + conversation to a checkpoint."""
    session_id: str
    checkpoint_id: str


# ── Response / event schemas ─────────────────────────────────────


class SessionInfo(BaseModel):
    session_id: str
    message_count: int = 0
    model: str = ""
    provider: str = ""
    created_at: str = ""


class AgentInfo(BaseModel):
    agent_id: str
    parent_agent_id: str | None = None
    title: str
    subagent_type: str
    status: str
    model: str
    created_at: str
    usage: dict[str, Any] = Field(default_factory=dict)
    final_result: str = ""
    error: str = ""


class ToolInfo(BaseModel):
    name: str
    description: str = ""
    is_read_only: bool = False
    is_enabled: bool = True


class ModelInfo(BaseModel):
    name: str
    description: str = ""


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = "0.1.0"


# ── CoreEvent serialization ──────────────────────────────────────
# These models represent CoreEvent variants on the wire.  They carry
# a ``type`` discriminator so the client can dispatch correctly.


class StreamTextPayload(BaseModel):
    type: Literal["stream_text"] = "stream_text"
    text: str


class ThinkingPayload(BaseModel):
    type: Literal["thinking"] = "thinking"
    text: str


class ToolUsePayload(BaseModel):
    type: Literal["tool_use"] = "tool_use"
    tool_name: str
    tool_input: dict[str, Any]
    tool_use_id: str
    agent_id: str | None = None


class ToolResultPayload(BaseModel):
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    tool_name: str
    result: str
    is_error: bool = False
    result_for_display: str | None = None
    agent_id: str | None = None


class PermissionRequestPayload(BaseModel):
    type: Literal["permission_request"] = "permission_request"
    tool_name: str
    tool_input: dict[str, Any]
    tool_use_id: str
    reason: str | None = None
    permission_key: str | None = None
    agent_id: str | None = None


class PermissionResponsePayload(BaseModel):
    type: Literal["permission_response"] = "permission_response"
    tool_use_id: str
    allowed: bool
    always_allow: bool = False
    agent_id: str | None = None


class ChoiceRequestPayload(BaseModel):
    type: Literal["choice_request"] = "choice_request"
    tool_use_id: str
    question: str
    options: list[str]
    multiple: bool = False
    agent_id: str | None = None


class ChoiceResponsePayload(BaseModel):
    type: Literal["choice_response"] = "choice_response"
    tool_use_id: str
    selected: list[str]
    cancelled: bool = False
    agent_id: str | None = None


class CompactPayload(BaseModel):
    type: Literal["compact"] = "compact"
    summary: str
    messages_before: int = 0
    messages_after: int = 0


class ErrorPayload(BaseModel):
    type: Literal["error"] = "error"
    message: str
    recoverable: bool = True
    error_type: str = ""


class TurnCompletePayload(BaseModel):
    type: Literal["turn_complete"] = "turn_complete"
    reason: str = "end_turn"
    turn_count: int = 0
    usage: dict[str, Any] = Field(default_factory=dict)


class StreamModePayload(BaseModel):
    type: Literal["stream_mode"] = "stream_mode"
    mode: str
    agent_id: str | None = None


class AgentStatePayload(BaseModel):
    type: Literal["agent_state"] = "agent_state"
    agent_id: str
    parent_agent_id: str | None = None
    status: str
    subagent_type: str
    title: str
    message: str = ""
    usage: dict[str, Any] = Field(default_factory=dict)


class AgentOutputPayload(BaseModel):
    type: Literal["agent_output"] = "agent_output"
    agent_id: str
    stream: str
    text: str
    tool_name: str | None = None


class ModeChangePayload(BaseModel):
    type: Literal["mode_change"] = "mode_change"
    mode: str
    reason: str = ""


class PlanReadyPayload(BaseModel):
    type: Literal["plan_ready"] = "plan_ready"
    plan: dict[str, Any]


class TeamMessagePayload(BaseModel):
    type: Literal["team_message"] = "team_message"
    team_id: str
    from_agent: str
    to_agent: str
    text: str
    msg_type: str = "text"
    message_id: str = ""


class TeamStatePayload(BaseModel):
    type: Literal["team_state"] = "team_state"
    team_id: str
    agent_id: str
    old_state: str
    new_state: str
    role: str = ""


class TaskUpdatePayload(BaseModel):
    type: Literal["task_update"] = "task_update"
    team_id: str
    task_id: str
    status: str
    assignee: str | None = None
    description: str = ""


class FileChangePayload(BaseModel):
    """File change notification for frontends (VSCode, etc.).

    Emitted when a tool modifies the filesystem so the client can
    refresh its editor view, show diffs, etc.
    """

    type: Literal["file_change"] = "file_change"
    path: str
    action: Literal["create", "modify", "delete"]
    diff: str | None = None


class SnapshotPayload(BaseModel):
    type: Literal["snapshot"] = "snapshot"
    snapshot_id: str
    tool_name: str
    files: list[str] = Field(default_factory=list)


class RevertPayload(BaseModel):
    type: Literal["revert"] = "revert"
    snapshot_id: str
    files_restored: list[str] = Field(default_factory=list)


class ServerConnectedPayload(BaseModel):
    type: Literal["server.connected"] = "server.connected"
    properties: dict[str, Any] = Field(default_factory=dict)


class ServerHeartbeatPayload(BaseModel):
    type: Literal["server.heartbeat"] = "server.heartbeat"
    properties: dict[str, Any] = Field(default_factory=dict)


EventPayload = Union[
    StreamTextPayload,
    ThinkingPayload,
    ToolUsePayload,
    ToolResultPayload,
    PermissionRequestPayload,
    PermissionResponsePayload,
    ChoiceRequestPayload,
    ChoiceResponsePayload,
    CompactPayload,
    ErrorPayload,
    TurnCompletePayload,
    StreamModePayload,
    AgentStatePayload,
    AgentOutputPayload,
    ModeChangePayload,
    PlanReadyPayload,
    TeamMessagePayload,
    TeamStatePayload,
    TaskUpdatePayload,
    FileChangePayload,
    SnapshotPayload,
    RevertPayload,
    ServerConnectedPayload,
    ServerHeartbeatPayload,
]


# ── CoreEvent → Pydantic conversion ──────────────────────────────

def core_event_to_payload(event: Any) -> EventPayload:
    """Convert a crabcode_core CoreEvent dataclass to a Pydantic payload."""
    from crabcode_core.types.event import (
        AgentOutputEvent,
        AgentStateEvent,
        ChoiceRequestEvent,
        ChoiceResponseEvent,
        CompactEvent,
        ErrorEvent,
        ModeChangeEvent,
        PermissionRequestEvent,
        PermissionResponseEvent,
        PlanReadyEvent,
        StreamModeEvent,
        StreamTextEvent,
        TaskUpdateEvent,
        TeamMessageEvent,
        TeamStateEvent,
        ThinkingEvent,
        ToolResultEvent,
        ToolUseEvent,
        TurnCompleteEvent,
    )

    if isinstance(event, StreamTextEvent):
        return StreamTextPayload(text=event.text)
    if isinstance(event, ThinkingEvent):
        return ThinkingPayload(text=event.text)
    if isinstance(event, ToolUseEvent):
        return ToolUsePayload(
            tool_name=event.tool_name,
            tool_input=event.tool_input,
            tool_use_id=event.tool_use_id,
            agent_id=event.agent_id,
        )
    if isinstance(event, ToolResultEvent):
        return ToolResultPayload(
            tool_use_id=event.tool_use_id,
            tool_name=event.tool_name,
            result=event.result,
            is_error=event.is_error,
            result_for_display=event.result_for_display,
            agent_id=event.agent_id,
        )
    if isinstance(event, PermissionRequestEvent):
        return PermissionRequestPayload(
            tool_name=event.tool_name,
            tool_input=event.tool_input,
            tool_use_id=event.tool_use_id,
            reason=event.reason,
            permission_key=event.permission_key,
            agent_id=event.agent_id,
        )
    if isinstance(event, PermissionResponseEvent):
        return PermissionResponsePayload(
            tool_use_id=event.tool_use_id,
            allowed=event.allowed,
            always_allow=event.always_allow,
            agent_id=event.agent_id,
        )
    if isinstance(event, ChoiceRequestEvent):
        return ChoiceRequestPayload(
            tool_use_id=event.tool_use_id,
            question=event.question,
            options=event.options,
            multiple=event.multiple,
            agent_id=event.agent_id,
        )
    if isinstance(event, ChoiceResponseEvent):
        return ChoiceResponsePayload(
            tool_use_id=event.tool_use_id,
            selected=event.selected,
            cancelled=event.cancelled,
            agent_id=event.agent_id,
        )
    if isinstance(event, CompactEvent):
        return CompactPayload(
            summary=event.summary,
            messages_before=event.messages_before,
            messages_after=event.messages_after,
        )
    if isinstance(event, ErrorEvent):
        return ErrorPayload(
            message=event.message,
            recoverable=event.recoverable,
            error_type=event.error_type,
        )
    if isinstance(event, TurnCompleteEvent):
        return TurnCompletePayload(
            reason=event.reason,
            turn_count=event.turn_count,
            usage=event.usage,
        )
    if isinstance(event, StreamModeEvent):
        return StreamModePayload(mode=event.mode, agent_id=event.agent_id)
    if isinstance(event, AgentStateEvent):
        return AgentStatePayload(
            agent_id=event.agent_id,
            parent_agent_id=event.parent_agent_id,
            status=event.status,
            subagent_type=event.subagent_type,
            title=event.title,
            message=event.message,
            usage=event.usage,
        )
    if isinstance(event, AgentOutputEvent):
        return AgentOutputPayload(
            agent_id=event.agent_id,
            stream=event.stream,
            text=event.text,
            tool_name=event.tool_name,
        )
    if isinstance(event, ModeChangeEvent):
        return ModeChangePayload(mode=event.mode, reason=event.reason)
    if isinstance(event, PlanReadyEvent):
        return PlanReadyPayload(plan=event.plan)
    if isinstance(event, TeamMessageEvent):
        return TeamMessagePayload(
            team_id=event.team_id,
            from_agent=event.from_agent,
            to_agent=event.to_agent,
            text=event.text,
            msg_type=event.msg_type,
            message_id=event.message_id,
        )
    if isinstance(event, TeamStateEvent):
        return TeamStatePayload(
            team_id=event.team_id,
            agent_id=event.agent_id,
            old_state=event.old_state,
            new_state=event.new_state,
            role=event.role,
        )
    if isinstance(event, TaskUpdateEvent):
        return TaskUpdatePayload(
            team_id=event.team_id,
            task_id=event.task_id,
            status=event.status,
            assignee=event.assignee,
            description=event.description,
        )
    from crabcode_core.types.event import SnapshotEvent, RevertEvent
    if isinstance(event, SnapshotEvent):
        return SnapshotPayload(
            snapshot_id=event.snapshot_id,
            tool_name=event.tool_name,
            files=event.files,
        )
    if isinstance(event, RevertEvent):
        return RevertPayload(
            snapshot_id=event.snapshot_id,
            files_restored=event.files_restored,
        )
    # Fallback: wrap as error
    return ErrorPayload(
        message=f"Unknown event type: {type(event).__name__}",
        recoverable=True,
        error_type="unknown",
    )
