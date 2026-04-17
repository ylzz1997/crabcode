"""Agent Teams — flat teams with named messaging and shared task boards."""

from crabcode_core.team.models import (
    BridgePolicy,
    CrossTeamMessage,
    TaskItem,
    TaskStatus,
    TeamConfig,
    TeamMessage,
    TeamState,
    TeammateInfo,
    TeammateRole,
    TeammateState,
)

__all__ = [
    "BridgePolicy",
    "CrossTeamMessage",
    "TaskItem",
    "TaskStatus",
    "TeamConfig",
    "TeamMessage",
    "TeamState",
    "TeammateInfo",
    "TeammateRole",
    "TeammateState",
]
