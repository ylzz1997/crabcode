"""Anthropic API adapter — primary backend, closest to original behavior."""

from __future__ import annotations

import json
import os
from typing import Any, AsyncGenerator

import anthropic

from crabcode_core.api.base import APIAdapter, ModelConfig, StreamChunk
from crabcode_core.types.config import ApiConfig
from crabcode_core.types.message import (
    ContentBlock,
    Message,
    MessageRole,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)


def _messages_to_api(messages: list[Message]) -> list[dict[str, Any]]:
    """Convert internal messages to Anthropic API format."""
    result: list[dict[str, Any]] = []
    for msg in messages:
        if msg.role == MessageRole.SYSTEM:
            continue

        if isinstance(msg.content, str):
            result.append({"role": msg.role.value, "content": msg.content})
            continue

        blocks: list[dict[str, Any]] = []
        for block in msg.content:
            if isinstance(block, TextBlock):
                blocks.append({"type": "text", "text": block.text})
            elif isinstance(block, ToolUseBlock):
                blocks.append({
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                })
            elif isinstance(block, ToolResultBlock):
                blocks.append({
                    "type": "tool_result",
                    "tool_use_id": block.tool_use_id,
                    "content": block.content,
                    **({"is_error": True} if block.is_error else {}),
                })
            elif isinstance(block, ThinkingBlock):
                blocks.append({
                    "type": "thinking",
                    "thinking": block.thinking,
                })

        if blocks:
            result.append({"role": msg.role.value, "content": blocks})

    return result


def _tools_to_api(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert tool schemas to Anthropic API format."""
    result = []
    for tool in tools:
        api_tool: dict[str, Any] = {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "input_schema": tool.get("input_schema", {"type": "object", "properties": {}}),
        }
        result.append(api_tool)
    return result


class AnthropicAdapter(APIAdapter):
    """Adapter for Anthropic's Messages API (direct, first-party)."""

    def __init__(self, config: ApiConfig):
        self.config = config
        api_key = None
        if config.api_key_env:
            api_key = os.environ.get(config.api_key_env)
        if not api_key:
            api_key = os.environ.get("ANTHROPIC_API_KEY")

        kwargs: dict[str, Any] = {}
        if api_key:
            kwargs["api_key"] = api_key
        if config.base_url:
            kwargs["base_url"] = config.base_url

        self.client = anthropic.AsyncAnthropic(**kwargs)

    async def stream_message(
        self,
        messages: list[Message],
        system: list[str],
        tools: list[dict[str, Any]],
        config: ModelConfig,
    ) -> AsyncGenerator[StreamChunk, None]:
        model = config.model or self.config.model
        if not model:
            raise ValueError(
                "No model configured. Set api.model in ~/.crabcode/settings.json or use the -m flag."
            )

        system_blocks = [{"type": "text", "text": s} for s in system if s]

        params: dict[str, Any] = {
            "model": model,
            "max_tokens": config.max_tokens,
            "system": system_blocks,
            "messages": _messages_to_api(messages)
        }

        api_tools = _tools_to_api(tools)
        if api_tools:
            params["tools"] = api_tools

        if config.thinking_enabled:
            params["thinking"] = {
                "type": "enabled",
                "budget_tokens": config.thinking_budget,
            }

        if config.temperature is not None:
            params["temperature"] = config.temperature

        current_tool_id = ""
        current_tool_name = ""
        tool_input_buffer = ""

        async with self.client.messages.stream(**params) as stream:
            async for event in stream:
                event_type = getattr(event, "type", "")

                if event_type == "content_block_start":
                    block = event.content_block
                    if hasattr(block, "type"):
                        if block.type == "tool_use":
                            current_tool_id = block.id
                            current_tool_name = block.name
                            tool_input_buffer = ""
                            yield StreamChunk(
                                type="tool_use_start",
                                tool_use_id=block.id,
                                tool_name=block.name,
                            )
                        elif block.type == "thinking":
                            pass
                        elif block.type == "text":
                            pass

                elif event_type == "content_block_delta":
                    delta = event.delta
                    if hasattr(delta, "type"):
                        if delta.type == "text_delta":
                            yield StreamChunk(type="text", text=delta.text)
                        elif delta.type == "thinking_delta":
                            yield StreamChunk(type="thinking", text=delta.thinking)
                        elif delta.type == "input_json_delta":
                            tool_input_buffer += delta.partial_json
                            yield StreamChunk(
                                type="tool_use_delta",
                                tool_use_id=current_tool_id,
                                tool_input_json=delta.partial_json,
                            )

                elif event_type == "content_block_stop":
                    if current_tool_id:
                        yield StreamChunk(
                            type="tool_use_end",
                            tool_use_id=current_tool_id,
                            tool_name=current_tool_name,
                            tool_input_json=tool_input_buffer,
                        )
                        current_tool_id = ""
                        current_tool_name = ""
                        tool_input_buffer = ""

                elif event_type == "message_delta":
                    usage = {}
                    if hasattr(event, "usage") and event.usage:
                        usage = {
                            "output_tokens": getattr(event.usage, "output_tokens", 0),
                        }
                    stop_reason = getattr(event.delta, "stop_reason", "") or ""
                    yield StreamChunk(
                        type="message_delta",
                        stop_reason=stop_reason,
                        usage=usage,
                    )

                elif event_type == "message_start":
                    usage = {}
                    if hasattr(event.message, "usage") and event.message.usage:
                        u = event.message.usage
                        usage = {
                            "input_tokens": getattr(u, "input_tokens", 0),
                            "output_tokens": getattr(u, "output_tokens", 0),
                        }
                    yield StreamChunk(type="message_start", usage=usage)

                elif event_type == "message_stop":
                    yield StreamChunk(type="message_stop")

    async def count_tokens(
        self,
        messages: list[Message],
        system: list[str],
    ) -> int:
        try:
            result = await self.client.messages.count_tokens(
                model=self.config.model or "",
                messages=_messages_to_api(messages),
                system=[{"type": "text", "text": s} for s in system if s],
            )
            return result.input_tokens
        except Exception:
            total = sum(len(s) for s in system)
            for msg in messages:
                if isinstance(msg.content, str):
                    total += len(msg.content)
                else:
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            total += len(block.text)
            return total // 4


class BedrockAdapter(AnthropicAdapter):
    """Adapter for Anthropic via AWS Bedrock."""

    def __init__(self, config: ApiConfig):
        self.config = config
        self.client = anthropic.AsyncAnthropicBedrock()


class VertexAdapter(AnthropicAdapter):
    """Adapter for Anthropic via Google Vertex AI."""

    def __init__(self, config: ApiConfig):
        self.config = config
        self.client = anthropic.AsyncAnthropicVertex(
            region=os.environ.get("CLOUD_ML_REGION", "us-east5"),
            project_id=os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID"),
        )
