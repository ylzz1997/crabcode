"""Network routing and recovery without provider requests or VPN changes."""
import asyncio
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from crabcode_core.api.base import APIAdapter, StreamChunk
from crabcode_core.api.network import http_options, network_error_message, request_timeout
from crabcode_core.query.loop import _is_recoverable_api_exception
from crabcode_core.query.loop import QueryParams, query_loop
from crabcode_core.types.config import ApiConfig
from crabcode_core.types.event import ErrorEvent, StreamRetryEvent
from crabcode_core.types.message import AssistantMessage, create_user_message
from crabcode_core.types.tool import ToolContext
from test_stream_reconnect import ScriptedAdapter, run


def test_default_is_bounded_and_proxy_policy_is_explicit():
    config = ApiConfig()
    assert config.max_retries == 3
    assert not config.unbounded_connection_retries
    assert http_options(config) == {}
    assert http_options(ApiConfig(network_mode="direct")) == {"trust_env": False}
    assert http_options(ApiConfig(network_mode="proxy", proxy_url="http://127.0.0.1:7890")) == {
        "trust_env": False, "proxy": "http://127.0.0.1:7890",
    }
    assert request_timeout(config).connect == 10
    with pytest.raises(ValueError):
        http_options(ApiConfig(network_mode="proxy"))
    with pytest.raises(ValueError):
        ApiConfig(proxy_url="http://user:secret@localhost:7890")


def test_direct_ignores_dead_environment_proxy_without_automatic_fallback(monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # An allocated but non-listening socket cannot serve as a proxy.
    import socket
    with socket.socket() as dead:
        dead.bind(("127.0.0.1", 0))
        proxy = f"http://127.0.0.1:{dead.getsockname()[1]}"
        monkeypatch.setenv("HTTP_PROXY", proxy)
        monkeypatch.setenv("NO_PROXY", "")
        monkeypatch.delenv("http_proxy", raising=False)
        # Environment names are case-insensitive on Windows: set again last.
        monkeypatch.setenv("HTTP_PROXY", proxy)
        async def check():
            url = f"http://127.0.0.1:{server.server_port}"
            async with httpx.AsyncClient(**http_options(ApiConfig(network_mode="direct"))) as client:
                assert (await client.get(url)).text == "OK"
            for config in (ApiConfig(), ApiConfig(network_mode="proxy", proxy_url=proxy)):
                async with httpx.AsyncClient(timeout=1, **http_options(config)) as client:
                    with pytest.raises((httpx.ConnectError, httpx.ConnectTimeout)):
                        await client.get(url)
        try:
            asyncio.run(check())
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


def test_reset_replaces_and_closes_client_with_same_policy():
    replacement = object()
    old = SimpleNamespace(with_options=Mock(return_value=replacement), close=AsyncMock())
    adapter = SimpleNamespace(client=old, config=ApiConfig(network_mode="direct"))
    async def check():
        await APIAdapter.reset_network_client(adapter)
        assert adapter.client is replacement
        old.close.assert_awaited_once()
        assert old.with_options.call_args.kwargs["max_retries"] == 0
        await old.with_options.call_args.kwargs["http_client"].aclose()
    asyncio.run(check())


def test_certificate_failure_is_terminal_and_diagnostics_hide_secrets():
    error = httpx.ConnectError("https://user:secret@example.com")
    error.__cause__ = ssl.SSLCertVerificationError("CERTIFICATE_VERIFY_FAILED")
    assert not _is_recoverable_api_exception(error)
    message = network_error_message(error, ApiConfig())
    assert "证书" in message and "secret" not in message


def test_connection_retries_exhaust_and_reset_before_each_retry():
    adapter = ScriptedAdapter([[httpx.ConnectError("offline")]], max_retries=3)
    adapter.reset_network_client = AsyncMock()
    events, _ = run(adapter)
    assert len(adapter.requests) == 4
    assert sum(isinstance(e, StreamRetryEvent) for e in events) == 3
    assert any(isinstance(e, ErrorEvent) for e in events)
    assert adapter.reset_network_client.await_count == 3


def test_partial_response_without_checkpoints_is_replayed():
    adapter = ScriptedAdapter([
        [StreamChunk(type="text", text="partial"), httpx.ReadError("offline")],
        [StreamChunk(type="text", text="recovered"), StreamChunk(type="message_stop")],
    ])
    adapter.emits_response_item_events = False
    events, messages = run(adapter)
    assert len(adapter.requests) == 2
    assert any(isinstance(e, StreamRetryEvent) for e in events)
    assert not any(isinstance(e, ErrorEvent) for e in events)
    durable = [message.text_content for message in messages if isinstance(message, AssistantMessage)]
    assert durable == ["recovered"]


def test_waiting_for_network_can_be_cancelled_without_another_request():
    adapter = ScriptedAdapter([[httpx.ConnectError("offline")]], unbounded=True)
    async def check():
        messages = [create_user_message("hello")]
        params = QueryParams(messages=messages, system_prompt=[], user_context={}, system_context={},
            tools=[], tool_context=ToolContext(messages=messages), api_adapter=adapter,
            api_config=adapter.config, auto_compact_enabled=False)
        waiting = asyncio.Event()
        async def consume():
            async for event in query_loop(params):
                if isinstance(event, StreamRetryEvent):
                    waiting.set()
        task = asyncio.create_task(consume())
        await asyncio.wait_for(waiting.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
        assert len(adapter.requests) == 1
    asyncio.run(check())
