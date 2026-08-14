"""Tools for discovering and messaging independent CrabCode sessions."""

from __future__ import annotations

from typing import Any

from crabcode_core.types.tool import Tool, ToolContext, ToolResult


class ListAgentsTool(Tool):
    name = "ListAgents"
    description = (
        "List other live CrabCode sessions on this machine that can receive "
        "cross-session messages."
    )
    is_read_only = True
    is_concurrency_safe = True
    input_schema = {"type": "object", "properties": {}}

    async def call(self, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        session = context.session
        if session is None:
            return ToolResult(
                result_for_model="Error: cross-session discovery is unavailable in this agent context",
                is_error=True,
            )
        try:
            runtime = await session.ensure_peer_runtime()
        except Exception as exc:
            return ToolResult(
                result_for_model=f"Error: cross-session discovery is unavailable: {exc}",
                is_error=True,
            )
        if runtime is None:
            return ToolResult(
                data={"agents": []},
                result_for_model="Cross-session messaging is disabled for this session.",
            )
        peers = runtime.list_peers()
        data = [peer.model_dump(exclude={"auth_token"}) for peer in peers]
        if not peers:
            return ToolResult(data={"agents": []}, result_for_model="No other live sessions found.")
        lines = [
            f"{peer.name} · {peer.session_id[:8]} · {peer.cwd} · {peer.permission_class}"
            for peer in peers
        ]
        return ToolResult(data={"agents": data}, result_for_model="\n".join(lines))


class SendMessageTool(Tool):
    name = "SendMessage"
    description = (
        "Send a plain-text message to another live CrabCode session by its "
        "name, full session ID, or unique session ID prefix. The receiver is "
        "told that the message came from another AI session, not from the user."
    )
    is_read_only = False
    is_concurrency_safe = True
    input_schema = {
        "type": "object",
        "properties": {
            "to": {
                "type": "string",
                "minLength": 1,
                "description": "Target session name or session ID.",
            },
            "text": {
                "type": "string",
                "minLength": 1,
                "description": "Concise plain-text message for the other session.",
            },
        },
        "required": ["to", "text"],
    }

    async def call(self, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        session = context.session
        if session is None:
            return ToolResult(
                result_for_model="Error: cross-session messaging is unavailable in this agent context",
                is_error=True,
            )
        try:
            runtime = await session.ensure_peer_runtime()
        except Exception as exc:
            return ToolResult(
                result_for_model=f"Error: cross-session messaging is unavailable: {exc}",
                is_error=True,
            )
        if runtime is None:
            return ToolResult(
                result_for_model="Error: cross-session messaging is disabled",
                is_error=True,
            )
        delivery = await runtime.send(str(tool_input["to"]), str(tool_input["text"]))
        return ToolResult(
            data=delivery.model_dump(),
            result_for_model=(
                f"Message {delivery.status}: {delivery.message_id or '(not created)'}"
                + (f"\n{delivery.detail}" if delivery.detail else "")
            ),
            is_error=delivery.status in {"failed", "refused"},
        )
