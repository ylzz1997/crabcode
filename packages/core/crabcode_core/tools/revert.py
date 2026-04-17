"""RevertTool — revert files and conversation to a previous checkpoint."""

from __future__ import annotations

from typing import Any

from crabcode_core.types.tool import Tool, ToolContext, ToolResult


class RevertTool(Tool):
    name = "Revert"
    description = "Revert files and conversation to a previous checkpoint."
    is_read_only = False
    is_concurrency_safe = False
    input_schema = {
        "type": "object",
        "properties": {
            "checkpoint_id": {
                "type": "string",
                "description": "The checkpoint ID to revert to. Use 'latest' to revert the most recent checkpoint.",
            },
        },
        "required": ["checkpoint_id"],
    }

    async def get_prompt(self, **kwargs: Any) -> str:
        return (
            "Revert both files and conversation to a previously created checkpoint. "
            "This restores the file system to the snapshot taken at checkpoint time "
            "and rolls back the conversation history.\n\n"
            "Use this tool when:\n"
            "- A change went wrong and you need to undo it\n"
            "- You want to try a different approach after a failed attempt\n"
            "- The user asks to undo recent changes\n\n"
            "You can pass 'latest' as the checkpoint_id to revert the most recent checkpoint. "
            "To see available checkpoints, use action='list' first.\n\n"
            "WARNING: This is a destructive operation — any changes made after the "
            "checkpoint will be lost. Use carefully."
        )

    async def validate_input(self, tool_input: dict[str, Any]) -> str | None:
        checkpoint_id = tool_input.get("checkpoint_id")
        if not checkpoint_id:
            return "checkpoint_id is required"
        return None

    async def call(
        self,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        session = getattr(context, "session", None)
        if not session:
            return ToolResult(
                result_for_model="No active session; cannot revert.",
                is_error=True,
            )

        checkpoint_id = tool_input["checkpoint_id"]

        # Resolve 'latest' to the most recent checkpoint
        if checkpoint_id == "latest":
            cps = session.list_checkpoints()
            if not cps:
                return ToolResult(
                    result_for_model="No checkpoints exist to revert to.",
                    is_error=True,
                )
            checkpoint_id = cps[0]["id"]

        result = session.revert(checkpoint_id)

        if not result.get("success"):
            return ToolResult(
                result_for_model=f"Revert failed. Checkpoint '{checkpoint_id[:8]}…' not found or revert unsuccessful.",
                is_error=True,
            )

        parts = [f"Reverted to checkpoint {checkpoint_id[:8]}…"]
        if result.get("files_restored"):
            parts.append(f"Files restored: {len(result['files_restored'])}")
        if result.get("messages_rolled_back"):
            parts.append(f"Messages rolled back: {result['messages_rolled_back']}")
        if result.get("warning"):
            parts.append(f"Warning: {result['warning']}")

        return ToolResult(
            data=result,
            result_for_model=". ".join(parts),
        )
