"""OpenAI API adapter — translates between internal Anthropic-style messages and OpenAI format."""

from __future__ import annotations

import json
import os
from typing import Any, AsyncGenerator

from crabcode_core.api.base import (
    APIAdapter,
    ModelConfig,
    StreamChunk,
    normalize_openai_usage,
)
from crabcode_core.types.config import ApiConfig
from crabcode_core.utf8_sanitize import safe_utf8_json_tree, safe_utf8_str
from crabcode_core.types.message import (
    Message,
    MessageRole,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    ThinkingBlock,
    ImageBlock,
)


def _messages_to_openai(
    messages: list[Message],
    system: list[str],
    pass_reasoning_content: bool = False,
) -> list[dict[str, Any]]:
    """Convert internal messages + system to OpenAI chat format."""
    result: list[dict[str, Any]] = []
    pending_tool_ids: set[str] = set()
    deferred_images: list[dict[str, Any]] = []

    def require_complete_image_batch() -> None:
        if deferred_images:
            raise ValueError(
                "Cannot send tool images: missing tool results for "
                + ", ".join(sorted(pending_tool_ids))
            )

    system_text = "\n\n".join(s for s in system if s)
    if system_text:
        result.append({"role": "system", "content": system_text})

    for msg in messages:
        if msg.role == MessageRole.SYSTEM:
            continue

        if isinstance(msg.content, str):
            require_complete_image_batch()
            result.append({"role": msg.role.value, "content": msg.content})
            continue

        text_parts: list[str] = []
        reasoning_parts: list[str] = []
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
                reasoning_parts.append(block.thinking)

        if msg.role == MessageRole.ASSISTANT:
            require_complete_image_batch()
            pending_tool_ids = {call["id"] for call in tool_calls}
            entry: dict[str, Any] = {"role": "assistant"}
            if text_parts:
                entry["content"] = "".join(text_parts)
            elif tool_calls:
                # OpenAI expects explicit null when the turn is tool-only.
                entry["content"] = None
            if tool_calls:
                entry["tool_calls"] = tool_calls
            if pass_reasoning_content and reasoning_parts:
                entry["reasoning_content"] = "".join(reasoning_parts)
            result.append(entry)
        elif msg.role == MessageRole.USER:
            if not tool_results:
                require_complete_image_batch()
            if tool_results:
                result.extend(tool_results)
                pending_tool_ids.difference_update(
                    item["tool_call_id"] for item in tool_results
                )

            # Chat Completions requires all tool results before the next user
            # message. Hold tool images until the entire call batch is closed.
            has_images = isinstance(msg.content, list) and any(
                isinstance(b, ImageBlock) for b in msg.content
            )
            if has_images:
                content_parts: list[dict[str, Any]] = []
                for block in msg.content if isinstance(msg.content, list) else []:
                    if isinstance(block, TextBlock):
                        content_parts.append({"type": "text", "text": block.text})
                    elif isinstance(block, ImageBlock):
                        # Build data URL from base64 source
                        source = block.source
                        if source.get("type") == "base64":
                            data_url = f"data:{source.get('media_type', 'image/png')};base64,{source.get('data', '')}"
                        else:
                            data_url = source.get("url", "")
                        content_parts.append({
                            "type": "image_url",
                            "image_url": {"url": data_url},
                        })
                if content_parts:
                    image_message = {"role": "user", "content": content_parts}
                    if tool_results:
                        deferred_images.append(image_message)
                    else:
                        result.append(image_message)
            elif text_parts and not tool_results:
                result.append({"role": "user", "content": "".join(text_parts)})

            if not pending_tool_ids:
                result.extend(deferred_images)
                deferred_images.clear()

    require_complete_image_batch()
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
    """Adapter for OpenAI-compatible APIs.

    Designed to be subclassed by providers that use an OpenAI-compatible
    chat completions surface (Azure, Ollama, Gemini, etc.).  Subclasses
    can override :meth:`_create_client` and :meth:`_resolve_model` to
    customise client construction and model/deployment name resolution.
    """

    def __init__(self, config: ApiConfig):
        import openai

        self._sdk = openai
        self.config = config
        self.client = self._create_client(config)

    def _create_client(self, config: ApiConfig) -> Any:
        """Create the async OpenAI client.  Override in subclasses."""
        import openai

        api_key: str | None = None
        if config.api_key_env:
            api_key = os.environ.get(config.api_key_env)
        if not api_key:
            api_key = os.environ.get("OPENAI_API_KEY")

        kwargs: dict[str, Any] = {}
        if api_key:
            kwargs["api_key"] = api_key
        if config.base_url:
            kwargs["base_url"] = config.base_url
        if config.http_headers:
            kwargs["default_headers"] = config.http_headers

        from crabcode_core.api.network import sdk_options
        return openai.AsyncOpenAI(**kwargs, **sdk_options(config, self._sdk))

    def _resolve_model(self, config: ModelConfig) -> str:
        """Resolve the model name for the API call.  Override in subclasses."""
        return config.model or self.config.model or "gpt-4o"

    async def stream_message(
        self,
        messages: list[Message],
        system: list[str],
        tools: list[dict[str, Any]],
        config: ModelConfig,
    ) -> AsyncGenerator[StreamChunk, None]:
        model = self._resolve_model(config)

        params: dict[str, Any] = {
            "model": model,
            "max_tokens": config.max_tokens,
            "messages": _messages_to_openai(
                messages,
                system,
                pass_reasoning_content=self.config.pass_reasoning_content,
            ),
            "stream": True,
            "stream_options": {"include_usage": True},
        }

        api_tools = _tools_to_openai(tools)
        if api_tools:
            params["tools"] = api_tools

        if config.temperature is not None:
            params["temperature"] = config.temperature

        extra_body = safe_utf8_json_tree(dict(self.config.extra_body or {}))
        if extra_body:
            params["extra_body"] = extra_body

        tool_call_buffers: dict[int, dict[str, str]] = {}

        stream = await self.client.chat.completions.create(**params)
        async for chunk in stream:
            if not chunk.choices:
                if chunk.usage:
                    yield StreamChunk(
                        type="message_delta",
                        usage=normalize_openai_usage(chunk.usage),
                        model_id=str(getattr(chunk, "model", "") or ""),
                    )
                continue

            delta = chunk.choices[0].delta
            finish_reason = chunk.choices[0].finish_reason

            reasoning_content = getattr(delta, "reasoning_content", None)
            if reasoning_content:
                yield StreamChunk(
                    type="thinking",
                    text=safe_utf8_str(reasoning_content),
                )

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

            if finish_reason:
                # In particular, length is a completed but truncated response.
                # Preserve it so the query loop reports output_limit instead
                # of mistaking the end of the iterator for a successful turn.
                # Keep consuming the stream for the separate usage-only chunk.
                usage = normalize_openai_usage(chunk.usage) if chunk.usage else {}
                yield StreamChunk(
                    type="message_stop",
                    stop_reason={"stop": "end_turn", "tool_calls": "tool_use"}.get(
                        finish_reason, finish_reason,
                    ),
                    usage=usage,
                    model_id=str(getattr(chunk, "model", "") or ""),
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
