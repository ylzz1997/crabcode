"""Data structures for structured execution plans."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PlanStep:
    """A single step in an execution plan."""

    id: str
    title: str
    description: str
    files: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    subagent_type: str = "generalPurpose"
    status: str = "pending"  # pending | running | completed | failed | cancelled
    agent_id: str | None = None
    result: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "files": list(self.files),
            "depends_on": list(self.depends_on),
            "subagent_type": self.subagent_type,
            "status": self.status,
            "agent_id": self.agent_id,
            "result": self.result,
            "error": self.error,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> PlanStep:
        return PlanStep(
            id=data["id"],
            title=data.get("title", ""),
            description=data.get("description", ""),
            files=data.get("files", []),
            depends_on=data.get("depends_on", []),
            subagent_type=data.get("subagent_type", "generalPurpose"),
            status=data.get("status", "pending"),
            agent_id=data.get("agent_id"),
            result=data.get("result", ""),
            error=data.get("error", ""),
        )


@dataclass
class ExecutionPlan:
    """A structured execution plan with steps and dependency DAG."""

    title: str
    steps: list[PlanStep] = field(default_factory=list)
    summary: str = ""
    status: str = "pending"  # pending | running | completed | failed

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "summary": self.summary,
            "status": self.status,
            "steps": [s.to_dict() for s in self.steps],
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> ExecutionPlan:
        steps = [PlanStep.from_dict(s) for s in data.get("steps", [])]
        return ExecutionPlan(
            title=data.get("title", ""),
            summary=data.get("summary", ""),
            status=data.get("status", "pending"),
            steps=steps,
        )

    def validate_dag(self) -> list[str]:
        """Validate the DAG has no cycles and all dependencies exist.

        Returns a list of error messages (empty if valid).
        """
        step_ids = {s.id for s in self.steps}
        errors: list[str] = []

        for step in self.steps:
            for dep in step.depends_on:
                if dep not in step_ids:
                    errors.append(
                        f"Step '{step.id}' depends on unknown step '{dep}'"
                    )

        # Cycle detection via topological sort (Kahn's algorithm)
        in_degree: dict[str, int] = {s.id: 0 for s in self.steps}
        adjacency: dict[str, list[str]] = {s.id: [] for s in self.steps}
        for step in self.steps:
            for dep in step.depends_on:
                if dep in step_ids:
                    adjacency[dep].append(step.id)
                    in_degree[step.id] += 1

        queue = [sid for sid, deg in in_degree.items() if deg == 0]
        visited = 0
        while queue:
            node = queue.pop(0)
            visited += 1
            for neighbor in adjacency[node]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        if visited != len(self.steps):
            errors.append("Dependency graph contains a cycle")

        return errors

    def get_ready_steps(self) -> list[PlanStep]:
        """Return steps whose dependencies are all completed."""
        completed = {s.id for s in self.steps if s.status == "completed"}
        return [
            s
            for s in self.steps
            if s.status == "pending"
            and all(dep in completed for dep in s.depends_on)
        ]

    def render(self) -> str:
        """Render a human-readable view of the plan."""
        status_icons = {
            "pending": "○",
            "running": "◉",
            "completed": "●",
            "failed": "✗",
            "cancelled": "⊘",
        }
        lines = [f"Plan: {self.title}"]
        if self.summary:
            lines.append(f"  {self.summary}")
        lines.append("")
        for step in self.steps:
            icon = status_icons.get(step.status, "?")
            deps = ""
            if step.depends_on:
                deps = f" (after: {', '.join(step.depends_on)})"
            lines.append(f"  {icon} [{step.id}] {step.title}{deps}")
            if step.files:
                lines.append(f"      files: {', '.join(step.files)}")
        done = sum(1 for s in self.steps if s.status == "completed")
        total = len(self.steps)
        lines.append(f"\n  Progress: {done}/{total} steps completed")
        return "\n".join(lines)
