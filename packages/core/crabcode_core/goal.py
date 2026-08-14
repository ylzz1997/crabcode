"""Durable session goals for long-running tasks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal
from xml.sax.saxutils import escape


GoalStatus = Literal["active", "paused", "complete", "blocked"]
GOAL_STATUSES = frozenset({"active", "paused", "complete", "blocked"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Goal:
    """One session-scoped objective and its lifecycle state."""

    objective: str
    status: GoalStatus = "active"
    token_budget: int | None = None
    tokens_used: int = 0
    created_at: str = ""
    updated_at: str = ""
    completed_at: str | None = None

    def __post_init__(self) -> None:
        self.objective = self.objective.strip()
        if not self.objective:
            raise ValueError("Goal objective cannot be empty")
        if self.status not in GOAL_STATUSES:
            raise ValueError(f"Invalid goal status: {self.status}")

        if self.token_budget is not None:
            self.token_budget = int(self.token_budget)
            if self.token_budget <= 0:
                raise ValueError("Goal token budget must be positive")
        self.tokens_used = int(self.tokens_used)
        if self.tokens_used < 0:
            raise ValueError("Goal token usage cannot be negative")

        timestamp = _now_iso()
        if not self.created_at:
            self.created_at = timestamp
        if not self.updated_at:
            self.updated_at = self.created_at
        if self.status == "complete" and not self.completed_at:
            self.completed_at = self.updated_at

    @property
    def is_terminal(self) -> bool:
        return self.status in {"complete", "blocked"}

    @property
    def remaining_tokens(self) -> int | None:
        if self.token_budget is None:
            return None
        return max(0, self.token_budget - self.tokens_used)

    def to_dict(self) -> dict[str, Any]:
        return {
            "objective": self.objective,
            "status": self.status,
            "token_budget": self.token_budget,
            "tokens_used": self.tokens_used,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Goal:
        """Restore a goal from session metadata."""
        objective = data.get("objective")
        if not isinstance(objective, str):
            raise ValueError("Persisted goal has no objective")
        status = data.get("status", "active")
        if status not in GOAL_STATUSES:
            raise ValueError(f"Invalid persisted goal status: {status}")
        return cls(
            objective=objective,
            status=status,
            token_budget=data.get("token_budget"),
            tokens_used=data.get("tokens_used", 0),
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
            completed_at=(
                str(data["completed_at"]) if data.get("completed_at") else None
            ),
        )

    def with_status(self, status: GoalStatus) -> Goal:
        """Return a copy with an updated lifecycle status."""
        if status not in GOAL_STATUSES:
            raise ValueError(f"Invalid goal status: {status}")
        timestamp = _now_iso()
        return Goal(
            objective=self.objective,
            status=status,
            token_budget=self.token_budget,
            tokens_used=self.tokens_used,
            created_at=self.created_at,
            updated_at=timestamp,
            completed_at=timestamp if status == "complete" else None,
        )

    def with_added_usage(self, tokens: int) -> Goal:
        """Return a copy with additional model token usage."""
        tokens = int(tokens)
        if tokens <= 0:
            return self
        return Goal(
            objective=self.objective,
            status=self.status,
            token_budget=self.token_budget,
            tokens_used=self.tokens_used + tokens,
            created_at=self.created_at,
            updated_at=_now_iso(),
            completed_at=self.completed_at,
        )

    def prompt_context(self) -> str:
        """Format the active goal as stable model context."""
        lines = [
            "<active-goal>",
            "The user has set a persistent goal for this session.",
            f"Objective: {escape(self.objective)}",
        ]
        if self.token_budget is not None:
            lines.extend(
                [
                    f"Token budget: {self.token_budget}",
                    f"Tokens used: {self.tokens_used}",
                    f"Tokens remaining: {self.remaining_tokens}",
                ]
            )
        lines.extend(
            [
                "Keep this objective as the primary success criterion.",
                "Do not claim completion until the requested outcome is verified.",
                "Use update_goal only when the goal is genuinely complete or blocked.",
                "</active-goal>",
            ]
        )
        return "\n".join(lines)
