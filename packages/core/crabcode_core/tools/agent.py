"""Agent tools — managed sub-agents for parallel/isolated work."""

from __future__ import annotations

import asyncio
from typing import Any

from crabcode_core.agent_manager import AgentManager
from crabcode_core.tools._input_helpers import (
    first_non_empty_str,
    latest_user_text_for_agent_fallback,
)
from crabcode_core.types.config import AgentSettings
from crabcode_core.types.tool import Tool, ToolContext, ToolResult


class AgentTool(Tool):
    name = "Agent"
    description = (
        "Spawn a sub-agent for parallel or isolated tasks. Agents run in the "
        "background by default and report back through a completion notification."
    )
    is_read_only = False
    is_concurrency_safe = True
    input_schema = {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "minLength": 1,
                "description": "The task for the sub-agent to perform.",
            },
            "subagent_type": {
                "type": "string",
                "description": "Type of agent to spawn (e.g., 'explore', 'generalPurpose').",
                "enum": ["explore", "generalPurpose"],
            },
            "description": {
                "type": "string",
                "description": "A short description used to identify the task.",
            },
            "name": {
                "type": "string",
                "description": "Optional stable name for the sub-agent.",
            },
            "model_profile": {
                "type": "string",
                "description": (
                    "Optional named model profile configured under settings.models. "
                    "Do not pass a provider model ID."
                ),
            },
            "run_in_background": {
                "type": "boolean",
                "description": (
                    "Run asynchronously and report back through a task notification. "
                    "Defaults to true; set false only when the result is required "
                    "before this turn can continue."
                ),
                "default": True,
            },
        },
        "required": ["prompt"],
    }

    def __init__(
        self,
        manager: AgentManager | None = None,
        settings: AgentSettings | None = None,
        max_turns: int = 10,
        timeout: int = 300,
        max_output_chars: int = 12_000,
        max_display_lines: int = 120,
    ):
        self._manager = manager
        self._settings = settings or AgentSettings()
        self._max_turns = max_turns
        self._timeout = timeout
        self._max_output_chars = max_output_chars
        self._max_display_lines = max_display_lines

    async def get_prompt(self, **kwargs: Any) -> str:
        return (
            "Launch a sub-agent to handle complex, multi-step tasks "
            "autonomously. Subagents are valuable for parallelizing "
            "independent queries or for protecting the main context "
            "window from excessive results.\n\n"
            "Available types:\n"
            "- 'explore': Fast agent for codebase exploration — finding "
            "files, searching code, answering questions about the codebase.\n"
            "- 'generalPurpose': For complex multi-step tasks that need "
            "searching, reading, and executing commands.\n\n"
            "When to use:\n"
            "- Broadly exploring the codebase for context on a large task\n"
            "- Parallelizing independent research queries\n"
            "- Tasks that may produce large outputs\n\n"
            "Execution mode:\n"
            "- Agents run in the background by default. The tool returns an agent ID "
            "immediately, and the result arrives later as a task notification.\n"
            "- Set run_in_background=false only when this turn cannot continue without "
            "the result. Do not poll a background agent; its completion automatically "
            "resumes the parent conversation.\n\n"
            "When NOT to use:\n"
            "- Simple, single-step tasks — just call the tools directly\n"
            "- Searching for a specific file or class — use Glob or Grep\n\n"
            "IMPORTANT: Avoid duplicating work that subagents are already "
            "doing. If you delegate research to a subagent, do not also "
            "perform the same searches yourself."
        )

    async def call(
        self,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        prompt = first_non_empty_str(
            tool_input,
            ("prompt", "task", "instruction", "query", "message", "description"),
        )
        if not prompt:
            prompt = latest_user_text_for_agent_fallback(context.messages)

        if not prompt:
            return ToolResult(
                result_for_model=(
                    "Error: prompt is required. Pass a non-empty \"prompt\" string "
                    "describing the sub-agent's task (or use field aliases: task, "
                    "instruction, query, message, description)."
                ),
                is_error=True,
            )

        manager = context.agent_manager or self._manager
        if not manager:
            return ToolResult(
                result_for_model="Error: AgentTool requires an agent manager",
                is_error=True,
            )

        agent_id: str | None = None
        # Claude Code treats an omitted/null value as the background default;
        # only an explicit false requests synchronous execution.
        run_in_background = tool_input.get("run_in_background") is not False
        title = first_non_empty_str(tool_input, ("description", "name"))
        try:
            agent_id = await manager.spawn_agent(
                prompt=prompt,
                subagent_type=tool_input.get("subagent_type", "generalPurpose"),
                name=title,
                model_profile=tool_input.get("model_profile"),
                parent_agent_id=context.agent_id,
                parent_tool_use_id=context.tool_use_id,
                depth=context.agent_depth + 1,
                callback=run_in_background,
            )
            if run_in_background:
                snapshot = manager.get_agent(agent_id)
                description = snapshot.title if snapshot is not None else (title or prompt[:80])
                output_file = snapshot.transcript_path if snapshot is not None else None
                resolved_model = snapshot.model if snapshot is not None else None
                data = {
                    "status": "async_launched",
                    "isAsync": True,
                    "agentId": agent_id,
                    "description": description,
                    "resolvedModel": resolved_model,
                    "prompt": prompt,
                    "outputFile": output_file,
                    "canReadOutputFile": bool(output_file),
                }
                lines = [
                    "status: async_launched",
                    f"agentId: {agent_id}",
                    f"description: {description}",
                ]
                if resolved_model:
                    lines.append(f"resolvedModel: {resolved_model}")
                if output_file:
                    lines.append(f"outputFile: {output_file}")
                lines.append(
                    "The result will arrive automatically as a task notification; "
                    "do not poll for completion."
                )
                return ToolResult(data=data, result_for_model="\n".join(lines))
            snapshot = await manager.wait_agent(agent_id, timeout_ms=self._timeout * 1000)
        except asyncio.TimeoutError:
            return ToolResult(
                result_for_model=f"status: timed_out\nresult:\nSub-agent timed out after {self._timeout}s",
                is_error=True,
            )
        except asyncio.CancelledError:
            if agent_id:
                await manager.cancel_agent(agent_id)
            raise
        except ValueError as exc:
            return ToolResult(result_for_model=f"Error: {exc}", is_error=True)

        if snapshot is None:
            return ToolResult(
                result_for_model="status: timeout\nresult:\nSub-agent wait timed out.",
                is_error=True,
            )

        result = AgentManager.format_snapshot(
            snapshot,
            max_result_chars=self._max_output_chars,
        )
        display = result
        lines = display.split("\n")
        if len(lines) > self._max_display_lines:
            kept = "\n".join(lines[:self._max_display_lines])
            display = kept + f"\n… ({len(lines) - self._max_display_lines} more lines truncated)"

        return ToolResult(
            data={"agent": snapshot.to_dict()},
            result_for_model=result,
            result_for_display=display,
            is_error=snapshot.status != "completed",
        )


class AgentStatusTool(Tool):
    name = "AgentStatus"
    description = "Inspect one or more managed sub-agents."
    is_read_only = True
    is_concurrency_safe = True
    input_schema = {
        "type": "object",
        "properties": {
            "agent_id": {
                "type": "string",
                "description": "Optional agent ID. Omit to list all agents.",
            }
        },
    }

    async def call(self, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        manager = context.agent_manager
        if not manager:
            return ToolResult(result_for_model="Error: agent manager unavailable", is_error=True)
        agent_id = tool_input.get("agent_id")
        if agent_id:
            snapshot = manager.get_agent(agent_id)
            if not snapshot:
                return ToolResult(result_for_model=f"Error: unknown agent {agent_id}", is_error=True)
            text = AgentManager.format_snapshot(
                snapshot,
                max_result_chars=manager.max_output_chars,
            )
            return ToolResult(data={"agents": [snapshot.to_dict()]}, result_for_model=text)
        snapshots = manager.list_agents()
        if not snapshots:
            return ToolResult(data={"agents": []}, result_for_model="No managed agents.")
        result_budget = max(1, manager.max_output_chars // len(snapshots))
        body = "\n\n".join(
            AgentManager.format_snapshot(
                snapshot,
                max_result_chars=result_budget,
            )
            for snapshot in snapshots
        )
        return ToolResult(
            data={"agents": [snapshot.to_dict() for snapshot in snapshots]},
            result_for_model=body,
        )


class AgentWaitTool(Tool):
    name = "AgentWait"
    description = "Wait for a managed sub-agent to finish."
    is_read_only = True
    is_concurrency_safe = True
    input_schema = {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "Agent ID to wait for."},
            "agent_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional list of agent IDs to wait on.",
            },
            "wait_any": {
                "type": "boolean",
                "description": "If true, return when any listed agent completes.",
                "default": True,
            },
            "timeout_seconds": {
                "type": "integer",
                "description": "Optional timeout in seconds.",
            },
        },
    }

    async def call(self, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        manager = context.agent_manager
        if not manager:
            return ToolResult(result_for_model="Error: agent manager unavailable", is_error=True)
        timeout = tool_input.get("timeout_seconds")
        timeout_ms = None if timeout is None else int(timeout) * 1000
        agent_ids = [str(v) for v in tool_input.get("agent_ids", []) if str(v)]
        agent_id = tool_input.get("agent_id")
        if agent_id:
            agent_ids.append(str(agent_id))
        if not agent_ids:
            return ToolResult(result_for_model="Error: agent_id or agent_ids is required.", is_error=True)
        if len(agent_ids) == 1 or not tool_input.get("wait_any", True):
            snapshot = await manager.wait_agent(agent_ids[0], timeout_ms=timeout_ms)
        else:
            snapshot = await manager.wait_any(agent_ids, timeout_ms=timeout_ms)
        if snapshot is None:
            return ToolResult(
                data={"agent_ids": agent_ids, "status": "timeout"},
                result_for_model="status: timeout\nresult:\nAgent is still running.",
                is_error=True,
            )
        text = AgentManager.format_snapshot(
            snapshot,
            max_result_chars=manager.max_output_chars,
        )
        return ToolResult(
            data={"agent": snapshot.to_dict()},
            result_for_model=text,
            is_error=snapshot.status
            not in {"completed", "stopped", "cancelled", "failed"},
        )


class AgentCancelTool(Tool):
    name = "AgentCancel"
    description = "Cancel a running managed sub-agent."
    is_read_only = False
    is_concurrency_safe = True
    input_schema = {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "Agent ID to cancel."}
        },
        "required": ["agent_id"],
    }

    async def call(self, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        manager = context.agent_manager
        if not manager:
            return ToolResult(result_for_model="Error: agent manager unavailable", is_error=True)
        cancelled = await manager.cancel_agent(tool_input["agent_id"])
        if not cancelled:
            return ToolResult(
                data={"agent_id": tool_input["agent_id"], "cancelled": False},
                result_for_model="Error: agent is not running or does not exist.",
                is_error=True,
            )
        snapshot = manager.get_agent(tool_input["agent_id"])
        text = (
            AgentManager.format_snapshot(
                snapshot,
                max_result_chars=manager.max_output_chars,
            )
            if snapshot
            else f"Cancelled agent: {tool_input['agent_id']}"
        )
        return ToolResult(
            data={"agent": snapshot.to_dict() if snapshot else None, "cancelled": True},
            result_for_model=text,
        )


class AgentSendInputTool(Tool):
    name = "AgentSendInput"
    description = "Send another prompt to an existing managed sub-agent."
    is_read_only = False
    is_concurrency_safe = True
    input_schema = {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "Agent ID to continue."},
            "prompt": {"type": "string", "description": "Additional input for the agent."},
            "interrupt": {
                "type": "boolean",
                "description": "Cancel a currently running agent before sending input.",
                "default": False,
            },
        },
        "required": ["agent_id", "prompt"],
    }

    async def call(self, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        manager = context.agent_manager
        if not manager:
            return ToolResult(result_for_model="Error: agent manager unavailable", is_error=True)
        sent = await manager.send_input(
            tool_input["agent_id"],
            tool_input["prompt"],
            interrupt=bool(tool_input.get("interrupt", False)),
        )
        if not sent:
            return ToolResult(
                data={"agent_id": tool_input["agent_id"], "sent": False},
                result_for_model="Error: failed to send input to agent.",
                is_error=True,
            )
        snapshot = manager.get_agent(tool_input["agent_id"])
        text = (
            AgentManager.format_snapshot(
                snapshot,
                max_result_chars=manager.max_output_chars,
            )
            if snapshot
            else f"Sent input to agent: {tool_input['agent_id']}"
        )
        return ToolResult(
            data={"agent": snapshot.to_dict() if snapshot else None, "sent": True},
            result_for_model=text,
        )
