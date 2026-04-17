"""Core data models for Agent Teams.

Defines Team, Teammate, Message, Task and related types.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TeamState(str, Enum):
    FORMING = "forming"
    ACTIVE = "active"
    IDLE = "idle"
    SHUTDOWN = "shutdown"


class TeammateRole(str, Enum):
    LEAD = "lead"
    WORKER = "worker"
    RESEARCHER = "researcher"
    REVIEWER = "reviewer"


class TeammateState(str, Enum):
    IDLE = "idle"
    BUSY = "busy"
    READY = "ready"
    CANCELLING = "cancelling"
    SHUTDOWN = "shutdown"

    @classmethod
    def terminal_states(cls) -> set[TeammateState]:
        return {cls.IDLE, cls.READY, cls.SHUTDOWN}

    def can_transition_to(self, target: TeammateState) -> bool:
        """Validate state machine transitions."""
        _ALLOWED: dict[TeammateState, set[TeammateState]] = {
            TeammateState.IDLE: {TeammateState.BUSY, TeammateState.SHUTDOWN},
            TeammateState.BUSY: {TeammateState.READY, TeammateState.CANCELLING, TeammateState.SHUTDOWN},
            TeammateState.READY: {TeammateState.BUSY, TeammateState.SHUTDOWN},
            TeammateState.CANCELLING: {TeammateState.READY, TeammateState.SHUTDOWN},
            TeammateState.SHUTDOWN: set(),  # terminal
        }
        return target in _ALLOWED.get(self, set())


class TaskStatus(str, Enum):
    PENDING = "pending"
    CLAIMED = "claimed"
    COMPLETED = "completed"
    FAILED = "failed"


class BridgePolicy(str, Enum):
    ALLOW_ALL = "allow_all"
    ALLOW_TAGGED = "allow_tagged"
    DENY = "deny"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class TeamConfig(BaseModel):
    """Configuration for a team."""

    name: str
    max_teammates: int = 8
    inbox_dir: str | None = None  # override default inbox storage path
    backpressure_queue_size: int = 100
    max_message_size_bytes: int = 10_000  # 10KB per message
    bridge_policy: BridgePolicy = BridgePolicy.DENY


class TeammateInfo(BaseModel):
    """Information about a teammate within a team."""

    agent_id: str
    role: TeammateRole = TeammateRole.WORKER
    state: TeammateState = TeammateState.IDLE
    model_profile: str | None = None
    name: str | None = None
    joined_at: str = Field(default_factory=_now_iso)
    updated_at: str = Field(default_factory=_now_iso)

    def transition_to(
        self,
        target: TeammateState,
        *,
        guard: bool = False,
        force: bool = False,
    ) -> bool:
        """Attempt a state transition.

        Args:
            guard: If True, skip if already in a terminal state (prevents
                   race conditions during cleanup).
            force: If True, bypass validation entirely (used in recovery).
        """
        if guard and self.state in TeammateState.terminal_states():
            return False
        if force or self.state.can_transition_to(target):
            self.state = target
            self.updated_at = _now_iso()
            return True
        return False


class TeamMessage(BaseModel):
    """A message between teammates."""

    id: str = Field(default_factory=_new_id)
    from_agent: str = ""  # agent_id of sender
    to_agent: str = ""  # agent_id of recipient (empty = broadcast)
    text: str = ""
    timestamp: str = Field(default_factory=_now_iso)
    read: bool = False
    msg_type: str = "text"  # text | task | system | delivery_receipt

    def size_bytes(self) -> int:
        return len(self.text.encode("utf-8"))


class TaskItem(BaseModel):
    """A task on the shared task board."""

    id: str = Field(default_factory=_new_id)
    description: str = ""
    assignee: str | None = None  # agent_id
    status: TaskStatus = TaskStatus.PENDING
    result: str = ""
    created_at: str = Field(default_factory=_now_iso)
    updated_at: str = Field(default_factory=_now_iso)

    def claim(self, agent_id: str) -> bool:
        """Atomically claim this task if it's still pending."""
        if self.status != TaskStatus.PENDING:
            return False
        self.status = TaskStatus.CLAIMED
        self.assignee = agent_id
        self.updated_at = _now_iso()
        return True

    def complete(self, result: str = "") -> bool:
        if self.status != TaskStatus.CLAIMED:
            return False
        self.status = TaskStatus.COMPLETED
        self.result = result
        self.updated_at = _now_iso()
        return True

    def fail(self, reason: str = "") -> bool:
        if self.status != TaskStatus.CLAIMED:
            return False
        self.status = TaskStatus.FAILED
        self.result = reason
        self.updated_at = _now_iso()
        return True


class CrossTeamMessage(BaseModel):
    """A message sent between teams via a bridge."""

    id: str = Field(default_factory=_new_id)
    from_team: str = ""
    from_agent: str = ""
    to_team: str = ""
    to_agent: str = ""  # empty = broadcast within target team
    text: str = ""
    timestamp: str = Field(default_factory=_now_iso)
    bridge_policy: BridgePolicy = BridgePolicy.ALLOW_TAGGED
