"""SwitchMode tool — allows the agent to request a mode switch and submit plans."""

from __future__ import annotations

import json
from typing import Any

from crabcode_core.types.event import ModeChangeEvent, PlanReadyEvent
from crabcode_core.types.tool import Tool, ToolContext, ToolResult


class SwitchModeTool(Tool):
    name = "SwitchMode"
    description = "Switch between plan mode (read-only planning) and agent mode (full execution). In plan mode, use this to submit a structured execution plan."
    is_read_only = True
    is_concurrency_safe = False
    input_schema = {
        "type": "object",
        "properties": {
            "target_mode": {
                "type": "string",
                "enum": ["agent", "plan"],
                "description": "The mode to switch to.",
            },
            "explanation": {
                "type": "string",
                "description": "Brief explanation for why the mode switch is requested.",
            },
            "plan": {
                "type": "object",
                "description": (
                    "A structured execution plan to submit when switching from plan to agent mode. "
                    "Required when switching from plan to agent. Schema: "
                    '{"title": str, "summary": str, "steps": [{"id": str, "title": str, '
                    '"description": str, "files": [str], "depends_on": [str], "subagent_type": str}]}'
                ),
            },
        },
        "required": ["target_mode"],
    }

    # Set by CoreSession before each query loop turn so the tool
    # prompt can reflect the current mode.
    current_mode: str = "agent"

    def _build_description(self) -> str:
        """Build the dynamic tool description based on current_mode."""
        base = self.description
        if self.current_mode == "plan":
            return (
                base
                + " IMPORTANT: You are currently in plan mode — "
                "only use target_mode='agent' (with a plan) to submit your plan. "
                "Do NOT use target_mode='plan' as you are already in plan mode."
            )
        return (
            base
            + " IMPORTANT: You are currently in agent mode — "
            "only use target_mode='plan' if you want to switch to read-only planning. "
            "Do NOT use target_mode='agent' as you are already in agent mode."
        )

    def to_api_schema(self) -> dict[str, Any]:
        """Override to use dynamic description instead of cached prompt."""
        return {
            "name": self.name,
            "description": self._build_description(),
            "input_schema": self.input_schema,
        }

    async def get_prompt(self, **kwargs: Any) -> str:
        mode_note = ""
        if self.current_mode == "plan":
            mode_note = (
                "\n\nIMPORTANT: You are currently in plan mode. "
                "Do NOT call this tool with target_mode='plan' — you are already in plan mode. "
                "Use target_mode='agent' only when you are ready to submit your execution plan."
            )
        else:
            mode_note = (
                "\n\nIMPORTANT: You are currently in agent mode. "
                "Only call this tool if you want to switch to plan mode for read-only planning. "
                "Do NOT call this tool with target_mode='agent' unless you are in plan mode "
                "submitting a plan."
            )

        return (
            "Switch between plan mode (read-only analysis and planning) and agent mode "
            "(full tool access for executing changes).\n\n"
            "Use cases:\n"
            "- In plan mode: after gathering context and designing a plan, call this tool "
            "with target_mode='agent' and a structured plan in the 'plan' field to submit "
            "the plan to the interface for review. This ends the current turn. Do not call "
            "other tools afterwards, and do not assume execution has started.\n"
            "- In agent mode: call with target_mode='plan' to switch to read-only planning mode.\n\n"
            "When submitting a plan (switching plan -> agent), the 'plan' field is required. "
            "The plan should contain:\n"
            "- title: short plan title\n"
            "- summary: 1-3 sentence overview\n"
            "- steps: array of execution steps, each with:\n"
            "  - id: unique short identifier (e.g. 's1', 's2')\n"
            "  - title: one-line step description\n"
            "  - description: detailed prompt for the sub-agent executing this step\n"
            "  - files: file paths this step will modify\n"
            "  - depends_on: ids of steps that must complete first (for DAG scheduling)\n"
            "  - subagent_type: 'generalPurpose' (default) or 'explore'\n\n"
            "Steps with no dependencies run in parallel. Design steps for maximum parallelism."
            + mode_note
        )

    async def validate_input(self, tool_input: dict[str, Any]) -> str | None:
        target = tool_input.get("target_mode")
        if target not in ("agent", "plan"):
            return "target_mode must be 'agent' or 'plan'"
        if target == self.current_mode:
            return (
                f"You are already in {target} mode. "
                f"Switching to the same mode is unnecessary — continue with your current work."
            )
        return None

    async def call(
        self,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        target_mode = tool_input["target_mode"]
        explanation = tool_input.get("explanation", "")
        plan_data = tool_input.get("plan")

        if target_mode == "agent" and plan_data:
            from crabcode_core.plan.types import ExecutionPlan

            try:
                plan = ExecutionPlan.from_dict(plan_data)
            except Exception as e:
                return ToolResult(
                    result_for_model=f"Invalid plan structure: {e}",
                    is_error=True,
                )

            errors = plan.validate_dag()
            if errors:
                return ToolResult(
                    result_for_model=f"Plan validation failed:\n" + "\n".join(f"- {e}" for e in errors),
                    is_error=True,
                )

            if context.tool_event_queue:
                await context.tool_event_queue.put(
                    PlanReadyEvent(plan=plan.to_dict())
                )

        if context.tool_event_queue:
            await context.tool_event_queue.put(
                ModeChangeEvent(mode=target_mode, reason=explanation)
            )

        if target_mode == "agent" and plan_data:
            return ToolResult(
                result_for_model=(
                    f"Mode switch to '{target_mode}' requested with execution plan. "
                    f"The plan has {len(plan_data.get('steps', []))} steps. "
                    f"Return control to the interface now so the user can choose whether to "
                    f"execute, revise, or cancel it. Do not call more tools in this turn."
                ),
            )

        return ToolResult(
            result_for_model=f"Mode switch to '{target_mode}' requested. {explanation}",
        )
