"""Anthropic API adapter — primary backend, closest to original behavior."""

from __future__ import annotations

import json
import os
from typing import Any, AsyncGenerator

import anthropic
import httpx

from crabcode_core.api.base import (
    APIAdapter,
    ModelConfig,
    StreamChunk,
    usage_int_field,
)
from crabcode_core.logging_utils import get_logger
from crabcode_core.types.config import ApiConfig
from crabcode_core.utf8_sanitize import safe_utf8_json_tree, safe_utf8_str
from crabcode_core.types.message import (
    ContentBlock,
    ImageBlock,
    Message,
    MessageRole,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)

logger = get_logger(__name__)


ANTHROPIC_VERSION = "2023-06-01"


def _anthropic_usage(
    raw: Any,
    *,
    include_input: bool,
    include_output: bool,
) -> dict[str, int]:
    usage: dict[str, int] = {}
    if include_input:
        input_tokens, has_input = usage_int_field(raw, "input_tokens")
        cache_read, has_cache_read = usage_int_field(raw, "cache_read_input_tokens")
        cache_write, has_cache_write = usage_int_field(raw, "cache_creation_input_tokens")
        if has_input:
            usage["input_tokens"] = input_tokens
            usage["total_input_tokens"] = input_tokens + cache_read + cache_write
        if has_cache_read:
            usage["cache_read_tokens"] = cache_read
        if has_cache_write:
            usage["cache_write_tokens"] = cache_write
    if include_output:
        output_tokens, has_output = usage_int_field(raw, "output_tokens")
        if has_output:
            usage["output_tokens"] = output_tokens
    return usage


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
            elif isinstance(block, ImageBlock):
                blocks.append({
                    "type": "image",
                    "source": block.source,
                })

        if blocks:
            result.append({"role": msg.role.value, "content": blocks})

    return safe_utf8_json_tree(result)


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
    return safe_utf8_json_tree(result)


class AnthropicAdapter(APIAdapter):
    """Adapter for Anthropic's Messages API (direct, first-party)."""

    def __init__(self, config: ApiConfig):
        self.config = config
        self._cached_context_window: int | None = None
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
        if config.http_headers:
            kwargs["default_headers"] = config.http_headers

        self.client = anthropic.AsyncAnthropic(**kwargs)
        self._api_key = api_key

    def _messages_url(self) -> str | None:
        """Return a Messages API URL for Anthropic-compatible custom endpoints."""
        if not self.config.base_url:
            return "https://api.anthropic.com/v1/messages"
        base = self.config.base_url.rstrip("/")
        if base.endswith("/v1"):
            return f"{base}/messages"
        return f"{base}/v1/messages"

    def _manual_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {
            "accept": "text/event-stream",
            "content-type": "application/json",
            "anthropic-version": ANTHROPIC_VERSION,
        }
        headers.update(self.config.http_headers or {})
        lower_header_names = {k.lower() for k in headers}
        if self._api_key and "x-api-key" not in lower_header_names:
            headers["x-api-key"] = self._api_key
        return headers

    async def _stream_message_httpx(
        self,
        params: dict[str, Any],
    ) -> AsyncGenerator[StreamChunk, None]:
        """Stream from Anthropic-compatible routers without SDK helper headers.

        Some Anthropic-compatible proxies reject the Python SDK's stream helper
        headers and return a JSON error body with HTTP 200. Reading SSE directly
        keeps the wire format Anthropic-compatible while avoiding those headers.
        """
        url = self._messages_url()
        if not url:
            return

        payload = dict(params)
        payload["stream"] = True

        timeout = httpx.Timeout(
            float(self.config.timeout),
            connect=min(float(self.config.timeout), 30.0),
            read=float(self.config.timeout),
            write=float(self.config.timeout),
        )
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST",
                url,
                headers=self._manual_headers(),
                json=payload,
            ) as response:
                content_type = response.headers.get("content-type", "")
                if response.status_code >= 400:
                    body = await response.aread()
                    yield StreamChunk(
                        type="error",
                        error=(
                            body.decode("utf-8", errors="replace")
                            or response.reason_phrase
                        ),
                    )
                    return

                if "text/event-stream" not in content_type.lower():
                    body = await response.aread()
                    text = body.decode("utf-8", errors="replace")
                    error_text = text
                    try:
                        data = json.loads(text)
                        error_text = (
                            data.get("error", {}).get("message")
                            if isinstance(data.get("error"), dict)
                            else data.get("code_msg") or data.get("message") or text
                        )
                    except Exception:
                        pass
                    yield StreamChunk(type="error", error=str(error_text))
                    return

                current_tool_id = ""
                current_tool_name = ""
                tool_input_buffer = ""

                async for raw_line in response.aiter_lines():
                    if not raw_line.startswith("data:"):
                        continue
                    raw_data = raw_line[5:].strip()
                    if not raw_data or raw_data == "[DONE]":
                        continue
                    try:
                        event = json.loads(raw_data)
                    except json.JSONDecodeError:
                        continue

                    event_type = event.get("type", "")
                    if event_type == "content_block_start":
                        block = event.get("content_block") or {}
                        block_type = block.get("type")
                        if block_type == "tool_use":
                            current_tool_id = str(block.get("id") or "")
                            current_tool_name = str(block.get("name") or "")
                            tool_input_buffer = ""
                            yield StreamChunk(
                                type="tool_use_start",
                                tool_use_id=current_tool_id,
                                tool_name=current_tool_name,
                            )

                    elif event_type == "content_block_delta":
                        delta = event.get("delta") or {}
                        delta_type = delta.get("type")
                        if delta_type == "text_delta":
                            yield StreamChunk(
                                type="text",
                                text=safe_utf8_str(str(delta.get("text") or "")),
                            )
                        elif delta_type == "thinking_delta":
                            yield StreamChunk(
                                type="thinking",
                                text=safe_utf8_str(str(delta.get("thinking") or "")),
                            )
                        elif delta_type == "input_json_delta":
                            partial = str(delta.get("partial_json") or "")
                            tool_input_buffer += partial
                            yield StreamChunk(
                                type="tool_use_delta",
                                tool_use_id=current_tool_id,
                                tool_input_json=partial,
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
                        raw_usage = event.get("usage")
                        if isinstance(raw_usage, dict):
                            usage = _anthropic_usage(
                                raw_usage,
                                include_input=True,
                                include_output=True,
                            )
                        delta = event.get("delta") or {}
                        yield StreamChunk(
                            type="message_delta",
                            stop_reason=str(delta.get("stop_reason") or ""),
                            usage=usage,
                        )

                    elif event_type == "message_start":
                        usage = {}
                        message = event.get("message") or {}
                        raw_usage = message.get("usage")
                        if isinstance(raw_usage, dict):
                            usage = _anthropic_usage(
                                raw_usage,
                                include_input=True,
                                include_output=True,
                            )
                        yield StreamChunk(type="message_start", usage=usage)

                    elif event_type == "message_stop":
                        yield StreamChunk(type="message_stop")

                    elif event_type == "error":
                        error = event.get("error") or {}
                        if isinstance(error, dict):
                            message = error.get("message") or json.dumps(error, ensure_ascii=False)
                        else:
                            message = str(error or event)
                        yield StreamChunk(type="error", error=message)

    async def resolve_context_window(self) -> int:
        """Query the Anthropic Models API for context window, with caching."""
        from crabcode_core.api.model_info import DEFAULT_CONTEXT_WINDOW, lookup_context_window

        if self.config.context_window:
            return self.config.context_window

        if self._cached_context_window is not None:
            return self._cached_context_window

        model = self.config.model
        if model:
            try:
                model_info = await self.client.models.retrieve(model_id=model)
                window = getattr(model_info, "max_input_tokens", None)
                if window:
                    self._cached_context_window = window
                    return window
            except Exception:
                logger.debug("Failed to query Anthropic Models API for %s", model, exc_info=True)

        looked_up = lookup_context_window(model)
        if looked_up is not None:
            self._cached_context_window = looked_up
            return looked_up

        self._cached_context_window = DEFAULT_CONTEXT_WINDOW
        return DEFAULT_CONTEXT_WINDOW

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

        system_blocks = safe_utf8_json_tree(
            [{"type": "text", "text": s} for s in system if s]
        )

        params: dict[str, Any] = {
            "model": model,
            "max_tokens": config.max_tokens,
            "system": system_blocks,
            "messages": _messages_to_api(messages),
        }

        api_tools = _tools_to_api(tools)
        if api_tools:
            params["tools"] = api_tools

        if config.thinking_enabled:
            params["thinking"] = {
                "type": "enabled",
                "budget_tokens": config.thinking_budget,
            }

        if config.reasoning_effort in {"low", "medium", "high", "xhigh", "max"}:
            params["output_config"] = {"effort": config.reasoning_effort}

        if config.temperature is not None:
            params["temperature"] = config.temperature

        transport = self.config.anthropic_stream_transport
        use_httpx_stream = transport == "httpx" or (
            transport == "auto" and bool(self.config.base_url)
        )
        if use_httpx_stream:
            async for chunk in self._stream_message_httpx(params):
                yield chunk
            return

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
                            yield StreamChunk(type="text", text=safe_utf8_str(delta.text))
                        elif delta.type == "thinking_delta":
                            yield StreamChunk(
                                type="thinking", text=safe_utf8_str(delta.thinking)
                            )
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
                        usage = _anthropic_usage(
                            event.usage,
                            include_input=True,
                            include_output=True,
                        )
                    stop_reason = getattr(event.delta, "stop_reason", "") or ""
                    yield StreamChunk(
                        type="message_delta",
                        stop_reason=stop_reason,
                        usage=usage,
                    )

                elif event_type == "message_start":
                    usage = {}
                    if hasattr(event.message, "usage") and event.message.usage:
                        usage = _anthropic_usage(
                            event.message.usage,
                            include_input=True,
                            include_output=True,
                        )
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
            logger.debug("Anthropic token counting failed; using heuristic estimate", exc_info=True)
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
