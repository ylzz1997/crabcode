"""Model-facing tools for managing the current session goal."""

from __future__ import annotations

from typing import Any

from crabcode_core.types.tool import Tool, ToolContext, ToolResult


def _session(context: ToolContext) -> Any | None:
    return context.session


def _goal_result(goal: Any, message: str) -> ToolResult:
    data = goal.to_dict()
    return ToolResult(data=data, result_for_model=f"{message}\n{data}")


class CreateGoalTool(Tool):
    name = "create_goal"
    description = (
        "Create a persistent goal only when the user explicitly requested goal-backed "
        "work. The objective must include a concrete outcome and verification criteria."
    )
    is_read_only = False
    is_concurrency_safe = False
    input_schema = {
        "type": "object",
        "properties": {
            "objective": {
                "type": "string",
                "description": "Concrete objective, scope, and completion evidence.",
            },
            "token_budget": {
                "type": "integer",
                "minimum": 1,
                "description": (
                    "Optional token budget. Set it only when the user explicitly "
                    "requested a budget."
                ),
            },
        },
        "required": ["objective"],
    }

    async def validate_input(self, tool_input: dict[str, Any]) -> str | None:
        objective = tool_input.get("objective")
        if not isinstance(objective, str) or not objective.strip():
            return "objective is required"
        budget = tool_input.get("token_budget")
        if budget is not None and (not isinstance(budget, int) or budget <= 0):
            return "token_budget must be a positive integer"
        return None

    async def call(
        self, tool_input: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        session = _session(context)
        if session is None:
            return ToolResult(
                result_for_model="Goal management is unavailable outside the main session.",
                is_error=True,
            )
        try:
            goal = session.create_goal(
                tool_input["objective"],
                token_budget=tool_input.get("token_budget"),
            )
        except (RuntimeError, ValueError) as exc:
            return ToolResult(result_for_model=str(exc), is_error=True)
        return _goal_result(goal, "Goal created.")


class GetGoalTool(Tool):
    name = "get_goal"
    description = "Get the current persistent goal and its lifecycle state."
    is_read_only = True
    is_concurrency_safe = True
    input_schema = {"type": "object", "properties": {}}

    async def call(
        self, tool_input: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        session = _session(context)
        if session is None:
            return ToolResult(
                result_for_model="Goal management is unavailable outside the main session.",
                is_error=True,
            )
        goal = session.get_goal()
        if goal is None:
            return ToolResult(data=None, result_for_model="No goal is set.")
        return _goal_result(goal, "Current goal.")


class UpdateGoalTool(Tool):
    name = "update_goal"
    description = (
        "Mark the current goal complete after verification, or blocked when it cannot "
        "make further progress without user input or an external state change."
    )
    is_read_only = False
    is_concurrency_safe = False
    input_schema = {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["complete", "blocked"],
                "description": "The terminal state to apply.",
            }
        },
        "required": ["status"],
    }

    async def validate_input(self, tool_input: dict[str, Any]) -> str | None:
        if tool_input.get("status") not in {"complete", "blocked"}:
            return "status must be 'complete' or 'blocked'"
        return None

    async def call(
        self, tool_input: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        session = _session(context)
        if session is None:
            return ToolResult(
                result_for_model="Goal management is unavailable outside the main session.",
                is_error=True,
            )
        try:
            goal = session.update_goal(tool_input["status"])
        except (RuntimeError, ValueError) as exc:
            return ToolResult(result_for_model=str(exc), is_error=True)
        return _goal_result(goal, f"Goal marked {goal.status}.")
