"""CheckpointTool — create a checkpoint (conversation + file snapshot)."""

from __future__ import annotations

from typing import Any

from crabcode_core.types.tool import Tool, ToolContext, ToolResult


class CheckpointTool(Tool):
    name = "Checkpoint"
    description = "Create a checkpoint of the current conversation and file state."
    is_read_only = False
    is_concurrency_safe = False
    input_schema = {
        "type": "object",
        "properties": {
            "label": {
                "type": "string",
                "description": "A short label for this checkpoint, e.g. 'before-refactor' or 'pre-migration'.",
            },
        },
        "required": [],
    }

    async def get_prompt(self, **kwargs: Any) -> str:
        return (
            "Create a checkpoint that saves both the current conversation state "
            "and a file-system snapshot. You can later revert to this checkpoint "
            "using the Revert tool to undo both file changes and conversation history.\n\n"
            "Use this tool proactively before making significant or risky changes, such as:\n"
            "- Large-scale refactoring across multiple files\n"
            "- Destructive operations that modify many files\n"
            "- Changes where rollback would be difficult manually\n"
            "- Before running commands that could alter the codebase unpredictably\n\n"
            "Do NOT use this tool for trivial changes (single-file edits, adding comments, etc.)."
        )

    async def call(
        self,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        session = getattr(context, "session", None)
        if not session:
            return ToolResult(
                result_for_model="No active session; cannot create checkpoint.",
                is_error=True,
            )

        label = tool_input.get("label", "")
        cp_id = session.checkpoint(label=label)

        if not cp_id:
            return ToolResult(
                result_for_model="Failed to create checkpoint (no messages or session storage unavailable).",
                is_error=True,
            )

        display = f"Checkpoint created: {cp_id[:8]}…"
        if label:
            display = f"Checkpoint created ({label}): {cp_id[:8]}…"
        return ToolResult(
            data={"checkpoint_id": cp_id, "label": label},
            result_for_model=f"{display} You can revert to this checkpoint later with the Revert tool using checkpoint_id '{cp_id[:8]}…'.",
        )
