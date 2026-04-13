"""GlobTool — find files by pattern."""

from __future__ import annotations

import asyncio
import fnmatch
from pathlib import Path
from typing import Any

from crabcode_core.tools._input_helpers import first_non_empty_str
from crabcode_core.types.tool import Tool, ToolContext, ToolResult


class GlobTool(Tool):
    name = "Glob"
    description = "Find files matching a glob pattern."
    is_read_only = True
    is_concurrency_safe = True
    input_schema = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "minLength": 1,
                "description": "Glob pattern to match (e.g., '**/*.py', 'src/**/*.ts').",
            },
            "path": {
                "type": "string",
                "description": "Directory to search in (default: cwd).",
            },
        },
        "required": ["pattern"],
    }

    async def get_prompt(self, **kwargs: Any) -> str:
        return (
            "Find files matching a glob pattern. Returns matching file paths "
            "sorted by modification time (most recent first). Works fast "
            "with codebases of any size.\n\n"
            "Patterns not starting with '**/' are automatically prepended "
            "with it for recursive searching. Use this instead of find or "
            "ls via Bash.\n\n"
            "Examples:\n"
            '- "*.py" (becomes "**/*.py") — find all Python files\n'
            '- "src/**/*.ts" — find all TypeScript files under src/\n'
            '- "**/test_*.py" — find all test files'
        )

    async def call(
        self,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        pattern = first_non_empty_str(
            tool_input,
            ("pattern", "glob_pattern", "file_pattern", "glob", "match", "include"),
        )
        search_path = tool_input.get("path", context.cwd)

        if not pattern:
            return ToolResult(
                result_for_model=(
                    "Error: pattern is required. Pass a non-empty glob string "
                    "(e.g. \"**/*.py\"); aliases: glob_pattern, file_pattern, glob, "
                    "match, include."
                ),
                is_error=True,
            )

        if not pattern.startswith("**/") and not pattern.startswith("/"):
            pattern = f"**/{pattern}"

        root = Path(search_path)
        if not root.is_absolute():
            root = Path(context.cwd) / root

        if not root.exists():
            return ToolResult(
                result_for_model=f"Error: directory not found: {root}",
                is_error=True,
            )

        try:
            matches: list[tuple[float, str]] = []
            for path in root.glob(pattern):
                if path.is_file():
                    try:
                        mtime = path.stat().st_mtime
                    except OSError:
                        mtime = 0
                    matches.append((mtime, str(path)))

                if len(matches) > 1000:
                    break

            matches.sort(key=lambda x: x[0], reverse=True)
            file_paths = [p for _, p in matches]

        except Exception as e:
            return ToolResult(
                result_for_model=f"Error during glob: {e}",
                is_error=True,
            )

        if not file_paths:
            return ToolResult(
                result_for_model="No files matched the pattern.",
                data={"count": 0},
            )

        result_text = "\n".join(file_paths)
        count = len(file_paths)
        suffix = " (showing first 1000)" if count >= 1000 else ""

        return ToolResult(
            data={"count": count, "files": file_paths},
            result_for_model=f"Found {count} files{suffix}:\n{result_text}",
        )
