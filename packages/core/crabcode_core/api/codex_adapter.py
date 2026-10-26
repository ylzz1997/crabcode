"""OpenAI Responses (Codex) API adapter — uses the newer Responses API endpoint.

Supports OpenAI's Responses API which is used by Codex and o-series models.
Falls back to Chat Completions API for models that don't support the Responses API.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncGenerator

import httpx

from crabcode_core.api.base import (
    APIAdapter,
    ModelConfig,
    StreamChunk,
    normalize_openai_usage,
)
from crabcode_core.api.error_logging import response_request_id
from crabcode_core.query.retry import MAX_REQUEST_RETRIES, request_retry_backoff
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
CODEX_MODELS_CACHE_FILENAME = "models_cache.json"


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


def _load_codex_context_window(config: ApiConfig, model: str | None) -> int | None:
    """Read Codex CLI's effective context window for an OAuth model."""
    if not model:
        return None

    cache_path = _resolve_codex_auth_path(config).with_name(
        CODEX_MODELS_CACHE_FILENAME
    )
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return None

    for item in models:
        if not isinstance(item, dict) or item.get("slug") != model:
            continue

        context_window = item.get("context_window")
        if (
            not isinstance(context_window, int)
            or isinstance(context_window, bool)
            or context_window <= 0
        ):
            return None

        effective_percent = item.get("effective_context_window_percent")
        if (
            isinstance(effective_percent, int)
            and not isinstance(effective_percent, bool)
            and 0 < effective_percent <= 100
        ):
            return context_window * effective_percent // 100
        return context_window

    return None


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


def _response_error_message(payload: Any) -> str | None:
    """Extract a useful error string from proxy-specific response shapes."""
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if isinstance(error, dict):
        message = error.get("message") or error.get("detail") or error.get("code")
        if message:
            return safe_utf8_str(str(message))
    elif error:
        return safe_utf8_str(str(error))
    for key in ("message", "detail", "error_message"):
        value = payload.get(key)
        if value:
            return safe_utf8_str(str(value))
    response = payload.get("response")
    if isinstance(response, dict):
        return _response_error_message(response)
    return None


def _response_error_code(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if isinstance(error, dict) and error.get("code"):
        return str(error["code"])
    response = payload.get("response")
    if isinstance(response, dict):
        return _response_error_code(response)
    return ""


def _retry_after_from_error(message: str, code: str) -> float | None:
    if code != "rate_limit_exceeded":
        return None
    match = re.search(
        r"(?:please\s+)?try\s+again\s+in\s+"
        r"(\d+(?:\.\d+)?)\s*(ms|s|seconds?)\b",
        message,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    value = float(match.group(1))
    return value / 1000.0 if match.group(2).lower() == "ms" else value


def _responses_error_chunk(
    payload: Any, fallback: str, *, error_body: Any = None, response: Any = None,
) -> StreamChunk:
    message = safe_utf8_str(_response_error_message(payload) or fallback)
    code = _response_error_code(payload)
    terminal_codes = {
        "context_length_exceeded",
        "insufficient_quota",
        "usage_not_included",
        "server_is_overloaded",
        "slow_down",
        "cyber_policy",
        "invalid_prompt",
        "bio_policy",
        "misalignment_policy_violation",
    }
    return StreamChunk(
        type="error",
        error=message,
        error_body=payload if error_body is None else error_body,
        error_status_code=getattr(response, "status_code", None),
        error_request_id=response_request_id(response),
        retryable=False if code in terminal_codes else True,
        retry_after=_retry_after_from_error(message, code),
    )


def _incomplete_response_message(response: Any) -> str:
    reason = "unknown"
    if isinstance(response, dict):
        details = response.get("incomplete_details")
        if isinstance(details, dict) and details.get("reason"):
            reason = str(details["reason"])
    else:
        details = getattr(response, "incomplete_details", None)
        value = getattr(details, "reason", None)
        if value:
            reason = str(value)
    return f"Incomplete response returned, reason: {reason}"


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

        chunks.append(
            StreamChunk(
                type="response_item_done",
                item_id=str(getattr(item, "id", "") or ""),
                item_type=str(item_type),
            )
        )

    usage = {}
    if hasattr(response, "usage") and response.usage:
        usage = normalize_openai_usage(response.usage)

    if not chunks and getattr(response, "error", None):
        err = response.error
        error_msg = safe_utf8_str(getattr(err, "message", str(err)))
        chunks.append(StreamChunk(
            type="error", error=error_msg or "Response failed", error_body=err,
        ))
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

    def parse_payload(data: str) -> dict[str, Any] | None:
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        return payload

    async for raw_line in response.aiter_lines():
        line = raw_line.rstrip("\r")

        if line.startswith("event:"):
            if data_lines:
                data = "\n".join(data_lines)
                if data and data != "[DONE]":
                    payload = parse_payload(data)
                    if payload is not None:
                        yield current_event or "", payload
                data_lines = []
            current_event = line.split(":", 1)[1].strip()
            continue

        if line.startswith("data:"):
            data_lines.append(line.split(":", 1)[1].lstrip())
            continue

        # A few compatible gateways return one JSON object per line while
        # still advertising a streaming response. Treat it as a response
        # payload instead of silently dropping it and reporting an empty turn.
        if line == "[DONE]":
            continue
        if line.startswith(("{", "[")):
            payload = parse_payload(line)
            if payload is not None:
                yield current_event or "", payload
            current_event = None
            continue

        if line == "":
            if not data_lines:
                continue
            data = "\n".join(data_lines)
            data_lines = []
            if data and data != "[DONE]":
                payload = parse_payload(data)
                if payload is not None:
                    yield current_event or "", payload
            current_event = None

    if data_lines:
        data = "\n".join(data_lines)
        if data and data != "[DONE]":
            payload = parse_payload(data)
            if payload is not None:
                yield current_event or "", payload


class CodexAdapter(APIAdapter):
    """Adapter for OpenAI's Responses API (Codex / o-series models).

    Uses client.responses.create() with stream=True.
    """

    emits_response_item_events = True

    def __init__(self, config: ApiConfig):
        import openai

        self._sdk = openai
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

        # Keep retry policy above the HTTP transport. Disable the SDK's
        # differing built-in policy (which also retries 429) and apply the
        # configured policy explicitly in _create_sdk_response_stream().
        from crabcode_core.api.network import sdk_options
        kwargs.update(sdk_options(config, self._sdk))
        self.client = openai.AsyncOpenAI(**kwargs)

    async def resolve_context_window(self) -> int:
        """Use Codex runtime metadata when authenticated through Codex OAuth."""
        if self.config.context_window:
            return self.config.context_window

        if self._using_codex_oauth:
            from crabcode_core.api.model_info import DEFAULT_CONTEXT_WINDOW

            cached_window = _load_codex_context_window(
                self.config,
                self.config.model,
            )
            return cached_window or DEFAULT_CONTEXT_WINDOW

        return await super().resolve_context_window()

    def _request_retry_limit(self) -> int:
        return min(
            max(0, int(getattr(self.config, "request_max_retries", 4))),
            MAX_REQUEST_RETRIES,
        )

    @staticmethod
    def _request_error_is_retryable(exc: BaseException) -> bool:
        """Retry transport failures and 5xx at this layer, but not 429."""
        from crabcode_core.api.network import certificate_failure
        if certificate_failure(exc):
            return False
        pending: list[BaseException] = [exc]
        seen: set[int] = set()
        while pending:
            current = pending.pop()
            if id(current) in seen:
                continue
            seen.add(id(current))
            if isinstance(current, httpx.TransportError):
                return True
            status = getattr(current, "status_code", None)
            if isinstance(status, int) and status >= 500:
                return True
            for nested in (current.__cause__, current.__context__):
                if nested is not None:
                    pending.append(nested)
        return False

    async def _create_sdk_response_stream(self, params: dict[str, Any]) -> Any:
        """Create a Responses stream with the request-layer retry policy."""
        max_retries = self._request_retry_limit()
        for attempt in range(max_retries + 1):
            try:
                return await self.client.responses.create(**params)
            except Exception as exc:
                if (
                    attempt >= max_retries
                    or not self._request_error_is_retryable(exc)
                ):
                    raise
                await asyncio.sleep(request_retry_backoff(attempt + 1))
        raise RuntimeError("unreachable request retry state")

    async def _send_httpx_stream_request(
        self,
        client: httpx.AsyncClient,
        *,
        url: str,
        headers: dict[str, str],
        params: dict[str, Any],
    ) -> httpx.Response:
        """Open a stream with the silent request-layer retry policy."""
        max_retries = self._request_retry_limit()
        for attempt in range(max_retries + 1):
            request = client.build_request(
                "POST",
                url,
                headers=headers,
                json=safe_utf8_json_tree(params),
            )
            try:
                response = await client.send(request, stream=True)
            except httpx.TransportError as exc:
                if attempt >= max_retries or not self._request_error_is_retryable(exc):
                    raise
                await asyncio.sleep(request_retry_backoff(attempt + 1))
                continue
            if response.status_code >= 500 and attempt < max_retries:
                await response.aclose()
                await asyncio.sleep(request_retry_backoff(attempt + 1))
                continue
            return response
        raise RuntimeError("unreachable request retry state")

    @asynccontextmanager
    async def _httpx_stream_with_retry(
        self,
        client: httpx.AsyncClient,
        *,
        url: str,
        headers: dict[str, str],
        params: dict[str, Any],
    ) -> AsyncGenerator[httpx.Response, None]:
        response = await self._send_httpx_stream_request(
            client,
            url=url,
            headers=headers,
            params=params,
        )
        try:
            yield response
        finally:
            await response.aclose()

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

    @property
    def supports_image_generation(self) -> bool:
        """OAuth is automatic; compatible endpoints require explicit opt-in."""
        return self._using_codex_oauth or self.config.image_generation_enabled

    async def generate_images(
        self,
        prompt: str,
        *,
        model: str,
        reference_images: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        """Run the native Responses image tool using this provider's credentials.

        This deliberately does not call the API-key-only Images API or fall
        back to another provider. Partial previews are not final artifacts.
        """
        if not self.supports_image_generation:
            raise RuntimeError(
                "Image generation requires Codex auth.json mode or "
                "image_generation_enabled=true on a compatible Codex endpoint"
            )

        headers = self._raw_responses_headers()
        secrets = [self._api_key]
        if self._using_codex_oauth:
            # Pick up credentials refreshed by `codex login` since startup.
            # Never read or forward Codex credentials to a compatible endpoint.
            token, account_id = _load_codex_oauth(self.config)
            if not token:
                raise RuntimeError("Codex auth.json is unavailable; run `codex login`")
            secrets.extend([token, account_id])
            headers = {
                key: value for key, value in headers.items()
                if key.lower() not in {"authorization", "chatgpt-account-id"}
            }
            headers["Authorization"] = f"Bearer {token}"
            if account_id:
                headers["ChatGPT-Account-Id"] = account_id
        for key, value in headers.items():
            if any(part in key.lower() for part in ("authorization", "token", "key", "account-id")):
                secrets.extend([value, value.removeprefix("Bearer ")])

        content: list[dict[str, str]] = [{"type": "input_text", "text": prompt}]
        content.extend({
            "type": "input_image",
            "image_url": f"data:{image['media_type']};base64,{image['data']}",
        } for image in reference_images)
        # Preserve compatible-provider routing/options while keeping the native
        # image request contract intact. OAuth retains its validated payload.
        params: dict[str, Any] = (
            {} if self._using_codex_oauth else dict(self.config.extra_body or {})
        )
        params.update({
            "model": model,
            "instructions": "Generate or edit the requested image using the image generation tool.",
            "input": [{"role": "user", "content": content}],
            "tools": [{"type": "image_generation"}],
            "tool_choice": {"type": "image_generation"},
            "stream": True,
            "store": False,
        })
        cache_key = self._prompt_cache_key()
        if cache_key:
            params["prompt_cache_key"] = cache_key
        if not self._using_codex_oauth and self.config.prompt_cache_retention:
            params["prompt_cache_retention"] = self.config.prompt_cache_retention

        images: dict[str, dict[str, str]] = {}
        completed = False

        def collect(item: dict[str, Any]) -> None:
            if item.get("type") != "image_generation_call":
                return
            if item.get("status") not in {None, "completed"}:
                return
            result = item.get("result")
            if not isinstance(result, str) or not result:
                return
            output_format = item.get("output_format") or "png"
            media_type = {"png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp"}.get(output_format)
            if not media_type:
                raise RuntimeError(f"Unsupported generated image format: {output_format}")
            images[str(item.get("id") or result)] = {"media_type": media_type, "data": result}

        from crabcode_core.api.network import http_options, request_timeout
        async with httpx.AsyncClient(timeout=request_timeout(self.config), **http_options(self.config)) as client:
            async with self._httpx_stream_with_retry(
                client,
                url=f"{self._base_url.rstrip('/')}/responses",
                headers=headers,
                params=params,
            ) as response:
                if response.is_error:
                    # Do not include raw upstream bodies, which can echo credentials.
                    if response.status_code == 401:
                        if self._using_codex_oauth:
                            raise RuntimeError("Codex login expired; run `codex login` and retry")
                        raise RuntimeError(
                            "Image generation HTTP 401; check api_key_env and "
                            "http_headers for the configured compatible endpoint"
                        )
                    raise RuntimeError(
                        f"Codex image generation HTTP {response.status_code}; "
                        "the account/model may not support image generation or may have reached its limit"
                    )
                async for event, payload in _iter_sse_payloads(response):
                    event_type = payload.get("type") or event
                    if event_type == "response.output_item.done":
                        collect(payload.get("item") or {})
                    elif event_type == "response.completed":
                        final = payload.get("response") or {}
                        if final.get("status") not in {None, "completed"} or final.get("error"):
                            raise RuntimeError("Codex image generation did not complete successfully")
                        for item in final.get("output") or []:
                            collect(item)
                        completed = True
                    elif event_type in {"response.failed", "response.incomplete", "response.error", "error"}:
                        message = _response_error_message(payload) or event_type
                        for secret in secrets:
                            if secret:
                                message = message.replace(secret, "[REDACTED_SECRET]")
                        raise RuntimeError(f"Codex image generation failed: {message}")
        if not completed:
            raise RuntimeError("Codex image stream ended without a completed response")
        if not images:
            raise RuntimeError("Codex returned no image; the account/model may not support image generation")
        return list(images.values())

    async def _stream_via_httpx(
        self,
        params: dict[str, Any],
    ) -> AsyncGenerator[StreamChunk, None]:
        url = f"{self._base_url.rstrip('/')}/responses"
        active_calls: dict[str, dict[str, str]] = {}
        finalized_call_items: set[str] = set()
        saw_terminal_event = False

        from crabcode_core.api.network import http_options, request_timeout
        async with httpx.AsyncClient(timeout=request_timeout(self.config), **http_options(self.config)) as client:
            async with self._httpx_stream_with_retry(
                client,
                url=url,
                headers=self._raw_responses_headers(),
                params=params,
            ) as response:
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError:
                    body = (await response.aread()).decode("utf-8", "replace").strip()
                    error_body = body
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
                        error_body=error_body,
                        error_status_code=response.status_code,
                        error_request_id=response_request_id(response),
                        retryable=(
                            response.status_code in {408, 409, 429}
                            or response.status_code >= 500
                        ),
                        retry_after=(
                            float(response.headers["retry-after"])
                            if response.headers.get("retry-after", "").replace(".", "", 1).isdigit()
                            else None
                        ),
                    )
                    return

                content_type = response.headers.get("content-type", "").lower()
                if "event-stream" not in content_type and "json" in content_type:
                    body = (await response.aread()).decode("utf-8", "replace").strip()
                    try:
                        payload = json.loads(body) if body else {}
                    except json.JSONDecodeError:
                        payload = {}
                    error_message = _response_error_message(payload)
                    if error_message:
                        yield _responses_error_chunk(payload, error_message, response=response)
                    else:
                        yield StreamChunk(
                            type="error",
                            error_body=body,
                            error_status_code=response.status_code,
                            error_request_id=response_request_id(response),
                            error=(
                                "Responses endpoint returned JSON without a usable "
                                "response or error payload"
                            ),
                            retryable=False,
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
                        finalized_call_items.add(item_id)

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
                            elif item_id not in finalized_call_items:
                                call_id = str(item.get("call_id", "") or item_id)
                                name = str(item.get("name", ""))
                                yield StreamChunk(
                                    type="tool_use_start",
                                    tool_use_id=call_id,
                                    tool_name=name,
                                )
                                yield StreamChunk(
                                    type="tool_use_end",
                                    tool_use_id=call_id,
                                    tool_name=name,
                                    tool_input_json=str(item.get("arguments", "") or "{}"),
                                )
                            finalized_call_items.add(item_id)
                        yield StreamChunk(
                            type="response_item_done",
                            item_id=str(item.get("id", "")),
                            item_type=str(item.get("type", "")),
                        )

                    elif event_type == "response.reasoning_summary_text.delta":
                        yield StreamChunk(
                            type="thinking",
                            text=safe_utf8_str(str(payload.get("delta", ""))),
                        )

                    elif event_type == "response.completed":
                        saw_terminal_event = True
                        response_payload = payload.get("response")
                        usage_payload = (
                            response_payload.get("usage", {})
                            if isinstance(response_payload, dict)
                            else {}
                        ) or {}
                        usage = normalize_openai_usage(usage_payload)
                        yield StreamChunk(
                            type="message_stop",
                            stop_reason="end_turn",
                            usage=usage,
                        )

                    elif event_type == "response.failed":
                        saw_terminal_event = True
                        response_payload = payload.get("response")
                        error_payload = (
                            response_payload.get("error", {})
                            if isinstance(response_payload, dict)
                            else payload.get("error", {})
                        ) or {}
                        error_message = (
                            _response_error_message({"error": error_payload})
                            or "Response failed"
                        )
                        yield _responses_error_chunk(payload, error_message, response=response)

                    elif event_type == "response.incomplete":
                        saw_terminal_event = True
                        yield StreamChunk(
                            type="error",
                            error_body=payload,
                            error_status_code=response.status_code,
                            error_request_id=response_request_id(response),
                            error=_incomplete_response_message(
                                payload.get("response") or {}
                            ),
                            retryable=True,
                        )

                    elif event_type in {"response.error", "error"}:
                        saw_terminal_event = True
                        yield _responses_error_chunk(payload, "Unknown error", response=response)

                    elif _response_error_message(payload):
                        # Some proxies wrap an error in a non-standard SSE
                        # event name. Preserve the payload instead of
                        # reducing it to a generic empty-stream failure.
                        saw_terminal_event = True
                        yield _responses_error_chunk(payload, "Unknown error", response=response)

                if not saw_terminal_event:
                    yield StreamChunk(
                        type="error",
                        error="Responses stream ended without a terminal event",
                    )

    def _request_params(
        self, messages: list[Message], system: list[str],
        tools: list[dict[str, Any]], config: ModelConfig,
    ) -> dict[str, Any]:
        """Share input serialization between generation and server counting."""
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

        if extra_body:
            params["extra_body"] = extra_body
        return params

    async def count_input_tokens(
        self, messages: list[Message], system: list[str],
        tools: list[dict[str, Any]], config: ModelConfig,
    ) -> int | None:
        # The OAuth backend is not the public Responses API. Do not forward
        # its credentials to api.openai.com to obtain a count.
        if self._using_codex_oauth:
            return None
        payload = self._request_params(messages, system, tools, config)
        payload.update(payload.pop("extra_body", {}))
        supported = {
            "model", "input", "instructions", "tools", "tool_choice",
            "parallel_tool_calls", "previous_response_id", "conversation",
            "reasoning", "text", "truncation", "personality",
        }
        payload = {key: value for key, value in payload.items() if key in supported}
        headers = {key: value for key, value in self._raw_responses_headers().items()
                   if key.lower() != "accept"}
        headers["Accept"] = "application/json"
        return await self._count_tokens_http(
            f"{self._base_url.rstrip('/')}/responses/input_tokens", payload, headers,
        )

    async def stream_message(
        self,
        messages: list[Message],
        system: list[str],
        tools: list[dict[str, Any]],
        config: ModelConfig,
    ) -> AsyncGenerator[StreamChunk, None]:
        sdk_params = self._request_params(messages, system, tools, config)
        raw_params = dict(sdk_params)
        raw_params.update(raw_params.pop("extra_body", {}))

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
        finalized_call_items: set[str] = set()
        emitted_stream_event = False
        saw_sdk_terminal_event = False
        try:
            stream = await self._create_sdk_response_stream(sdk_params)
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
                    finalized_call_items.add(item_id)

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
                        elif item_id not in finalized_call_items:
                            call_id = getattr(item, "call_id", "") or item_id
                            name = getattr(item, "name", "") or ""
                            arguments = getattr(item, "arguments", "") or "{}"
                            yield StreamChunk(
                                type="tool_use_start",
                                tool_use_id=call_id,
                                tool_name=name,
                            )
                            yield StreamChunk(
                                type="tool_use_end",
                                tool_use_id=call_id,
                                tool_name=name,
                                tool_input_json=arguments,
                            )
                        finalized_call_items.add(item_id)
                    yield StreamChunk(
                        type="response_item_done",
                        item_id=str(getattr(item, "id", "") or ""),
                        item_type=str(item_type),
                    )

                # Reasoning summary text delta — treat as thinking
                elif event_type == "response.reasoning_summary_text.delta":
                    yield StreamChunk(type="thinking", text=safe_utf8_str(event.delta))

                # Response completed
                elif event_type == "response.completed":
                    saw_sdk_terminal_event = True
                    usage = {}
                    response = event.response
                    if hasattr(response, "usage") and response.usage:
                        usage = normalize_openai_usage(response.usage)
                    yield StreamChunk(
                        type="message_stop",
                        stop_reason="end_turn",
                        usage=usage,
                        model_id=str(getattr(response, "model", "") or ""),
                    )

                # Response failed or incomplete
                elif event_type == "response.failed":
                    saw_sdk_terminal_event = True
                    error_msg = ""
                    error_code = ""
                    if hasattr(event, "response") and hasattr(event.response, "error"):
                        err = event.response.error
                        if err:
                            error_msg = getattr(err, "message", str(err))
                            error_code = str(getattr(err, "code", "") or "")
                    yield _responses_error_chunk(
                        {"error": {"message": error_msg, "code": error_code}},
                        "Response failed",
                        error_body=event,
                    )

                elif event_type == "response.incomplete":
                    saw_sdk_terminal_event = True
                    yield StreamChunk(
                        type="error",
                        error_body=event,
                        error=_incomplete_response_message(
                            getattr(event, "response", None)
                        ),
                        retryable=True,
                    )

                # Error event
                elif event_type == "response.error":
                    saw_sdk_terminal_event = True
                    error_msg = ""
                    error_code = ""
                    if hasattr(event, "error"):
                        err = event.error
                        error_msg = getattr(err, "message", str(err)) if err else ""
                        error_code = str(getattr(err, "code", "") or "") if err else ""
                    yield _responses_error_chunk(
                        {"error": {"message": error_msg, "code": error_code}},
                        "Unknown error",
                        error_body=event,
                    )
            if not saw_sdk_terminal_event:
                yield StreamChunk(
                    type="error",
                    error="Responses stream ended without a terminal event",
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
        # Legacy local estimate; count_input_tokens() uses the server when available
        total = sum(len(s) for s in system)
        for msg in messages:
            if isinstance(msg.content, str):
                total += len(msg.content)
            else:
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        total += len(block.text)
        return total // 4
