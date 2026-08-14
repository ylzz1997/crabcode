"""OpenAI Responses (Codex) API adapter — uses the newer Responses API endpoint.

Supports OpenAI's Responses API which is used by Codex and o-series models.
Falls back to Chat Completions API for models that don't support the Responses API.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, AsyncGenerator

import httpx

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


OPENAI_RESPONSES_BASE_URL = "https://api.openai.com/v1"
CODEX_OAUTH_BASE_URL = "https://chatgpt.com/backend-api/codex"
CODEX_AUTH_FILENAME = "auth.json"


def _default_codex_auth_path() -> Path:
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        return Path(codex_home).expanduser() / CODEX_AUTH_FILENAME
    return Path.home() / ".codex" / CODEX_AUTH_FILENAME


def _resolve_codex_auth_path(config: ApiConfig) -> Path:
    if config.codex_auth_path:
        return Path(config.codex_auth_path).expanduser()
    return _default_codex_auth_path()


def _load_codex_oauth(config: ApiConfig) -> tuple[str | None, str | None]:
    auth_path = _resolve_codex_auth_path(config)
    try:
        payload = json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, None

    tokens = payload.get("tokens")
    if not isinstance(tokens, dict):
        return None, None

    access_token = tokens.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        return None, None

    account_id = tokens.get("account_id")
    if not isinstance(account_id, str) or not account_id:
        account_id = None

    return access_token, account_id


def _has_header(headers: dict[str, str], name: str) -> bool:
    needle = name.lower()
    return any(key.lower() == needle for key in headers)


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
            # Add user message — handle multimodal content with images
            has_images = isinstance(msg.content, list) and any(
                isinstance(b, ImageBlock) for b in msg.content
            )
            if has_images:
                content_parts: list[dict[str, Any]] = []
                for block in msg.content if isinstance(msg.content, list) else []:
                    if isinstance(block, TextBlock):
                        content_parts.append({"type": "input_text", "text": block.text})
                    elif isinstance(block, ImageBlock):
                        source = block.source
                        if source.get("type") == "base64":
                            data_url = f"data:{source.get('media_type', 'image/png')};base64,{source.get('data', '')}"
                        else:
                            data_url = source.get("url", "")
                        content_parts.append({
                            "type": "input_image",
                            "image_url": data_url,
                        })
                if content_parts:
                    result.append({
                        "type": "message",
                        "role": "user",
                        "content": content_parts,
                    })
            elif text_parts and not tool_results:
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
        usage = normalize_openai_usage(response.usage)

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


async def _iter_sse_payloads(
    response: httpx.Response,
) -> AsyncGenerator[tuple[str, dict[str, Any]], None]:
    """Yield parsed SSE payloads, tolerating split event/data frames.

    Some OpenAI-compatible proxies emit:

        event: response.created

        data: {...}

    which inserts an extra blank line between the event name and payload.
    The official OpenAI Python SDK treats that as two separate SSE events and
    fails to decode the empty payload. We keep the pending event name across
    blank lines until a data block arrives.
    """

    current_event: str | None = None
    data_lines: list[str] = []

    async for raw_line in response.aiter_lines():
        line = raw_line.rstrip("\r")

        if line.startswith("event:"):
            if data_lines:
                data = "\n".join(data_lines)
                if data and data != "[DONE]":
                    yield current_event or "", json.loads(data)
                data_lines = []
            current_event = line.split(":", 1)[1].strip()
            continue

        if line.startswith("data:"):
            data_lines.append(line.split(":", 1)[1].lstrip())
            continue

        if line == "":
            if not data_lines:
                continue
            data = "\n".join(data_lines)
            data_lines = []
            if data and data != "[DONE]":
                yield current_event or "", json.loads(data)
            current_event = None

    if data_lines:
        data = "\n".join(data_lines)
        if data and data != "[DONE]":
            yield current_event or "", json.loads(data)


class CodexAdapter(APIAdapter):
    """Adapter for OpenAI's Responses API (Codex / o-series models).

    Uses client.responses.create() with stream=True.
    """

    def __init__(self, config: ApiConfig):
        import openai

        self.config = config
        api_key = None
        oauth_account_id = None
        using_codex_oauth = False
        if config.api_key_env:
            api_key = os.environ.get(config.api_key_env)
        if not api_key:
            api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key and not config.base_url:
            api_key, oauth_account_id = _load_codex_oauth(config)
            using_codex_oauth = bool(api_key)
            if not api_key:
                auth_path = _resolve_codex_auth_path(config)
                raise RuntimeError(
                    "Codex provider requires an API key, a base_url, or a "
                    f"Codex OAuth auth file at {auth_path}"
                )
        self._api_key = api_key
        self._codex_oauth_account_id = oauth_account_id
        self._using_codex_oauth = using_codex_oauth
        self._base_url = (
            CODEX_OAUTH_BASE_URL
            if using_codex_oauth
            else config.base_url or OPENAI_RESPONSES_BASE_URL
        )

        kwargs: dict[str, Any] = {}
        if api_key:
            kwargs["api_key"] = api_key
        if config.base_url:
            kwargs["base_url"] = config.base_url
        if config.http_headers:
            kwargs["default_headers"] = config.http_headers

        self.client = openai.AsyncOpenAI(**kwargs)

    def _raw_responses_headers(self) -> dict[str, str]:
        headers = dict(self.config.http_headers or {})
        if self._api_key:
            if not _has_header(headers, "Authorization"):
                headers["Authorization"] = f"Bearer {self._api_key}"
        if self._codex_oauth_account_id and not _has_header(
            headers, "ChatGPT-Account-Id"
        ):
            headers["ChatGPT-Account-Id"] = self._codex_oauth_account_id
        headers.setdefault("Content-Type", "application/json")
        headers.setdefault("Accept", "text/event-stream")
        return headers

    def _prompt_cache_key(self) -> str | None:
        if self.config.prompt_cache_key:
            return safe_utf8_str(self.config.prompt_cache_key)

        session_id = (self.config.http_headers or {}).get("session_id")
        if session_id:
            return safe_utf8_str(session_id)

        return None

    async def _stream_via_httpx(
        self,
        params: dict[str, Any],
    ) -> AsyncGenerator[StreamChunk, None]:
        url = f"{self._base_url.rstrip('/')}/responses"
        active_calls: dict[str, dict[str, str]] = {}

        async with httpx.AsyncClient(timeout=self.config.timeout) as client:
            async with client.stream(
                "POST",
                url,
                headers=self._raw_responses_headers(),
                json=safe_utf8_json_tree(params),
            ) as response:
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError:
                    body = (await response.aread()).decode("utf-8", "replace").strip()
                    if response.status_code == 401 and self._using_codex_oauth:
                        body = (
                            "Codex OAuth token was rejected. Run `codex login` "
                            "to refresh your Codex auth file, or configure "
                            "api_key_env/base_url instead."
                        )
                    error_message = f"HTTP {response.status_code}"
                    if body:
                        error_message = f"{error_message}: {body}"
                    yield StreamChunk(
                        type="error",
                        error=safe_utf8_str(error_message),
                    )
                    return

                async for sse_event, payload in _iter_sse_payloads(response):
                    event_type = payload.get("type") or sse_event

                    if event_type == "response.output_text.delta":
                        yield StreamChunk(
                            type="text",
                            text=safe_utf8_str(str(payload.get("delta", ""))),
                        )

                    elif event_type == "response.function_call_arguments.delta":
                        item_id = str(payload.get("item_id", ""))
                        if item_id not in active_calls:
                            active_calls[item_id] = {
                                "call_id": "",
                                "name": "",
                                "arguments": "",
                            }
                        buf = active_calls[item_id]
                        delta = str(payload.get("delta", ""))
                        buf["arguments"] += delta
                        call_id = buf.get("call_id", "") or item_id
                        yield StreamChunk(
                            type="tool_use_delta",
                            tool_use_id=call_id,
                            tool_input_json=delta,
                        )

                    elif event_type == "response.output_item.added":
                        item = payload.get("item", {}) or {}
                        if item.get("type") == "function_call":
                            item_id = str(item.get("id", ""))
                            call_id = str(item.get("call_id", "") or item_id)
                            name = str(item.get("name", ""))
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

                    elif event_type == "response.function_call_arguments.done":
                        item_id = str(payload.get("item_id", ""))
                        buf = active_calls.get(item_id, {})
                        yield StreamChunk(
                            type="tool_use_end",
                            tool_use_id=buf.get("call_id", item_id),
                            tool_name=buf.get("name", ""),
                            tool_input_json=str(
                                payload.get("arguments", buf.get("arguments", ""))
                            ),
                        )
                        active_calls.pop(item_id, None)

                    elif event_type == "response.output_item.done":
                        item = payload.get("item", {}) or {}
                        if item.get("type") == "function_call":
                            item_id = str(item.get("id", ""))
                            if item_id and item_id in active_calls:
                                buf = active_calls.pop(item_id)
                                yield StreamChunk(
                                    type="tool_use_end",
                                    tool_use_id=buf.get("call_id", item_id),
                                    tool_name=buf.get("name", ""),
                                    tool_input_json=buf.get("arguments", ""),
                                )

                    elif event_type == "response.reasoning_summary_text.delta":
                        yield StreamChunk(
                            type="thinking",
                            text=safe_utf8_str(str(payload.get("delta", ""))),
                        )

                    elif event_type == "response.completed":
                        usage_payload = (
                            payload.get("response", {}) or {}
                        ).get("usage", {}) or {}
                        usage = normalize_openai_usage(usage_payload)
                        yield StreamChunk(
                            type="message_stop",
                            stop_reason="end_turn",
                            usage=usage,
                        )

                    elif event_type == "response.failed":
                        error_payload = ((payload.get("response", {}) or {}).get("error", {}) or {})
                        yield StreamChunk(
                            type="error",
                            error=safe_utf8_str(
                                str(error_payload.get("message", "Response failed"))
                            ),
                        )

                    elif event_type == "response.incomplete":
                        yield StreamChunk(
                            type="error",
                            error="Response incomplete (max output tokens or content filter)",
                        )

                    elif event_type in {"response.error", "error"}:
                        error_payload = payload.get("error", {})
                        if isinstance(error_payload, dict):
                            error_msg = error_payload.get("message", "")
                        else:
                            error_msg = str(error_payload)
                        yield StreamChunk(
                            type="error",
                            error=safe_utf8_str(error_msg or "Unknown error"),
                        )

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

        extra_body = safe_utf8_json_tree(dict(self.config.extra_body or {}))
        prompt_cache_key = self._prompt_cache_key()
        if prompt_cache_key:
            extra_body["prompt_cache_key"] = prompt_cache_key
        if self.config.prompt_cache_retention:
            extra_body["prompt_cache_retention"] = self.config.prompt_cache_retention

        # For o-series models, configure reasoning
        reasoning_effort = (
            config.reasoning_effort
            if config.reasoning_effort is not None
            else self.config.reasoning_effort
        )
        if reasoning_effort is not None:
            if reasoning_effort != "none":
                params["reasoning"] = {
                    "effort": reasoning_effort,
                    "summary": "auto",
                }
        elif config.thinking_enabled:
            # Backward-compatible mapping from thinking_budget to reasoning effort
            budget = config.thinking_budget
            if budget >= 20000:
                effort = "high"
            elif budget >= 8000:
                effort = "medium"
            else:
                effort = "low"
            params["reasoning"] = {"effort": effort, "summary": "auto"}

        sdk_params = dict(params)
        if extra_body:
            sdk_params["extra_body"] = extra_body

        raw_params = dict(params)
        if extra_body:
            raw_params.update(extra_body)

        if self._using_codex_oauth:
            # The Codex OAuth endpoint does not support server-side response
            # storage and rejects requests unless this is explicitly false.
            # Force the required value even if extra_body contains store=true.
            raw_params["store"] = False
            # Output limits are managed by the Codex backend; unlike the public
            # Responses API, its OAuth endpoint rejects max_output_tokens.
            raw_params.pop("max_output_tokens", None)
            async for chunk in self._stream_via_httpx(raw_params):
                yield chunk
            return

        # Track active function calls by item_id
        active_calls: dict[str, dict[str, str]] = {}
        emitted_stream_event = False
        try:
            stream = await self.client.responses.create(**sdk_params)
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
                        usage = normalize_openai_usage(response.usage)
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

            async for chunk in self._stream_via_httpx(raw_params):
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
