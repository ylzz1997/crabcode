"""FileWriteTool — create or overwrite files."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from crabcode_core.logging_utils import get_logger
from crabcode_core.tools.diff_utils import compute_diff, format_edit_summary
from crabcode_core.types.tool import Tool, ToolContext, ToolResult

logger = get_logger(__name__)


class FileWriteTool(Tool):
    name = "Write"
    description = "Write content to a file, creating it if necessary."
    is_read_only = False
    is_concurrency_safe = False
    input_schema = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "The path of the file to write.",
            },
            "content": {
                "type": "string",
                "description": "The content to write to the file.",
            },
        },
        "required": ["file_path", "content"],
    }

    async def get_prompt(self, **kwargs: Any) -> str:
        return (
            "Write content to a file. Creates the file and parent directories "
            "if they don't exist. WARNING: This will overwrite the existing "
            "file if there is one.\n\n"
            "ALWAYS prefer editing existing files with the Edit tool instead "
            "of rewriting them. Only use Write for:\n"
            "- Creating new files that don't exist yet\n"
            "- Cases where the entire file content needs to be replaced\n\n"
            "NEVER create files unless absolutely necessary for achieving "
            "your goal. This prevents file bloat and builds on existing "
            "work more effectively.\n"
            "NEVER proactively create documentation or README files unless "
            "the user explicitly requests them."
        )

    async def call(
        self,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        file_path = tool_input.get("file_path", "")
        content = tool_input.get("content", "")

        if not file_path:
            return ToolResult(
                result_for_model="Error: file_path is required",
                is_error=True,
            )

        path = Path(file_path)
        if not path.is_absolute():
            path = Path(context.cwd) / path

        is_new = not path.exists()
        old_content = ""
        if not is_new:
            try:
                old_content = path.read_text(errors="replace")
            except Exception:
                logger.debug("Failed to read existing file before overwrite: %s", path, exc_info=True)

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        except Exception as e:
            return ToolResult(
                result_for_model=f"Error writing file: {e}",
                is_error=True,
            )

        if is_new:
            line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
            return ToolResult(
                data={"file_path": str(path), "created": True, "lines": line_count},
                result_for_model=f"Created {path} ({line_count} lines).",
            )

        diff_info = compute_diff(old_content, content, str(path))
        model_msg, display_msg = format_edit_summary(str(path), diff_info)

        return ToolResult(
            data={
                "file_path": str(path),
                "created": False,
                "line_range": diff_info["line_range"],
                "stats": diff_info["stats"],
            },
            result_for_model=model_msg,
            result_for_display=display_msg,
        )
