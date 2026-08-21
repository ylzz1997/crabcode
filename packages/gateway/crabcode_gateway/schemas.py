"""Pydantic request/response schemas for the gateway API.

Maps crabcode_core CoreEvent dataclasses to serializable Pydantic models
for HTTP JSON and SSE transport.
"""

from __future__ import annotations

import base64
import binascii
from typing import Any, Literal, Union

from pydantic import BaseModel, Field, field_validator, model_validator

from crabcode_core import VERSION
from crabcode_gateway.protocol import (
    GATEWAY_MAX_PROTOCOL_VERSION,
    GATEWAY_MIN_PROTOCOL_VERSION,
    GATEWAY_PROTOCOL_VERSION,
)
from crabcode_core.types.config import ReasoningEffort


# ── Request schemas ──────────────────────────────────────────────


MAX_IMAGE_ATTACHMENT_BYTES = 20 * 1024 * 1024


class ImageAttachment(BaseModel):
    media_type: str
    data: str

    @field_validator("media_type")
    @classmethod
    def validate_media_type(cls, value: str) -> str:
        media_type = value.strip()
        if not media_type.lower().startswith("image/"):
            raise ValueError("media_type must be an image MIME type")
        return media_type

    @field_validator("data")
    @classmethod
    def validate_data(cls, value: str) -> str:
        # Reject obviously oversized input before decoding it. A base64
        # string for N bytes is at most 4 * ceil(N / 3) characters.
        max_encoded_chars = 4 * ((MAX_IMAGE_ATTACHMENT_BYTES + 2) // 3)
        if len(value) > max_encoded_chars:
            raise ValueError("image data exceeds the 20MB limit")
        try:
            decoded = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("data must be valid base64") from exc
        if len(decoded) > MAX_IMAGE_ATTACHMENT_BYTES:
            raise ValueError("image data exceeds the 20MB limit")
        return value


class SendMessageRequest(BaseModel):
    text: str
    max_turns: int = Field(default=0, ge=0, strict=True)
    session_id: str | None = None
    operation_id: str | None = Field(default=None, min_length=1)
    images: list[ImageAttachment] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_content(self) -> "SendMessageRequest":
        if not self.text.strip() and not self.images:
            raise ValueError("text or at least one image is required")
        return self


class NewSessionRequest(BaseModel):
    cwd: str | None = None
    additional_directories: list[str] = Field(default_factory=list)
    model: str | None = None
    provider: str | None = None
    base_url: str | None = None
    api_format: str | None = None
    model_profile: str | None = None


class ResumeSessionRequest(BaseModel):
    session_id: str
    additional_directories: list[str] = Field(default_factory=list)
    model: str | None = None
    provider: str | None = None
    base_url: str | None = None
    api_format: str | None = None
    model_profile: str | None = None


class CompactRequest(BaseModel):
    session_id: str
    custom_instructions: str | None = None


class ClearSessionRequest(BaseModel):
    session_id: str


class InterruptRequest(BaseModel):
    session_id: str
    operation_id: str | None = Field(default=None, min_length=1)


class PermissionResponseRequest(BaseModel):
    tool_use_id: str
    allowed: bool
    always_allow: bool = False
    agent_id: str | None = None
    feedback: str | None = None
    # Optional for multi-session clients; omitted keeps default-session behavior.
    session_id: str | None = None


class ChoiceResponseRequest(BaseModel):
    tool_use_id: str
    selected: list[str]
    cancelled: bool = False
    agent_id: str | None = None
    # Optional for multi-session clients; omitted keeps default-session behavior.
    session_id: str | None = None


class SpawnAgentRequest(BaseModel):
    prompt: str = Field(min_length=1)
    subagent_type: str = Field(default="generalPurpose", min_length=1)
    name: str | None = None
    model_profile: str | None = None
    callback: bool = False
    session_id: str | None = None

    @field_validator("prompt", "subagent_type")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value


class AgentInputRequest(BaseModel):
    prompt: str = Field(min_length=1)
    interrupt: bool = False
    session_id: str | None = None

    @field_validator("prompt")
    @classmethod
    def validate_prompt(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("prompt must not be blank")
        return value


class WaitAgentRequest(BaseModel):
    agent_id: str | list[str]
    timeout_ms: int | None = Field(default=None, ge=0, strict=True)
    session_id: str | None = None

    @field_validator("agent_id")
    @classmethod
    def validate_agent_ids(cls, value: str | list[str]) -> str | list[str]:
        values = [value] if isinstance(value, str) else value
        if not values or any(not isinstance(item, str) or not item.strip() for item in values):
            raise ValueError("agent_id must contain at least one non-blank ID")
        return value


class SwitchModelRequest(BaseModel):
    name: str
    session_id: str | None = None


class SwitchModeRequest(BaseModel):
    mode: Literal["agent", "plan"]
    session_id: str | None = None


class SetReasoningEffortRequest(BaseModel):
    effort: ReasoningEffort
    session_id: str | None = None


class SetUltraModeRequest(BaseModel):
    # Omitted/null means toggle; an explicit boolean is idempotent.
    enabled: bool | None = None
    session_id: str | None = None


class SetPermissionModeRequest(BaseModel):
    """Client-side tool permission override used by chat surfaces."""

    mode: Literal["default", "ask", "run_everything", "ai_review"]
    session_id: str | None = None


class GoalRequest(BaseModel):
    action: Literal[
        "set", "edit", "pause", "resume", "complete", "blocked", "clear"
    ] = "set"
    objective: str | None = None
    token_budget: int | None = Field(default=None, gt=0)
    session_id: str | None = None


class SkillExpandRequest(BaseModel):
    name: str
    user_input: str = ""
    session_id: str | None = None


class TaskStopRequest(BaseModel):
    task_id: str
    session_id: str | None = None


class ScheduleCreateRequest(BaseModel):
    name: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    schedule: str = Field(min_length=1)
    schedule_type: Literal["cron", "interval", "once"]
    cwd: str | None = None
    enabled: bool = True
    max_runs: int | None = Field(default=None, gt=0)
    next_run: str | None = None
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    timeout: int | None = Field(default=None, gt=0)
    model_profile: str | None = None
    # The Gateway session that owns this request is ``session_id``. A job can
    # independently reuse a session for its executions via ``job_session_id``.
    job_session_id: str | None = Field(default=None, min_length=1)
    extra: dict[str, Any] = Field(default_factory=dict)
    session_id: str | None = None

    @field_validator("name", "prompt", "schedule")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value

    @field_validator("job_session_id")
    @classmethod
    def validate_job_session_id(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("job_session_id must not be blank")
        return value


class ScheduleJobRequest(BaseModel):
    job_id: str = Field(min_length=1)
    session_id: str | None = None
    scope: Literal["global"] | None = None


class PeerSendRequest(BaseModel):
    to: str = Field(min_length=1)
    text: str = Field(min_length=1)
    session_id: str | None = None

    @field_validator("to", "text")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value


class TeamCreateRequest(BaseModel):
    name: str = Field(min_length=1)
    max_teammates: int | None = Field(default=None, gt=0)
    session_id: str | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must not be blank")
        return value


class TeamSpawnRequest(BaseModel):
    team_id: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    role: Literal["lead", "worker", "researcher", "reviewer"] = "worker"
    name: str | None = None
    model_profile: str | None = None
    session_id: str | None = None

    @field_validator("team_id", "prompt")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value


class TeamMessageRequest(BaseModel):
    team_id: str = Field(min_length=1)
    to: str = Field(min_length=1)
    text: str = Field(min_length=1)
    from_agent: str = ""
    session_id: str | None = None

    @field_validator("team_id", "to", "text")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value


class TeamBroadcastRequest(BaseModel):
    team_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    from_agent: str = ""
    session_id: str | None = None

    @field_validator("team_id", "text")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value


class TeamTaskAddRequest(BaseModel):
    team_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    session_id: str | None = None

    @field_validator("team_id", "description")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value


class TeamTaskClaimRequest(BaseModel):
    team_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    agent_id: str = ""
    session_id: str | None = None

    @field_validator("team_id", "task_id")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value


class TeamTaskCompleteRequest(BaseModel):
    team_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    result: str = ""
    agent_id: str = ""
    session_id: str | None = None

    @field_validator("team_id", "task_id")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value


class TeamTaskFailRequest(BaseModel):
    team_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    reason: str = ""
    agent_id: str = ""
    session_id: str | None = None

    @field_validator("team_id", "task_id")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value


class TeamRemoveRequest(BaseModel):
    team_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    session_id: str | None = None

    @field_validator("team_id", "agent_id")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value


class TeamMessagesReadRequest(BaseModel):
    team_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    message_ids: list[str] | None = None
    session_id: str | None = None

    @field_validator("team_id", "agent_id")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value

    @field_validator("message_ids")
    @classmethod
    def validate_message_ids(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and any(not item.strip() for item in value):
            raise ValueError("message_ids must not contain blank IDs")
        return value


class TeamBridgeRequest(BaseModel):
    team_a: str = Field(min_length=1)
    team_b: str = Field(min_length=1)
    policy: Literal["allow_all", "allow_tagged", "deny"] = "allow_tagged"
    session_id: str | None = None

    @field_validator("team_a", "team_b")
    @classmethod
    def validate_team_ids(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("team ID must not be blank")
        return value

    @model_validator(mode="after")
    def validate_distinct_teams(self) -> "TeamBridgeRequest":
        if self.team_a == self.team_b:
            raise ValueError("a bridge requires two distinct teams")
        return self


class TeamCrossMessageRequest(BaseModel):
    from_team: str = Field(min_length=1)
    to_team: str = Field(min_length=1)
    text: str = Field(min_length=1)
    from_agent: str = ""
    to_agent: str = ""
    session_id: str | None = None

    @field_validator("from_team", "to_team", "text")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value

    @model_validator(mode="after")
    def validate_distinct_teams(self) -> "TeamCrossMessageRequest":
        if self.from_team == self.to_team:
            raise ValueError("cross-team messages require two distinct teams")
        return self


class TeamShutdownRequest(BaseModel):
    team_id: str = Field(min_length=1)
    session_id: str | None = None

    @field_validator("team_id")
    @classmethod
    def validate_team_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("team_id must not be blank")
        return value


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


class SearchSessionsRequest(BaseModel):
    query: str
    limit: int = 20


class ArchiveSessionRequest(BaseModel):
    session_id: str


class PruneSessionsRequest(BaseModel):
    days: int = Field(default=30, ge=0, le=36500)
    delete_files: bool = False


class ExportSessionRequest(BaseModel):
    session_id: str
    format: Literal["md", "json"] = "md"


class WorkspaceDirectoryCreateRequest(BaseModel):
    path: str = Field(min_length=1)


# ── Response / event schemas ─────────────────────────────────────


class SessionInfo(BaseModel):
    session_id: str
    message_count: int = 0
    model: str = ""
    provider: str = ""
    created_at: str = ""
    title: str = ""
    cwd: str = ""
    tokens_used: int = 0
    preview: str = ""


class WorkspaceInfo(BaseModel):
    startup_cwd: str
    home: str
    browse_roots: list[str] = Field(default_factory=list)
    documents_dir: str = ""


class WorkspaceDirectoryEntry(BaseModel):
    name: str
    path: str
    hidden: bool = False
    is_symlink: bool = False


class WorkspaceDirectoryListing(BaseModel):
    path: str
    parent: str | None = None
    directories: list[WorkspaceDirectoryEntry] = Field(default_factory=list)


class SearchIndexStatus(BaseModel):
    """Observable state for the optional semantic-search indexer."""

    state: str
    chunks: int | None = None
    files: int | None = None
    done: int | None = None
    total: int | None = None


class SessionRuntimeStatus(BaseModel):
    """Complete, non-secret runtime status shared by CLI-style clients."""

    version: str = VERSION
    session_id: str
    cwd: str = ""
    initialized: bool = False
    message_count: int = 0
    model: str = ""
    model_profile: str | None = None
    provider: str = ""
    mode: Literal["agent", "plan"] = "agent"
    reasoning_effort: ReasoningEffort | None = None
    ultra_mode: bool = False
    permission_mode: str = "default"
    context_used_tokens: int = 0
    context_window_tokens: int = 0
    context_remaining_tokens: int = 0
    context_used_percent: float = 0.0
    compact_count: int = 0
    auto_compact_enabled: bool = True
    thinking_enabled: bool = False
    max_tokens: int = 0
    tool_count: int | None = None
    agent_total: int = 0
    agent_active: int = 0
    agent_failed: int = 0
    agent_pending_callbacks: int = 0
    agent_max_concurrency: int = 0
    monitor_total: int = 0
    monitor_active: int = 0
    monitor_failed: int = 0
    search_index: SearchIndexStatus | None = None


class GoalInfo(BaseModel):
    objective: str
    status: Literal["active", "paused", "complete", "blocked"]
    token_budget: int | None = None
    tokens_used: int = 0
    created_at: str
    updated_at: str
    completed_at: str | None = None


class GoalState(BaseModel):
    goal: GoalInfo | None = None


class AgentInfo(BaseModel):
    agent_id: str
    session_id: str = ""
    parent_agent_id: str | None = None
    parent_tool_use_id: str | None = None
    title: str
    subagent_type: str
    status: str
    model: str
    model_profile: str | None = None
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    updated_at: str = ""
    usage: dict[str, Any] = Field(default_factory=dict)
    final_result: str = ""
    error: str = ""
    depth: int = 0
    transcript_path: str | None = None
    callback_enabled: bool = False
    callback_state: str = "disabled"
    callback_message_id: str | None = None
    callback_epoch: int = 0


class ToolInfo(BaseModel):
    name: str
    description: str = ""
    is_read_only: bool = False
    is_enabled: bool = True


class SkillInfo(BaseModel):
    name: str
    description: str = ""


class SkillExpansion(BaseModel):
    name: str
    prompt: str


class MonitorInfo(BaseModel):
    task_id: str
    session_id: str = ""
    description: str = ""
    task_type: str = "local_bash"
    source: str = ""
    status: str = ""
    output_file: str | None = None
    timeout_ms: int = 0
    persistent: bool = False
    tool_use_id: str | None = None
    created_at: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    updated_at: str = ""
    sequence: int = 0
    error: str = ""
    exit_code: int | None = None


class BackgroundTaskInfo(BaseModel):
    task_id: str
    session_id: str = ""
    cwd: str = ""
    description: str = ""
    task_type: str = ""
    source: str = ""
    status: str = ""
    output_file: str | None = None
    created_at: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    updated_at: str = ""
    error: str = ""
    exit_code: int | None = None
    agent_id: str | None = None


class ScheduleJobInfo(BaseModel):
    id: str
    name: str
    prompt: str
    schedule: str
    schedule_type: Literal["cron", "interval", "once"]
    cwd: str | None = None
    enabled: bool = True
    status: str
    last_run: str | None = None
    next_run: str | None = None
    run_count: int = 0
    max_runs: int | None = None
    created_at: str
    session_id: str | None = None
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    timeout: int | None = None
    model_profile: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)
    running: bool = False


class ScheduleRunInfo(BaseModel):
    id: str
    job_id: str
    status: str
    started_at: str | None = None
    finished_at: str | None = None
    duration_seconds: float | None = None
    session_id: str | None = None
    exit_code: int | None = None
    error_message: str | None = None
    result_summary: str = ""
    tokens_used: int = 0
    created_at: str


class PeerInfo(BaseModel):
    version: int = 1
    session_id: str
    name: str
    cwd: str
    pid: int
    socket_path: str = ""
    permission_class: Literal["prompting", "bypass"] = "prompting"
    started_at: str = ""


class PeerDeliveryInfo(BaseModel):
    message_id: str = ""
    status: Literal["delivered", "held", "refused", "failed"]
    detail: str = ""


class TeamTeammateInfo(BaseModel):
    agent_id: str
    name: str | None = None
    role: str
    state: str
    model_profile: str | None = None


class TeamTaskInfo(BaseModel):
    id: str
    description: str = ""
    assignee: str | None = None
    status: str
    result: str = ""
    created_at: str = ""
    updated_at: str = ""


class TeamTaskSummary(BaseModel):
    total: int = 0
    pending: int = 0
    claimed: int = 0
    completed: int = 0
    failed: int = 0


class TeamStatusInfo(BaseModel):
    team_id: str
    state: str
    teammates: list[TeamTeammateInfo] = Field(default_factory=list)
    teammate_count: int = 0
    max_teammates: int = 0
    tasks: TeamTaskSummary = Field(default_factory=TeamTaskSummary)


class TeamMessageInfo(BaseModel):
    id: str
    from_agent: str = ""
    to_agent: str = ""
    text: str = ""
    timestamp: str = ""
    read: bool = False
    msg_type: str = "text"


class TeamBridgeInfo(BaseModel):
    team_a: str
    team_b: str
    policy: Literal["allow_all", "allow_tagged", "deny"]


class CrossTeamMessageInfo(BaseModel):
    id: str
    from_team: str
    from_agent: str = ""
    to_team: str
    to_agent: str = ""
    text: str
    timestamp: str = ""
    bridge_policy: Literal["allow_all", "allow_tagged", "deny"]


class LogInfo(BaseModel):
    name: str
    path: str
    updated_at: str | None = None
    state: str | None = None


class LogsResponse(BaseModel):
    logs: list[LogInfo] = Field(default_factory=list)
    lines: list[str] = Field(default_factory=list)
    name: str | None = None
    path: str | None = None
    truncated: bool = False
    note: str | None = None


class AgentTranscriptResponse(BaseModel):
    agent_id: str
    path: str | None = None
    lines: list[str] = Field(default_factory=list)
    truncated: bool = False


class ModelInfo(BaseModel):
    name: str
    description: str = ""


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = VERSION
    protocol_version: int = GATEWAY_PROTOCOL_VERSION
    min_protocol_version: int = GATEWAY_MIN_PROTOCOL_VERSION
    max_protocol_version: int = GATEWAY_MAX_PROTOCOL_VERSION


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
    tool_input: dict[str, Any] = Field(default_factory=dict)
    agent_id: str | None = None


class PermissionRequestPayload(BaseModel):
    type: Literal["permission_request"] = "permission_request"
    tool_name: str
    tool_input: dict[str, Any]
    tool_use_id: str
    reason: str | None = None
    permission_key: str | None = None
    agent_id: str | None = None
    request_kind: Literal["tool", "peer_message"] = "tool"


class PermissionResponsePayload(BaseModel):
    type: Literal["permission_response"] = "permission_response"
    tool_use_id: str
    allowed: bool
    always_allow: bool = False
    agent_id: str | None = None
    feedback: str | None = None


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
    trigger: str = "auto"
    agent_id: str | None = None


class ErrorPayload(BaseModel):
    type: Literal["error"] = "error"
    message: str
    recoverable: bool = True
    error_type: str = ""
    agent_id: str | None = None
    # Errors generated while admitting/validating a transport command are
    # deliberately distinct from Core turn errors.  They may carry the
    # command and target operation without claiming foreground terminal
    # ownership.
    command: str | None = None
    command_error: bool = False


class TurnCompletePayload(BaseModel):
    type: Literal["turn_complete"] = "turn_complete"
    reason: str = "end_turn"
    turn_count: int = 0
    usage: dict[str, Any] = Field(default_factory=dict)
    context_used_tokens: int = 0
    context_window_tokens: int = 0
    context_remaining_tokens: int = 0
    context_used_percent: float = 0.0


class StreamModePayload(BaseModel):
    type: Literal["stream_mode"] = "stream_mode"
    mode: str
    agent_id: str | None = None


class SteeringAppliedPayload(BaseModel):
    type: Literal["steering_applied"] = "steering_applied"
    count: int = 1


class DocumentJobPayload(BaseModel):
    type: Literal["document_job"] = "document_job"
    action: str
    status: str
    locale: str = ""
    source: str = ""
    current: int = 0
    total: int = 0
    message: str = ""


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


class ModelChangePayload(BaseModel):
    type: Literal["model_change"] = "model_change"
    session_id: str
    model_profile: str


class PermissionModeChangePayload(BaseModel):
    type: Literal["permission_mode_change"] = "permission_mode_change"
    session_id: str
    permission_mode: Literal["default", "ask", "ai_review", "run_everything"]


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


class PeerMessagePayload(BaseModel):
    type: Literal["peer_message"] = "peer_message"
    message_id: str
    from_session_id: str
    from_name: str
    from_cwd: str
    text: str


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


class ScheduleRunPayload(BaseModel):
    """A scheduled job execution result delivered to connected clients."""

    type: Literal["schedule_run"] = "schedule_run"
    job_id: str
    run_id: str
    status: str
    duration_seconds: float = 0.0
    error_message: str = ""
    result_summary: str = ""
    next_run: str | None = None


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


class SessionMessagePayload(BaseModel):
    """Complete persisted message shape used when replaying a session."""

    uuid: str
    role: Literal["user", "assistant", "system"]
    content: str | list[dict[str, Any]]
    timestamp: str
    parent_uuid: str | None = None
    is_compact_summary: bool = False
    origin: str | None = None
    usage: dict[str, Any] | None = None
    tool_use_result: str | None = None
    source_tool_assistant_uuid: str | None = None
    reply_to_uuid: str | None = None
    api_error: str | None = None
    request_id: str | None = None


class SessionHistoryPayload(BaseModel):
    type: Literal["session_history"] = "session_history"
    session_id: str
    messages: list[SessionMessagePayload] = Field(default_factory=list)


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
    SteeringAppliedPayload,
    DocumentJobPayload,
    AgentStatePayload,
    AgentOutputPayload,
    ModeChangePayload,
    ModelChangePayload,
    PermissionModeChangePayload,
    PlanReadyPayload,
    PeerMessagePayload,
    TeamMessagePayload,
    TeamStatePayload,
    TaskUpdatePayload,
    ScheduleRunPayload,
    FileChangePayload,
    SnapshotPayload,
    RevertPayload,
    ServerConnectedPayload,
    ServerHeartbeatPayload,
    SessionHistoryPayload,
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
        DocumentJobEvent,
        ErrorEvent,
        ModeChangeEvent,
        PermissionRequestEvent,
        PermissionResponseEvent,
        PeerMessageEvent,
        PlanReadyEvent,
        ScheduleRunEvent,
        StreamModeEvent,
        SteeringAppliedEvent,
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
            tool_input=event.tool_input,
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
            request_kind=event.request_kind,
        )
    if isinstance(event, PermissionResponseEvent):
        return PermissionResponsePayload(
            tool_use_id=event.tool_use_id,
            allowed=event.allowed,
            always_allow=event.always_allow,
            agent_id=event.agent_id,
            feedback=event.feedback,
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
            trigger=event.trigger,
            agent_id=event.agent_id,
        )
    if isinstance(event, ErrorEvent):
        return ErrorPayload(
            message=event.message,
            recoverable=event.recoverable,
            error_type=event.error_type,
            agent_id=event.agent_id,
        )
    if isinstance(event, TurnCompleteEvent):
        return TurnCompletePayload(
            reason=event.reason,
            turn_count=event.turn_count,
            usage=event.usage,
            context_used_tokens=event.context_used_tokens,
            context_window_tokens=event.context_window_tokens,
            context_remaining_tokens=event.context_remaining_tokens,
            context_used_percent=event.context_used_percent,
        )
    if isinstance(event, StreamModeEvent):
        return StreamModePayload(mode=event.mode, agent_id=event.agent_id)
    if isinstance(event, SteeringAppliedEvent):
        return SteeringAppliedPayload(count=event.count)
    if isinstance(event, DocumentJobEvent):
        return DocumentJobPayload(
            action=event.action,
            status=event.status,
            locale=event.locale,
            source=event.source,
            current=event.current,
            total=event.total,
            message=event.message,
        )
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
    if isinstance(event, PeerMessageEvent):
        return PeerMessagePayload(
            message_id=event.message_id,
            from_session_id=event.from_session_id,
            from_name=event.from_name,
            from_cwd=event.from_cwd,
            text=event.text,
        )
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
    if isinstance(event, ScheduleRunEvent):
        return ScheduleRunPayload(
            job_id=event.job_id,
            run_id=event.run_id,
            status=event.status,
            duration_seconds=event.duration_seconds,
            error_message=event.error_message,
            result_summary=event.result_summary,
            next_run=event.next_run,
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
