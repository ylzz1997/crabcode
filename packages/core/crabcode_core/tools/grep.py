"""GrepTool — search file contents with portable fallbacks."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from pathlib import Path
from typing import Any

from crabcode_core.subprocess_utils import (
    subprocess_group_options,
    terminate_process_tree,
)
from crabcode_core.tools._input_helpers import first_non_empty_str
from crabcode_core.types.tool import Tool, ToolContext, ToolResult


_SKIP_DIRECTORIES = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "__pycache__",
    "dist",
    "node_modules",
    "target",
    "venv",
}


def _python_search(
    pattern: str,
    search_path: str,
    *,
    cwd: str,
    glob_pattern: str | None,
    case_insensitive: bool,
) -> str:
    """Pure-Python fallback for stock Windows installations."""
    flags = re.IGNORECASE if case_insensitive else 0
    expression = re.compile(pattern, flags)
    target = Path(search_path)
    if not target.is_absolute():
        target = Path(cwd) / target
    target = target.resolve()
    if not target.exists():
        raise FileNotFoundError(f"search path does not exist: {search_path}")

    if target.is_file():
        candidates = [target]
    else:
        candidates = []
        for root, directories, files in os.walk(target):
            directories[:] = [
                name for name in directories if name not in _SKIP_DIRECTORIES
            ]
            root_path = Path(root)
            for name in files:
                candidate = root_path / name
                if glob_pattern and not candidate.match(glob_pattern):
                    continue
                candidates.append(candidate)

    matches: list[str] = []
    for candidate in candidates:
        try:
            with open(candidate, "rb") as probe:
                if b"\0" in probe.read(8192):
                    continue
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            display = str(candidate.relative_to(Path(cwd).resolve()))
        except ValueError:
            display = str(candidate)
        for line_number, line in enumerate(text.splitlines(), start=1):
            if expression.search(line):
                matches.append(f"{display}:{line_number}:{line}")
                if len(matches) >= 200:
                    return "\n".join(matches)
    return "\n".join(matches)


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
            "Search file contents for a regex pattern using ripgrep, system grep, or a built-in fallback. "
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
        grep_bin = shutil.which("grep")

        if not use_ripgrep and not grep_bin:
            try:
                stdout = await asyncio.wait_for(
                    asyncio.to_thread(
                        _python_search,
                        pattern,
                        str(search_path),
                        cwd=context.cwd,
                        glob_pattern=glob_pattern,
                        case_insensitive=bool(case_insensitive),
                    ),
                    timeout=30,
                )
            except asyncio.TimeoutError:
                return ToolResult(
                    result_for_model="Search timed out after 30s",
                    is_error=True,
                )
            except (FileNotFoundError, re.error) as exc:
                return ToolResult(result_for_model=f"Grep error: {exc}", is_error=True)
            lines = stdout.splitlines() if stdout else []
            if len(stdout) > 50_000:
                stdout = stdout[:50_000] + "\n... (truncated)"
            return ToolResult(
                data={"match_count": len(lines)},
                result_for_model=stdout or "No matches found.",
            )

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
            assert grep_bin is not None
            args = [grep_bin, "-rn", "--color=never", "-E"]
            if case_insensitive:
                args.append("-i")
            if glob_pattern:
                args.extend(["--include", glob_pattern])
            args.extend(["-m", "200"])
            args.append(pattern)
            args.append(search_path)

        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=context.cwd,
                **subprocess_group_options(),
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=30
            )
        except asyncio.TimeoutError:
            if proc is not None and proc.returncode is None:
                await terminate_process_tree(proc)
            return ToolResult(
                result_for_model="Search timed out after 30s",
                is_error=True,
            )
        except asyncio.CancelledError:
            if proc is not None and proc.returncode is None:
                await terminate_process_tree(proc)
            raise
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
