"""FileReadTool — read file contents."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from crabcode_core.logging_utils import get_logger
from crabcode_core.tools._input_helpers import first_non_empty_str
from crabcode_core.types.tool import Tool, ToolContext, ToolResult

logger = get_logger(__name__)


class FileReadTool(Tool):
    name = "Read"
    description = "Read the contents of a file."
    is_read_only = True
    is_concurrency_safe = True
    input_schema = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "minLength": 1,
                "description": "The absolute or relative path of the file to read.",
            },
            "offset": {
                "type": "integer",
                "description": "Line number to start reading from (1-based). Negative values count from end.",
            },
            "limit": {
                "type": "integer",
                "description": "Number of lines to read.",
            },
        },
        "required": ["file_path"],
    }

    async def get_prompt(self, **kwargs: Any) -> str:
        return (
            "Read the contents of a file from the local filesystem. "
            "You can optionally specify a line offset and limit (especially "
            "useful for large files), but it's recommended to read the whole "
            "file by not providing these parameters.\n\n"
            "Lines in the output are numbered starting at 1, in the format "
            "LINE_NUMBER|LINE_CONTENT. The LINE_NUMBER prefix is metadata — "
            "do NOT treat it as part of the actual code.\n\n"
            "IMPORTANT: You MUST read a file at least once before editing it. "
            "You can call Read on multiple files in a single response to read "
            "them in parallel.\n\n"
            "If the file exists but is empty, you will receive 'File is empty.'.\n"
            "If the file does not exist, you will receive an error."
        )

    async def call(
        self,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        file_path = first_non_empty_str(
            tool_input,
            ("file_path", "path", "target_file", "filepath", "file"),
        )
        offset = tool_input.get("offset")
        limit = tool_input.get("limit")

        if not file_path:
            return ToolResult(
                result_for_model=(
                    "Error: file_path is required. Pass the file path as \"file_path\" "
                    "(aliases: path, target_file, filepath, file)."
                ),
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

        if not path.is_file():
            return ToolResult(
                result_for_model=f"Error: not a file: {path}",
                is_error=True,
            )

        try:
            content = path.read_text(errors="replace")
        except Exception as e:
            return ToolResult(
                result_for_model=f"Error reading file: {e}",
                is_error=True,
            )

        if not content:
            return ToolResult(
                result_for_model="File is empty.",
                data={"file_path": str(path), "content": ""},
            )

        lines = content.splitlines(keepends=True)
        total_lines = len(lines)

        if offset is not None:
            if offset < 0:
                start = max(0, total_lines + offset)
            else:
                start = max(0, offset - 1)
        else:
            start = 0

        if limit is not None:
            end = min(total_lines, start + limit)
        else:
            end = total_lines

        selected = lines[start:end]

        numbered = []
        for i, line in enumerate(selected, start=start + 1):
            numbered.append(f"{i:6d}|{line.rstrip()}")

        result_text = "\n".join(numbered)

        # Background LSP touch — notifies language servers about this file
        # so diagnostics are ready when the user writes later. Non-blocking.
        lsp = context.lsp_manager
        if lsp is not None:
            try:
                asyncio.get_event_loop().create_task(
                    _lsp_touch(str(path), lsp),
                )
            except Exception:
                pass

        return ToolResult(
            data={"file_path": str(path), "content": content, "total_lines": total_lines},
            result_for_model=result_text,
        )


async def _lsp_touch(file_path: str, lsp: Any) -> None:
    """Fire-and-forget LSP touch_file call."""
    try:
        await lsp.touch_file(file_path)
    except Exception:
        logger.debug("LSP touch_file failed for %s", file_path, exc_info=True)
