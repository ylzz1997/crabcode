"""OpenAI Responses (Codex) API adapter — uses the newer Responses API endpoint.

Supports OpenAI's Responses API which is used by Codex and o-series models.
Falls back to Chat Completions API for models that don't support the Responses API.
"""

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


def _messages_to_responses_input(
    messages: list[Message],
) -> list[dict[str, Any]]:
    """Convert internal messages to OpenAI Responses API input format.

    The Responses API uses a flat list of input items rather than
    the Chat Completions 'messages' array. Each item has a 'type' field.
    """
    result: list[dict[str, Any]] = []

    for msg in messages:
        if msg.role == MessageRole.SYSTEM:
            continue

        if isinstance(msg.content, str):
            result.append({
                "type": "message",
                "role": msg.role.value,
                "content": msg.content,
            })
            continue

        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        tool_results: list[dict[str, Any]] = []

        for block in msg.content:
            if isinstance(block, TextBlock):
                text_parts.append(block.text)
            elif isinstance(block, ToolUseBlock):
                # Responses API uses 'function_call' type
                tool_calls.append({
                    "type": "function_call",
                    "call_id": block.id,
                    "name": block.name,
                    "arguments": json.dumps(block.input),
                })
            elif isinstance(block, ToolResultBlock):
                # Responses API uses 'function_call_output' type
                tool_results.append({
                    "type": "function_call_output",
                    "call_id": block.tool_use_id,
                    "output": block.content,
                })
            elif isinstance(block, ThinkingBlock):
                pass

        if msg.role == MessageRole.ASSISTANT:
            # Add assistant message with text content
            if text_parts:
                result.append({
                    "type": "message",
                    "role": "assistant",
                    "content": "".join(text_parts),
                })
            # Add function_call items (they are separate top-level items)
            for tc in tool_calls:
                result.append(tc)
        elif msg.role == MessageRole.USER:
            # Add function_call_output items (they are separate top-level items)
            for tr in tool_results:
                result.append(tr)
            # Add user message if there's text content
            if text_parts and not tool_results:
                result.append({
                    "type": "message",
                    "role": "user",
                    "content": "".join(text_parts),
                })

    return safe_utf8_json_tree(result)


def _tools_to_responses(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert tool schemas to OpenAI Responses API function tool format."""
    result = []
    for tool in tools:
        schema = tool.get("input_schema", {"type": "object", "properties": {}})
        result.append({
            "type": "function",
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": schema,
            "strict": False,
        })
    return safe_utf8_json_tree(result)


def _response_to_stream_chunks(response: Any) -> list[StreamChunk]:
    """Convert a non-stream Responses API object into stream chunks.

    Some OpenAI-compatible proxies support the Responses endpoint but return
    SSE frames that the official SDK cannot parse. In that case we retry
    without streaming and translate the final response back into CrabCode's
    streaming abstraction.
    """
    chunks: list[StreamChunk] = []

    output_items = getattr(response, "output", None) or []
    for item in output_items:
        item_type = getattr(item, "type", "")

        if item_type == "message":
            for part in getattr(item, "content", None) or []:
                part_type = getattr(part, "type", "")
                if part_type == "output_text" and getattr(part, "text", ""):
                    chunks.append(
                        StreamChunk(type="text", text=safe_utf8_str(part.text))
                    )

        elif item_type == "function_call":
            call_id = getattr(item, "call_id", "") or getattr(item, "id", "")
            name = getattr(item, "name", "")
            arguments = getattr(item, "arguments", "") or ""
            chunks.append(
                StreamChunk(
                    type="tool_use_start",
                    tool_use_id=call_id,
                    tool_name=name,
                )
            )
            chunks.append(
                StreamChunk(
                    type="tool_use_end",
                    tool_use_id=call_id,
                    tool_name=name,
                    tool_input_json=arguments,
                )
            )

    usage = {}
    if hasattr(response, "usage") and response.usage:
        u = response.usage
        usage = {
            "input_tokens": getattr(u, "input_tokens", 0),
            "output_tokens": getattr(u, "output_tokens", 0),
        }

    if not chunks and getattr(response, "error", None):
        err = response.error
        error_msg = safe_utf8_str(getattr(err, "message", str(err)))
        chunks.append(StreamChunk(type="error", error=error_msg or "Response failed"))
        return chunks

    chunks.append(
        StreamChunk(
            type="message_stop",
            stop_reason="end_turn",
            usage=usage,
        )
    )
    return chunks


class CodexAdapter(APIAdapter):
    """Adapter for OpenAI's Responses API (Codex / o-series models).

    Uses client.responses.create() with stream=True.
    """

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
        model = config.model or self.config.model or "codex-mini-latest"

        # Responses API uses 'instructions' for system prompt
        instructions_raw = "\n\n".join(s for s in system if s) or None
        instructions = (
            safe_utf8_str(instructions_raw) if instructions_raw else None
        )

        params: dict[str, Any] = {
            "model": model,
            "input": _messages_to_responses_input(messages),
            "stream": True,
        }

        if instructions:
            params["instructions"] = instructions

        if config.max_tokens:
            params["max_output_tokens"] = config.max_tokens

        if config.temperature is not None:
            params["temperature"] = config.temperature

        api_tools = _tools_to_responses(tools)
        if api_tools:
            params["tools"] = api_tools

        # For o-series models, configure reasoning
        if config.thinking_enabled:
            # Map thinking_budget to reasoning effort
            budget = config.thinking_budget
            if budget >= 20000:
                effort = "high"
            elif budget >= 8000:
                effort = "medium"
            else:
                effort = "low"
            params["reasoning"] = {"effort": effort, "summary": "auto"}

        # Track active function calls by item_id
        active_calls: dict[str, dict[str, str]] = {}
        emitted_stream_event = False
        try:
            stream = await self.client.responses.create(**params)
            async for event in stream:
                emitted_stream_event = True
                event_type = getattr(event, "type", "")

                # Text delta
                if event_type == "response.output_text.delta":
                    yield StreamChunk(type="text", text=safe_utf8_str(event.delta))

                # Function call arguments delta
                elif event_type == "response.function_call_arguments.delta":
                    item_id = event.item_id
                    if item_id not in active_calls:
                        active_calls[item_id] = {
                            "call_id": "",
                            "name": "",
                            "arguments": "",
                        }
                    buf = active_calls[item_id]
                    buf["arguments"] += event.delta
                    call_id = buf.get("call_id", "") or item_id
                    yield StreamChunk(
                        type="tool_use_delta",
                        tool_use_id=call_id,
                        tool_input_json=event.delta,
                    )

                # Output item added — detect function_call start
                elif event_type == "response.output_item.added":
                    item = event.item
                    item_type = getattr(item, "type", "")
                    if item_type == "function_call":
                        item_id = getattr(item, "id", "")
                        call_id = getattr(item, "call_id", "") or item_id
                        name = getattr(item, "name", "")
                        if item_id:
                            active_calls[item_id] = {
                                "call_id": call_id,
                                "name": name,
                                "arguments": "",
                            }
                        yield StreamChunk(
                            type="tool_use_start",
                            tool_use_id=call_id,
                            tool_name=name,
                        )

                # Function call arguments done
                elif event_type == "response.function_call_arguments.done":
                    item_id = event.item_id
                    buf = active_calls.get(item_id, {})
                    call_id = buf.get("call_id", item_id)
                    name = buf.get("name", "")
                    arguments = event.arguments or buf.get("arguments", "")
                    yield StreamChunk(
                        type="tool_use_end",
                        tool_use_id=call_id,
                        tool_name=name,
                        tool_input_json=arguments,
                    )
                    active_calls.pop(item_id, None)

                # Output item done — also finalize function calls if not already done
                elif event_type == "response.output_item.done":
                    item = event.item
                    item_type = getattr(item, "type", "")
                    if item_type == "function_call":
                        item_id = getattr(item, "id", "")
                        # Only yield if not already yielded via arguments.done
                        if item_id and item_id in active_calls:
                            buf = active_calls.pop(item_id)
                            call_id = buf.get("call_id", item_id)
                            yield StreamChunk(
                                type="tool_use_end",
                                tool_use_id=call_id,
                                tool_name=buf.get("name", ""),
                                tool_input_json=buf.get("arguments", ""),
                            )

                # Reasoning summary text delta — treat as thinking
                elif event_type == "response.reasoning_summary_text.delta":
                    yield StreamChunk(type="thinking", text=safe_utf8_str(event.delta))

                # Response completed
                elif event_type == "response.completed":
                    usage = {}
                    response = event.response
                    if hasattr(response, "usage") and response.usage:
                        u = response.usage
                        usage = {
                            "input_tokens": getattr(u, "input_tokens", 0),
                            "output_tokens": getattr(u, "output_tokens", 0),
                        }
                    yield StreamChunk(
                        type="message_stop",
                        stop_reason="end_turn",
                        usage=usage,
                    )

                # Response failed or incomplete
                elif event_type == "response.failed":
                    error_msg = ""
                    if hasattr(event, "response") and hasattr(event.response, "error"):
                        err = event.response.error
                        if err:
                            error_msg = getattr(err, "message", str(err))
                    yield StreamChunk(
                        type="error", error=safe_utf8_str(error_msg or "Response failed")
                    )

                elif event_type == "response.incomplete":
                    yield StreamChunk(type="error", error="Response incomplete (max output tokens or content filter)")

                # Error event
                elif event_type == "response.error":
                    error_msg = ""
                    if hasattr(event, "error"):
                        err = event.error
                        error_msg = getattr(err, "message", str(err)) if err else ""
                    yield StreamChunk(
                        type="error", error=safe_utf8_str(error_msg or "Unknown error")
                    )
        except json.JSONDecodeError:
            if emitted_stream_event:
                raise

            fallback_params = dict(params)
            fallback_params.pop("stream", None)
            response = await self.client.responses.create(**fallback_params)
            for chunk in _response_to_stream_chunks(response):
                yield chunk

    async def count_tokens(
        self,
        messages: list[Message],
        system: list[str],
    ) -> int:
        # No token counting API for Responses; use character estimate
        total = sum(len(s) for s in system)
        for msg in messages:
            if isinstance(msg.content, str):
                total += len(msg.content)
            else:
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        total += len(block.text)
        return total // 4
