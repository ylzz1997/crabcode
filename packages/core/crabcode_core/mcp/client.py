"""MCP client — connects to MCP servers and loads tools."""

from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any

from crabcode_core.types.config import McpServerConfig
from crabcode_core.types.tool import Tool, ToolContext, ToolResult


def _normalize_name(name: str) -> str:
    """Normalize a server/tool name to a valid identifier."""
    return re.sub(r'[^a-zA-Z0-9_]', '_', name)


def build_mcp_tool_name(server_name: str, tool_name: str) -> str:
    """Build the prefixed MCP tool name."""
    return f"mcp__{_normalize_name(server_name)}__{_normalize_name(tool_name)}"


class McpToolWrapper(Tool):
    """Wraps an MCP tool as a CrabCode Tool."""

    def __init__(
        self,
        server_name: str,
        tool_name: str,
        description: str,
        schema: dict[str, Any],
        client: Any,
    ):
        self.name = build_mcp_tool_name(server_name, tool_name)
        self.description = description
        self.input_schema = schema
        self.is_read_only = False
        self.is_concurrency_safe = True
        self.is_enabled = True

        self._server_name = server_name
        self._original_name = tool_name
        self._client = client

    async def call(
        self,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        try:
            result = await self._client.call_tool(self._original_name, tool_input)

            text_parts: list[str] = []
            if hasattr(result, "content"):
                for block in result.content:
                    if hasattr(block, "text"):
                        text_parts.append(block.text)

            output = "\n".join(text_parts) if text_parts else str(result)
            is_error = getattr(result, "isError", False)

            return ToolResult(
                data=result,
                result_for_model=output,
                is_error=is_error,
            )
        except Exception as e:
            return ToolResult(
                result_for_model=f"MCP tool error: {e}",
                is_error=True,
            )


class McpManager:
    """Manages MCP server connections and tool discovery."""

    def __init__(self) -> None:
        self._connections: dict[str, Any] = {}
        self._tools: list[McpToolWrapper] = []

    async def connect(
        self,
        servers: dict[str, McpServerConfig],
    ) -> list[McpToolWrapper]:
        """Connect to all configured MCP servers and fetch tools."""
        all_tools: list[McpToolWrapper] = []

        for name, config in servers.items():
            if config.disabled:
                continue

            try:
                tools = await self._connect_server(name, config)
                all_tools.extend(tools)
            except Exception:
                pass

        self._tools = all_tools
        return all_tools

    async def _connect_server(
        self,
        name: str,
        config: McpServerConfig,
    ) -> list[McpToolWrapper]:
        """Connect to a single MCP server."""
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError:
            return []

        if not config.command:
            return []

        env = {**os.environ, **config.env}
        server_params = StdioServerParameters(
            command=config.command[0],
            args=config.command[1:] if len(config.command) > 1 else [],
            env=env,
        )

        try:
            read_stream, write_stream = await asyncio.wait_for(
                stdio_client(server_params).__aenter__(),
                timeout=30,
            )
            session = ClientSession(read_stream, write_stream)
            await session.initialize()
            self._connections[name] = session

            tools_response = await session.list_tools()
            tools: list[McpToolWrapper] = []
            for tool_info in tools_response.tools:
                wrapper = McpToolWrapper(
                    server_name=name,
                    tool_name=tool_info.name,
                    description=tool_info.description or "",
                    schema=tool_info.inputSchema or {"type": "object", "properties": {}},
                    client=session,
                )
                tools.append(wrapper)

            return tools

        except Exception:
            return []

    @property
    def tools(self) -> list[McpToolWrapper]:
        return self._tools

    async def disconnect_all(self) -> None:
        """Disconnect from all MCP servers."""
        for session in self._connections.values():
            try:
                await session.__aexit__(None, None, None)
            except Exception:
                pass
        self._connections.clear()
        self._tools.clear()
