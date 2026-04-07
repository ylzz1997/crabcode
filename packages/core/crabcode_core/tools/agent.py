"""AgentTool — spawn sub-agents for parallel/isolated work."""

from __future__ import annotations

import asyncio
from typing import Any

from crabcode_core.types.tool import Tool, ToolContext, ToolResult


class AgentTool(Tool):
    name = "Agent"
    description = "Spawn a sub-agent for parallel or isolated tasks."
    is_read_only = False
    is_concurrency_safe = True
    input_schema = {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "The task for the sub-agent to perform.",
            },
            "subagent_type": {
                "type": "string",
                "description": "Type of agent to spawn (e.g., 'explore', 'generalPurpose').",
                "enum": ["explore", "generalPurpose"],
            },
        },
        "required": ["prompt"],
    }

    def __init__(
        self,
        api_adapter: Any = None,
        tools: list[Tool] | None = None,
        prompt_profile: Any = None,
    ):
        self._api_adapter = api_adapter
        self._sub_tools = tools
        self._prompt_profile = prompt_profile

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
        prompt = tool_input.get("prompt", "")

        if not prompt:
            return ToolResult(
                result_for_model="Error: prompt is required",
                is_error=True,
            )

        if not self._api_adapter:
            return ToolResult(
                result_for_model="Error: AgentTool requires an API adapter",
                is_error=True,
            )

        from crabcode_core.prompts.profile import resolve_agent_prompt
        from crabcode_core.query.loop import QueryParams, query_loop
        from crabcode_core.types.message import create_user_message

        sub_messages = [create_user_message(content=prompt)]

        sub_context = ToolContext(
            cwd=context.cwd,
            messages=[],
            session_id=context.session_id,
            env=context.env,
        )

        tools = self._sub_tools or []
        agent_prompt = resolve_agent_prompt(self._prompt_profile)

        params = QueryParams(
            messages=sub_messages,
            system_prompt=[agent_prompt],
            user_context={},
            system_context={},
            tools=tools,
            tool_context=sub_context,
            api_adapter=self._api_adapter,
            max_turns=10,
        )

        result_parts: list[str] = []

        # Sub-agent timeout: 5 minutes total
        SUB_AGENT_TIMEOUT = 300

        async def _collect_results():
            async for event in query_loop(params):
                from crabcode_core.types.event import StreamTextEvent, ErrorEvent
                if isinstance(event, StreamTextEvent):
                    result_parts.append(event.text)
                elif isinstance(event, ErrorEvent):
                    result_parts.append(f"\n[Error: {event.message}]")

        try:
            await asyncio.wait_for(_collect_results(), timeout=SUB_AGENT_TIMEOUT)
        except asyncio.TimeoutError:
            result_parts.append(f"\n[Sub-agent timed out after {SUB_AGENT_TIMEOUT}s]")

        result = "".join(result_parts)
        if not result:
            result = "Sub-agent completed but produced no text output."

        return ToolResult(
            data={"agent_output": result},
            result_for_model=result,
        )
