"""OpenAI API adapter — translates between internal Anthropic-style messages and OpenAI format."""

from __future__ import annotations

import json
import os
from typing import Any, AsyncGenerator

from crabcode_core.api.base import APIAdapter, ModelConfig, StreamChunk
from crabcode_core.types.config import ApiConfig
from crabcode_core.utf8_sanitize import safe_utf8_json_tree, safe_utf8_str
from crabcode_core.types.message import (
    Message,
    MessageRole,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    ThinkingBlock,
)


def _messages_to_openai(
    messages: list[Message],
    system: list[str],
) -> list[dict[str, Any]]:
    """Convert internal messages + system to OpenAI chat format."""
    result: list[dict[str, Any]] = []

    system_text = "\n\n".join(s for s in system if s)
    if system_text:
        result.append({"role": "system", "content": system_text})

    for msg in messages:
        if msg.role == MessageRole.SYSTEM:
            continue

        if isinstance(msg.content, str):
            result.append({"role": msg.role.value, "content": msg.content})
            continue

        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        tool_results: list[dict[str, Any]] = []

        for block in msg.content:
            if isinstance(block, TextBlock):
                text_parts.append(block.text)
            elif isinstance(block, ToolUseBlock):
                tool_calls.append({
                    "id": block.id,
                    "type": "function",
                    "function": {
                        "name": block.name,
                        "arguments": json.dumps(block.input),
                    },
                })
            elif isinstance(block, ToolResultBlock):
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": block.tool_use_id,
                    "content": block.content,
                })
            elif isinstance(block, ThinkingBlock):
                pass

        if msg.role == MessageRole.ASSISTANT:
            entry: dict[str, Any] = {"role": "assistant"}
            if text_parts:
                entry["content"] = "".join(text_parts)
            elif tool_calls:
                # OpenAI expects explicit null when the turn is tool-only.
                entry["content"] = None
            if tool_calls:
                entry["tool_calls"] = tool_calls
            result.append(entry)
        elif msg.role == MessageRole.USER:
            if tool_results:
                result.extend(tool_results)
            elif text_parts:
                result.append({"role": "user", "content": "".join(text_parts)})

    return safe_utf8_json_tree(result)


def _tools_to_openai(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert tool schemas to OpenAI function format."""
    result = []
    for tool in tools:
        schema = tool.get("input_schema", {"type": "object", "properties": {}})
        result.append({
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": schema,
            },
        })
    return safe_utf8_json_tree(result)


class OpenAIAdapter(APIAdapter):
    """Adapter for OpenAI-compatible APIs."""

    def __init__(self, config: ApiConfig):
        import openai

        self.config = config
        api_key = None
        if config.api_key_env:
            api_key = os.environ.get(config.api_key_env)
        if not api_key:
            api_key = os.environ.get("OPENAI_API_KEY")

        kwargs: dict[str, Any] = {}
        if api_key:
            kwargs["api_key"] = api_key
        if config.base_url:
            kwargs["base_url"] = config.base_url

        self.client = openai.AsyncOpenAI(**kwargs)

    async def stream_message(
        self,
        messages: list[Message],
        system: list[str],
        tools: list[dict[str, Any]],
        config: ModelConfig,
    ) -> AsyncGenerator[StreamChunk, None]:
        model = config.model or self.config.model or "gpt-4o"

        params: dict[str, Any] = {
            "model": model,
            "max_tokens": config.max_tokens,
            "messages": _messages_to_openai(messages, system),
            "stream": True,
        }

        api_tools = _tools_to_openai(tools)
        if api_tools:
            params["tools"] = api_tools

        if config.temperature is not None:
            params["temperature"] = config.temperature

        tool_call_buffers: dict[int, dict[str, str]] = {}

        stream = await self.client.chat.completions.create(**params)
        async for chunk in stream:
            if not chunk.choices:
                continue

            delta = chunk.choices[0].delta
            finish_reason = chunk.choices[0].finish_reason

            if delta.content:
                yield StreamChunk(type="text", text=safe_utf8_str(delta.content))

            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in tool_call_buffers:
                        tool_call_buffers[idx] = {
                            "id": tc.id or "",
                            "name": "",
                            "arguments": "",
                        }
                    buf = tool_call_buffers[idx]
                    if tc.id:
                        buf["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            buf["name"] = tc.function.name
                            yield StreamChunk(
                                type="tool_use_start",
                                tool_use_id=buf["id"],
                                tool_name=buf["name"],
                            )
                        if tc.function.arguments:
                            buf["arguments"] += tc.function.arguments
                            yield StreamChunk(
                                type="tool_use_delta",
                                tool_use_id=buf["id"],
                                tool_input_json=tc.function.arguments,
                            )

            if finish_reason == "tool_calls":
                for buf in tool_call_buffers.values():
                    yield StreamChunk(
                        type="tool_use_end",
                        tool_use_id=buf["id"],
                        tool_name=buf["name"],
                        tool_input_json=buf["arguments"],
                    )
                tool_call_buffers.clear()

            if finish_reason == "stop":
                usage = {}
                if chunk.usage:
                    usage = {
                        "input_tokens": chunk.usage.prompt_tokens,
                        "output_tokens": chunk.usage.completion_tokens,
                    }
                yield StreamChunk(
                    type="message_stop",
                    stop_reason="end_turn",
                    usage=usage,
                )

    async def count_tokens(
        self,
        messages: list[Message],
        system: list[str],
    ) -> int:
        total = sum(len(s) for s in system)
        for msg in messages:
            if isinstance(msg.content, str):
                total += len(msg.content)
            else:
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        total += len(block.text)
        return total // 4
