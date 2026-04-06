"""BashTool — execute shell commands."""

from __future__ import annotations

import asyncio
import os
from typing import Any

from crabcode_core.types.tool import Tool, ToolContext, ToolResult


class BashTool(Tool):
    name = "Bash"
    description = "Execute a bash command in the shell."
    is_read_only = False
    is_concurrency_safe = False
    input_schema = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The bash command to execute.",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds (default: 120).",
            },
        },
        "required": ["command"],
    }

    async def get_prompt(self, **kwargs: Any) -> str:
        return (
            "Execute a bash command in the shell. Use for system commands, "
            "running scripts, git operations, and other terminal tasks. "
            "Prefer dedicated tools (Read, Edit, Write, Glob, Grep) over "
            "bash when they can accomplish the task.\n\n"
            "Commands run in the user's shell with their environment. "
            "The command is executed in the working directory of the "
            "current session. The shell is stateful across calls — "
            "environment variables and directory changes persist.\n\n"
            "Guidelines:\n"
            "- Always quote file paths that contain spaces with double "
            'quotes: cd "/path/with spaces" (correct) vs cd /path/with '
            "spaces (incorrect, will fail).\n"
            "- For long-running processes (dev servers, watchers), they "
            "will be killed when the timeout expires. Warn the user if "
            "a command is expected to run indefinitely.\n"
            "- When issuing multiple independent commands, call Bash "
            "multiple times in parallel rather than chaining with &&.\n"
            "- If commands depend on each other and must run sequentially, "
            "chain them with && in a single call.\n"
            "- Do NOT use interactive commands (e.g., git rebase -i, "
            "vim, nano) — they require user input that is not supported.\n"
            "- If a command fails, read the error output carefully before "
            "retrying. Do not blindly retry the same command."
        )

    async def call(
        self,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        command = tool_input.get("command", "")
        timeout = tool_input.get("timeout", 120)

        if not command.strip():
            return ToolResult(
                result_for_model="Error: empty command",
                is_error=True,
            )

        env = {**os.environ, **context.env}

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=context.cwd,
                env=env,
            )

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return ToolResult(
                    result_for_model=f"Command timed out after {timeout}s",
                    is_error=True,
                )

            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")
            exit_code = proc.returncode or 0

            max_chars = 100_000
            if len(stdout) > max_chars:
                stdout = stdout[:max_chars] + "\n... (truncated)"
            if len(stderr) > max_chars:
                stderr = stderr[:max_chars] + "\n... (truncated)"

            parts: list[str] = []
            if stdout:
                parts.append(stdout)
            if stderr:
                parts.append(f"stderr:\n{stderr}")
            if exit_code != 0:
                parts.append(f"Exit code: {exit_code}")

            output = "\n".join(parts) if parts else "(no output)"

            return ToolResult(
                data={"stdout": stdout, "stderr": stderr, "exit_code": exit_code},
                result_for_model=output,
                result_for_display=output,
                is_error=exit_code != 0,
            )

        except Exception as e:
            return ToolResult(
                result_for_model=f"Error executing command: {e}",
                is_error=True,
            )
