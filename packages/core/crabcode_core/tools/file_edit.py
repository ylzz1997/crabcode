"""FileEditTool — string replacement editing."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from crabcode_core.logging_utils import get_logger
from crabcode_core.tools.diff_utils import compute_diff, format_edit_summary
from crabcode_core.types.tool import Tool, ToolContext, ToolResult

logger = get_logger(__name__)


async def _get_lsp_diagnostics(
    file_path: str | Path,
    context: ToolContext,
) -> str:
    """Collect LSP diagnostics for the edited file, return formatted string."""
    lsp = context.lsp_manager
    if lsp is None:
        return ""
    from crabcode_core.lsp.diagnostics import collect_and_format_diagnostics
    return await collect_and_format_diagnostics(lsp, str(file_path))


class FileEditTool(Tool):
    name = "Edit"
    description = "Edit a file by replacing a specific string."
    is_read_only = False
    is_concurrency_safe = False
    input_schema = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "The path of the file to edit.",
            },
            "old_string": {
                "type": "string",
                "description": "The exact string to find and replace.",
            },
            "new_string": {
                "type": "string",
                "description": "The replacement string.",
            },
            "replace_all": {
                "type": "boolean",
                "description": "Replace all occurrences (default: false).",
            },
        },
        "required": ["file_path", "old_string", "new_string"],
    }

    async def get_prompt(self, **kwargs: Any) -> str:
        return (
            "Edit a file by performing exact string replacements. "
            "The old_string must uniquely identify the text to replace — "
            "if it matches multiple locations, the edit will FAIL. Include "
            "surrounding context lines to make the match unique. Preserves "
            "exact indentation (tabs/spaces) as it appears in the file. "
            "Use replace_all=true to replace all occurrences (useful for "
            "renaming a variable across the file).\n\n"
            "IMPORTANT: You MUST read the file before editing it. Never "
            "guess the file content.\n\n"
            "GOOD example — unique match with context:\n"
            '  old_string: "def calculate(a, b):\\n    return a + b"\n'
            '  new_string: "def calculate(a, b):\\n    return a * b"\n\n'
            "BAD example — too short, likely matches multiple locations:\n"
            '  old_string: "return"\n'
            '  new_string: "return None"'
        )

    async def validate_input(self, tool_input: dict[str, Any]) -> str | None:
        if tool_input.get("old_string") == tool_input.get("new_string"):
            return "old_string and new_string must be different"
        return None

    async def call(
        self,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        file_path = tool_input.get("file_path", "")
        old_string = tool_input.get("old_string", "")
        new_string = tool_input.get("new_string", "")
        replace_all = tool_input.get("replace_all", False)

        if not file_path:
            return ToolResult(
                result_for_model="Error: file_path is required",
                is_error=True,
            )

        path = Path(file_path)
        if not path.is_absolute():
            path = Path(context.cwd) / path

        if not path.exists():
            return ToolResult(
                result_for_model=f"Error: file not found: {path}",
                is_error=True,
            )

        try:
            content = path.read_text(errors="replace")
        except Exception as e:
            return ToolResult(
                result_for_model=f"Error reading file: {e}",
                is_error=True,
            )

        count = content.count(old_string)

        if count == 0:
            # Give the model a head start: show the first 120 chars of the
            # old_string it provided so it can spot whitespace/encoding drift,
            # and remind it to re-read before retrying.
            snippet = repr(old_string[:120]) + ("..." if len(old_string) > 120 else "")
            return ToolResult(
                result_for_model=(
                    f"Error: old_string not found in {path}.\n"
                    f"The string you provided (first 120 chars): {snippet}\n"
                    "Common causes: wrong indentation (tabs vs spaces), "
                    "stale content (file was already modified), or "
                    "the string was never in this file.\n"
                    "Fix: use the Read tool to re-read the file and verify "
                    "the exact content before retrying the Edit."
                ),
                is_error=True,
            )

        if count > 1 and not replace_all:
            return ToolResult(
                result_for_model=(
                    f"Error: old_string found {count} times in {path}. "
                    "Use replace_all=true to replace all occurrences, "
                    "or provide more context to make the match unique."
                ),
                is_error=True,
            )

        if replace_all:
            new_content = content.replace(old_string, new_string)
        else:
            new_content = content.replace(old_string, new_string, 1)

        # Track snapshot before writing
        if context.session_id:
            try:
                from crabcode_core.snapshot.tracker import track_snapshot_for_file
                track_snapshot_for_file(
                    cwd=context.cwd,
                    session_id=context.session_id,
                    file_path=str(path),
                    old_content=content,
                    action="modify",
                )
            except Exception:
                logger.debug("Failed to track snapshot for edit", exc_info=True)

        try:
            path.write_text(new_content)
        except Exception as e:
            return ToolResult(
                result_for_model=f"Error writing file: {e}",
                is_error=True,
            )

        replaced = count if replace_all else 1

        diff_info = compute_diff(content, new_content, str(path))
        model_msg, display_msg = format_edit_summary(
            str(path), diff_info, replacements=replaced
        )
        lsp_msg = await _get_lsp_diagnostics(path, context)

        return ToolResult(
            data={
                "file_path": str(path),
                "replacements": replaced,
                "line_range": diff_info["line_range"],
                "stats": diff_info["stats"],
            },
            result_for_model=model_msg + lsp_msg,
            result_for_display=display_msg,
        )
