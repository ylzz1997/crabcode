"""Mid-stream reconnect and completed-item checkpoint behavior."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from crabcode_core.api.anthropic_adapter import AnthropicAdapter
from crabcode_core.api.base import ModelConfig, StreamChunk
from crabcode_core.api.codex_adapter import CodexAdapter, _responses_error_chunk
from crabcode_core.api.openai_adapter import OpenAIAdapter
from crabcode_core.query.loop import QueryParams, query_loop
from crabcode_core.query.retry import ResponsesStreamRetryState, request_retry_backoff
from crabcode_core.types.config import ApiConfig
from crabcode_core.types.event import ErrorEvent, StreamRetryEvent, StreamTextEvent, TurnCompleteEvent
from crabcode_core.types.message import AssistantMessage, ToolResultBlock, create_user_message
from crabcode_core.types.tool import Tool, ToolContext, ToolResult
from crabcode_gateway.schemas import core_event_to_payload


class ScriptedAdapter:
    emits_response_item_events = True

    def __init__(self, responses, *, max_retries=2, unbounded=False):
        self.responses = responses
        self.requests = []
        self.config = ApiConfig(
            model="test",
            thinking_enabled=False,
            max_tokens=1000,
            max_retries=max_retries,
            unbounded_connection_retries=unbounded,
        )

    async def stream_message(self, messages, system, tools, config):
        self.requests.append([message.model_copy(deep=True) for message in messages])
        response = self.responses[min(len(self.requests) - 1, len(self.responses) - 1)]
        for item in response:
            if isinstance(item, BaseException):
                raise item
            yield item

    async def count_input_tokens(self, messages, system, tools, config):
        return None

    def try_switch_fallback_transport(self):
        return False


class CountingTool(Tool):
    name = "Count"
    description = "Count executions"
    input_schema = {"type": "object", "properties": {}}
    is_read_only = True

    def __init__(self):
        self.calls = 0

    async def call(self, tool_input, context):
        self.calls += 1
        return ToolResult(result_for_model=f"call {self.calls}")


def run(adapter, *, tools=None):
    messages = [create_user_message("hello")]
    params = QueryParams(
        messages=messages,
        system_prompt=[],
        user_context={},
        system_context={},
        tools=tools or [],
        tool_context=ToolContext(messages=messages),
        api_adapter=adapter,
        api_config=adapter.config,
        auto_compact_enabled=False,
    )

    async def collect():
        with patch("crabcode_core.query.loop.asyncio.sleep", new=AsyncMock()):
            return [event async for event in query_loop(params)]

    return asyncio.run(collect()), params.messages


def completed_text(text: str, item_id: str = "msg"):
    return [
        StreamChunk(type="text", text=text),
        StreamChunk(type="response_item_done", item_id=item_id, item_type="message"),
    ]


def test_incomplete_chunked_read_retries_after_partial_text():
    adapter = ScriptedAdapter([
        [
            StreamChunk(type="text", text="partial"),
            httpx.ReadError("peer closed connection without sending complete message body"),
        ],
        [*completed_text("recovered"), StreamChunk(type="message_stop")],
    ])

    events, messages = run(adapter)

    assert len(adapter.requests) == 2
    retry = next(event for event in events if isinstance(event, StreamRetryEvent))
    assert retry.message == "模型连接中断，正在重试 1/2"
    assert "ReadError" in retry.error
    assert retry.discarded_text_chars == len("partial")
    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert [event.text for event in events if isinstance(event, StreamTextEvent)] == [
        "partial",
        "recovered",
    ]
    durable = [message.text_content for message in messages if isinstance(message, AssistantMessage)]
    assert durable == ["recovered"]


def test_remote_protocol_error_exhausts_exact_stream_retry_budget():
    adapter = ScriptedAdapter([
        [
            StreamChunk(type="text", text="partial"),
            httpx.RemoteProtocolError("incomplete chunked read"),
        ]
    ])

    events, messages = run(adapter)

    retries = [event for event in events if isinstance(event, StreamRetryEvent)]
    errors = [event for event in events if isinstance(event, ErrorEvent)]
    assert len(adapter.requests) == 3
    assert [event.retry_count for event in retries] == [1, 2]
    assert len(errors) == 1
    assert "RemoteProtocolError" in errors[0].message
    assert not any(isinstance(message, AssistantMessage) for message in messages)


def test_completed_response_item_is_checkpointed_before_reconnect():
    adapter = ScriptedAdapter([
        [
            *completed_text("checkpoint", "msg-1"),
            StreamChunk(type="text", text="unfinished"),
            httpx.ReadError("incomplete chunked read"),
        ],
        [*completed_text("continued", "msg-2"), StreamChunk(type="message_stop")],
    ])

    events, messages = run(adapter)

    assert len(adapter.requests) == 2
    retry_request = adapter.requests[1]
    assert [
        message.text_content
        for message in retry_request
        if isinstance(message, AssistantMessage)
    ] == ["checkpoint"]
    assert [
        message.text_content for message in messages if isinstance(message, AssistantMessage)
    ] == ["checkpoint", "continued"]
    assert len([event for event in events if isinstance(event, StreamRetryEvent)]) == 1


def test_connection_failure_uses_unbounded_budget_even_when_stream_budget_is_zero():
    adapter = ScriptedAdapter([
        [httpx.ConnectError("offline")],
        [*completed_text("online"), StreamChunk(type="message_stop")],
    ], max_retries=0, unbounded=True)

    events, messages = run(adapter)

    retry = next(event for event in events if isinstance(event, StreamRetryEvent))
    assert "持续重连已开启" in retry.message
    assert retry.unbounded is True
    assert retry.delay_seconds == 5.0
    assert len(adapter.requests) == 2
    assert messages[-1].text_content == "online"


def test_idle_timeout_after_thinking_retries_without_item_checkpoints():
    class StallAfterThinking(ScriptedAdapter):
        emits_response_item_events = False

        def __init__(self):
            super().__init__([], max_retries=2)
            self.config.timeout = 1

        async def stream_message(self, messages, system, tools, config):
            self.requests.append([message.model_copy(deep=True) for message in messages])
            if len(self.requests) == 1:
                yield StreamChunk(type="thinking", text="still working")
                await asyncio.Event().wait()
            yield StreamChunk(type="text", text="resumed")
            yield StreamChunk(type="message_stop")

    adapter = StallAfterThinking()
    events, messages = run(adapter)

    assert len(adapter.requests) == 2
    retry = next(event for event in events if isinstance(event, StreamRetryEvent))
    assert "timed out after 1s" in retry.error
    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert [message.text_content for message in messages if isinstance(message, AssistantMessage)] == ["resumed"]


def test_closed_tool_call_without_item_events_runs_once_across_reconnect():
    tool = CountingTool()
    adapter = ScriptedAdapter([
        [
            StreamChunk(type="tool_use_start", tool_use_id="call-1", tool_name="Count"),
            StreamChunk(
                type="tool_use_end",
                tool_use_id="call-1",
                tool_name="Count",
                tool_input_json="{}",
            ),
            httpx.ReadError("incomplete chunked read"),
        ],
        [*completed_text("done", "msg-2"), StreamChunk(type="message_stop")],
    ])
    adapter.emits_response_item_events = False

    events, messages = run(adapter, tools=[tool])

    assert tool.calls == 1
    assert len(adapter.requests) == 2
    assert len([event for event in events if isinstance(event, StreamRetryEvent)]) == 1
    assert messages[-1].text_content == "done"


def test_completed_tool_call_runs_before_reconnect_and_is_not_replayed():
    tool = CountingTool()
    adapter = ScriptedAdapter([
        [
            StreamChunk(type="tool_use_start", tool_use_id="call-1", tool_name="Count"),
            StreamChunk(
                type="tool_use_end",
                tool_use_id="call-1",
                tool_name="Count",
                tool_input_json="{}",
            ),
            StreamChunk(
                type="response_item_done",
                item_id="fc-1",
                item_type="function_call",
            ),
            httpx.ReadError("incomplete chunked read"),
        ],
        [*completed_text("done", "msg-2"), StreamChunk(type="message_stop")],
    ])

    events, messages = run(adapter, tools=[tool])

    assert tool.calls == 1
    assert len(adapter.requests) == 2
    retry_request = adapter.requests[1]
    assert any(
        isinstance(block, ToolResultBlock) and block.tool_use_id == "call-1"
        for message in retry_request
        if isinstance(message.content, list)
        for block in message.content
    )
    assert len([event for event in events if isinstance(event, StreamRetryEvent)]) == 1
    assert messages[-1].text_content == "done"


def test_replayed_tool_call_id_reuses_result_without_side_effect():
    tool = CountingTool()
    tool_call = [
        StreamChunk(type="tool_use_start", tool_use_id="same-id", tool_name="Count"),
        StreamChunk(
            type="tool_use_end",
            tool_use_id="same-id",
            tool_name="Count",
            tool_input_json="{}",
        ),
        StreamChunk(type="response_item_done", item_id="fc", item_type="function_call"),
        StreamChunk(type="message_stop"),
    ]
    adapter = ScriptedAdapter([
        tool_call,
        tool_call,
        [*completed_text("done"), StreamChunk(type="message_stop")],
    ])

    _events, messages = run(adapter, tools=[tool])

    assert tool.calls == 1
    assert len(adapter.requests) == 3
    assert messages[-1].text_content == "done"


def test_retry_state_covers_backoff_unbounded_and_fallback():
    state = ResponsesStreamRetryState()
    with patch("crabcode_core.query.retry.random.uniform", return_value=1.0):
        first = state.schedule(error="drop", max_retries=2)
        second = state.schedule(error="drop", max_retries=2)
    assert first is not None and first.delay_seconds == 0.2
    assert second is not None and second.delay_seconds == 0.4

    fallback_calls = 0

    def fallback():
        nonlocal fallback_calls
        fallback_calls += 1
        return True

    switched = state.schedule(
        error="drop",
        max_retries=2,
        try_transport_fallback=fallback,
    )
    assert switched is not None and switched.transport_fallback
    assert fallback_calls == 1

    connection_state = ResponsesStreamRetryState()
    delays = [
        connection_state.schedule(
            error="offline",
            max_retries=0,
            connection_failed=True,
            unbounded_connection_retries=True,
        ).delay_seconds
        for _ in range(6)
    ]
    assert delays == [5.0, 10.0, 20.0, 40.0, 60.0, 60.0]


def test_request_layer_retries_transport_and_5xx_but_not_429():
    async def exercise(statuses):
        calls = []

        async def handler(request):
            calls.append(request)
            status = statuses[min(len(calls) - 1, len(statuses) - 1)]
            if isinstance(status, BaseException):
                raise status
            return httpx.Response(status, request=request)

        adapter = object.__new__(CodexAdapter)
        adapter.config = ApiConfig(
            model="test",
            thinking_enabled=False,
            request_max_retries=4,
        )
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            with patch(
                "crabcode_core.api.codex_adapter.asyncio.sleep",
                new=AsyncMock(),
            ):
                response = await adapter._send_httpx_stream_request(
                    client,
                    url="https://example.test/responses",
                    headers={},
                    params={"model": "test"},
                )
            status_code = response.status_code
            await response.aclose()
        return len(calls), status_code

    calls, status = asyncio.run(exercise([500, 502, 200]))
    assert (calls, status) == (3, 200)

    request = httpx.Request("POST", "https://example.test/responses")
    calls, status = asyncio.run(exercise([
        httpx.ConnectError("offline", request=request),
        200,
    ]))
    assert (calls, status) == (2, 200)

    calls, status = asyncio.run(exercise([429, 200]))
    assert (calls, status) == (1, 429)


def test_retry_delays_and_response_error_semantics():
    with patch("crabcode_core.query.retry.random.uniform", return_value=1.0):
        assert request_retry_backoff(1) == 0.2
        assert request_retry_backoff(2) == 0.4

    rate_limit = _responses_error_chunk(
        {
            "error": {
                "code": "rate_limit_exceeded",
                "message": "Please try again in 750ms",
            }
        },
        "rate limited",
    )
    assert rate_limit.retryable is True
    assert rate_limit.retry_after == 0.75

    overloaded = _responses_error_chunk(
        {"error": {"code": "server_is_overloaded", "message": "busy"}},
        "busy",
    )
    assert overloaded.retryable is False


def test_stream_retry_wire_payload_is_non_terminal():
    payload = core_event_to_payload(StreamRetryEvent(
        message="Reconnecting... 1/5",
        error="drop",
        retry_count=1,
        max_retries=5,
        delay_seconds=0.2,
    ))
    assert payload.type == "stream_retry"
    assert payload.retry_count == 1
    assert payload.error == "drop"


def anthropic_text_events(text="continue working", *, stop_reason="end_turn"):
    return [
        {"type": "message_start", "message": {"usage": {"input_tokens": 9000}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": stop_reason}, "usage": {"output_tokens": 20}},
        {"type": "message_stop"},
    ]


def anthropic_tool_events():
    return [
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "tool_use", "id": "call-1", "name": "Count", "input": {}}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "input_json_delta", "partial_json": "{}"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
        {"type": "message_stop"},
    ]


def scripted_anthropic(monkeypatch, responses, transport):
    """Exercise the real parsers without network calls or credentials."""
    adapter = object.__new__(AnthropicAdapter)
    adapter.config = ApiConfig(
        model="test", base_url="https://provider.invalid", thinking_enabled=False,
        anthropic_stream_transport=transport, max_retries=2,
        unbounded_connection_retries=True,
    )
    adapter._api_key = None
    adapter.count_input_tokens = AsyncMock(return_value=None)
    requests = []

    def next_events(params):
        requests.append(params)
        return responses[min(len(requests) - 1, len(responses) - 1)]

    if transport == "httpx":
        client_class = httpx.AsyncClient

        def handle(request):
            events = next_events(json.loads(request.content))
            body = "".join("data: " + json.dumps(event) + "\n\n" for event in events)
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

        monkeypatch.setattr(
            "crabcode_core.api.anthropic_adapter.httpx.AsyncClient",
            lambda **kwargs: client_class(transport=httpx.MockTransport(handle), **kwargs),
        )
    else:
        class SDKStream:
            def __init__(self, **params):
                self.events = next_events(params)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def __aiter__(self):
                for event in self.events:
                    yield json.loads(json.dumps(event), object_hook=lambda value: SimpleNamespace(**value))

        adapter.client = SimpleNamespace(messages=SimpleNamespace(stream=SDKStream))
    return adapter, requests


@pytest.mark.parametrize("transport", ["httpx", "sdk"])
@pytest.mark.parametrize("event_count", [0, 3, 4, 5, 6])
def test_anthropic_requires_terminal_event_on_both_transports(monkeypatch, transport, event_count):
    adapter, _ = scripted_anthropic(monkeypatch, [anthropic_text_events()[:event_count]], transport)

    async def collect():
        return [chunk async for chunk in adapter.stream_message([], [], [], ModelConfig(model="test"))]

    chunks = asyncio.run(collect())
    if event_count == 6:
        assert chunks[-1].type == "message_stop"
        assert not any(chunk.type == "error" for chunk in chunks)
    else:
        assert chunks[-1].type == "error"
        assert chunks[-1].retryable is True
        assert chunks[-1].connection_failed is False
        assert "before message_stop" in chunks[-1].error


@pytest.mark.parametrize("transport", ["httpx", "sdk"])
def test_anthropic_silent_eof_recovers_from_completed_text(monkeypatch, transport):
    adapter, requests = scripted_anthropic(monkeypatch, [
        anthropic_text_events("checkpoint")[:-1], anthropic_text_events("finished"),
    ], transport)
    events, messages = run(adapter)
    assert len(requests) == 2
    assert len([event for event in events if isinstance(event, StreamRetryEvent)]) == 1
    assert [message.text_content for message in messages if isinstance(message, AssistantMessage)] == [
        "checkpoint", "finished",
    ]
    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert events[-1].reason == "end_turn"


def test_anthropic_silent_eof_exhausts_bounded_budget_even_with_connection_retries(monkeypatch):
    adapter, requests = scripted_anthropic(monkeypatch, [anthropic_text_events()[:3]], "httpx")
    events, messages = run(adapter)
    assert len(requests) == 3
    assert len([event for event in events if isinstance(event, StreamRetryEvent)]) == 2
    assert len([event for event in events if isinstance(event, ErrorEvent)]) == 1
    assert not any(isinstance(event, TurnCompleteEvent) for event in events)
    assert not any(isinstance(message, AssistantMessage) for message in messages)


def test_anthropic_eof_after_tool_call_does_not_repeat_its_effect(monkeypatch):
    adapter, requests = scripted_anthropic(monkeypatch, [
        anthropic_tool_events()[:-1], anthropic_tool_events(), anthropic_text_events("done"),
    ], "httpx")
    tool = CountingTool()
    events, messages = run(adapter, tools=[tool])
    assert tool.calls == 1
    assert len(requests) == 3
    assert messages[-1].text_content == "done"
    assert not any(isinstance(event, ErrorEvent) for event in events)


@pytest.mark.parametrize("transport", ["httpx", "sdk"])
def test_anthropic_missing_declared_tool_is_retried(monkeypatch, transport):
    adapter, requests = scripted_anthropic(monkeypatch, [
        anthropic_text_events(stop_reason="tool_use"),
        anthropic_tool_events(), anthropic_text_events("done"),
    ], transport)
    tool = CountingTool()
    events, _ = run(adapter, tools=[tool])
    assert tool.calls == 1
    assert len(requests) == 3
    assert any(isinstance(event, StreamRetryEvent) for event in events)


def test_anthropic_terminal_with_unfinished_tool_never_executes_partial_input(monkeypatch):
    adapter, _ = scripted_anthropic(monkeypatch, [
        [*anthropic_tool_events()[:2], {"type": "message_stop"}],
        anthropic_text_events("recovered"),
    ], "httpx")
    tool = CountingTool()
    events, _ = run(adapter, tools=[tool])
    assert tool.calls == 0
    assert any(isinstance(event, StreamRetryEvent) for event in events)


def test_anthropic_explicit_error_is_not_followed_by_an_eof_error(monkeypatch):
    adapter, _ = scripted_anthropic(monkeypatch, [[
        {"type": "error", "error": {"message": "invalid request"}},
    ]], "httpx")

    async def collect():
        return [chunk async for chunk in adapter.stream_message([], [], [], ModelConfig(model="test"))]

    chunks = asyncio.run(collect())
    assert len(chunks) == 1
    assert chunks[0].type == "error"
    assert chunks[0].error == "invalid request"


def test_explicit_end_turn_is_not_replayed_based_on_text_and_is_logged(monkeypatch, caplog):
    adapter, requests = scripted_anthropic(monkeypatch, [anthropic_text_events("继续往上翻")], "httpx")
    events, _ = run(adapter)
    assert len(requests) == 1
    assert events[-1].reason == "end_turn"
    assert "stop_reason='end_turn'" in caplog.text
    assert "terminal_received=True" in caplog.text
    assert "tool_calls=0" in caplog.text
    assert "继续往上翻" not in caplog.text


def test_output_limit_is_not_reported_as_success(monkeypatch):
    adapter, requests = scripted_anthropic(monkeypatch, [anthropic_text_events(stop_reason="max_tokens")], "httpx")
    events, messages = run(adapter)
    assert len(requests) == 1
    assert events[-1].reason == "max_tokens"
    assert any(isinstance(event, ErrorEvent) and event.error_type == "output_limit" for event in events)
    assert messages[-1].text_content == "continue working"


def test_output_limit_without_visible_output_is_also_reported(monkeypatch):
    events = anthropic_text_events(stop_reason="max_tokens")
    adapter, requests = scripted_anthropic(monkeypatch, [[events[0], *events[-2:]]], "httpx")
    result, _ = run(adapter)
    assert len(requests) == 1
    assert result[-1].reason == "max_tokens"
    assert any(isinstance(event, ErrorEvent) and event.error_type == "output_limit" for event in result)


@pytest.mark.parametrize("output_kind", ["text", "thinking", "tool"])
def test_openai_output_limit_preserves_reason_and_never_executes_partial_tools(monkeypatch, output_kind):
    from openai.types.chat import ChatCompletionChunk

    delta = {
        "text": {"content": "unfinished reply"},
        "thinking": {"reasoning_content": "unfinished thought"},
        "tool": {"tool_calls": [{
            "index": 0, "id": "call-1", "type": "function",
            "function": {"name": "Count", "arguments": '{"partial":'},
        }]},
    }[output_kind]
    requests = []

    async def create(**kwargs):
        requests.append(kwargs)

        async def stream():
            for choices, usage in [
                ([{"index": 0, "delta": delta, "finish_reason": None}], None),
                ([{"index": 0, "delta": {}, "finish_reason": "length"}], None),
                ([], {"prompt_tokens": 100, "completion_tokens": 1000, "total_tokens": 1100}),
            ]:
                yield ChatCompletionChunk(
                    id="chunk", created=0, model="test", object="chat.completion.chunk",
                    choices=choices, usage=usage,
                )
        return stream()

    monkeypatch.setattr(OpenAIAdapter, "_create_client", lambda *_: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
    ))
    adapter = OpenAIAdapter(ApiConfig(model="test", max_tokens=1000, max_retries=0))
    tool = CountingTool()
    events, messages = run(adapter, tools=[tool])
    assert len(requests) == 1
    assert tool.calls == 0
    assert events[-1].reason == "length"
    assert events[-1].usage["output_tokens"] == 1000
    errors = [event for event in events if isinstance(event, ErrorEvent)]
    assert len(errors) == 1
    assert errors[0].error_type == "output_limit"
    assert core_event_to_payload(errors[0]).model_dump()["error_type"] == "output_limit"
    if output_kind == "text":
        assert messages[-1].text_content == "unfinished reply"
    else:
        assert not any(isinstance(message, AssistantMessage) for message in messages)


def test_retry_closes_failed_stream_before_starting_next_request():
    class ClosingAdapter(ScriptedAdapter):
        closed = 0

        async def stream_message(self, *args, **kwargs):
            assert self.closed == len(self.requests)
            try:
                async for chunk in super().stream_message(*args, **kwargs):
                    yield chunk
            finally:
                self.closed += 1

    adapter = ClosingAdapter([
        [StreamChunk(type="error", error="incomplete response", retryable=True)],
        [*completed_text("done"), StreamChunk(type="message_stop")],
    ])
    events, _ = run(adapter)
    assert adapter.closed == 2
    assert not any(isinstance(event, ErrorEvent) for event in events)
