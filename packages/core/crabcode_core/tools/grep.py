"""GrepTool — search file contents using ripgrep (falls back to grep)."""

from __future__ import annotations

import asyncio
import shutil
from typing import Any

from crabcode_core.tools._input_helpers import first_non_empty_str
from crabcode_core.types.tool import Tool, ToolContext, ToolResult


class GrepTool(Tool):
    name = "Grep"
    description = "Search file contents using regex patterns."
    is_read_only = True
    is_concurrency_safe = True
    input_schema = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "minLength": 1,
                "description": "The regex pattern to search for.",
            },
            "path": {
                "type": "string",
                "description": (
                    "Directory or file to search in (default: cwd). "
                    "Directories are searched recursively. "
                    "Use the glob parameter to filter file types when searching a directory."
                ),
            },
            "glob": {
                "type": "string",
                "description": "File glob pattern to filter (e.g., '*.py').",
            },
            "case_insensitive": {
                "type": "boolean",
                "description": "Case insensitive search.",
            },
        },
        "required": ["pattern"],
    }

    async def get_prompt(self, **kwargs: Any) -> str:
        return (
            "Search file contents for a regex pattern using ripgrep (falls back to grep if rg is not installed). "
            "Returns matching lines with file paths and line numbers. "
            "Use this instead of running grep or rg via Bash.\n\n"
            "Supports full regex syntax (e.g., 'log.*Error', "
            "'function\\s+\\w+'). Filter files with the glob parameter "
            "(e.g., '*.py', '*.tsx').\n\n"
            "Results are capped at 200 matches for responsiveness. "
            "If you need more specific results, narrow your search "
            "with a more specific pattern or glob filter."
        )

    async def call(
        self,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        pattern = first_non_empty_str(
            tool_input,
            ("pattern", "regex", "regexp", "search", "query"),
        )
        search_path = tool_input.get("path", context.cwd)
        glob_pattern = tool_input.get("glob")
        case_insensitive = tool_input.get("case_insensitive", False)

        if not pattern:
            return ToolResult(
                result_for_model=(
                    "Error: pattern is required. Pass a regex string; aliases: "
                    "regex, regexp, search, query."
                ),
                is_error=True,
            )

        rg = shutil.which("rg")
        use_ripgrep = bool(rg)

        if use_ripgrep:
            args = [rg, "--line-number", "--no-heading", "--color=never"]
            if case_insensitive:
                args.append("-i")
            if glob_pattern:
                args.extend(["--glob", glob_pattern])
            args.extend(["--max-count", "200"])
            args.append(pattern)
            args.append("--")
            args.append(search_path)
        else:
            # Fallback to system grep
            grep_bin = shutil.which("grep") or "grep"
            args = [grep_bin, "-rn", "--color=never", "-E"]
            if case_insensitive:
                args.append("-i")
            if glob_pattern:
                args.extend(["--include", glob_pattern])
            args.extend(["-m", "200"])
            args.append(pattern)
            args.append(search_path)

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=context.cwd,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=30
            )
        except asyncio.TimeoutError:
            return ToolResult(
                result_for_model="Search timed out after 30s",
                is_error=True,
            )
        except FileNotFoundError:
            return ToolResult(
                result_for_model="Error: neither ripgrep (rg) nor grep found on PATH.",
                is_error=True,
            )

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")

        if proc.returncode == 1:
            return ToolResult(
                result_for_model="No matches found.",
                data={"matches": 0},
            )

        if proc.returncode and proc.returncode > 1:
            return ToolResult(
                result_for_model=f"Grep error: {stderr or 'unknown error'}",
                is_error=True,
            )

        lines = stdout.strip().split("\n") if stdout.strip() else []

        max_output = 50_000
        if len(stdout) > max_output:
            stdout = stdout[:max_output] + "\n... (truncated)"

        return ToolResult(
            data={"match_count": len(lines)},
            result_for_model=stdout or "No matches found.",
        )
