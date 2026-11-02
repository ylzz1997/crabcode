"""Configuration must survive SDK serialization and HTTP transport selection."""

import asyncio
from copy import deepcopy
from io import StringIO
import json
import ssl
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from crabcode_core.api.base import ModelConfig, StreamChunk
from crabcode_core.api.registry import create_adapter
from crabcode_core.types.config import ApiConfig, CrabCodeSettings
from crabcode_core.types.message import create_user_message


@pytest.fixture
def wire(monkeypatch, tmp_path):
    monkeypatch.setenv("CRABCODE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FORWARD_TEST_KEY", "test-key")
    requests, outcomes = [], []
    client_class = httpx.AsyncClient

    def handle(request):
        requests.append(request)
        outcome = outcomes.pop(0) if outcomes else 200
        if isinstance(outcome, Exception):
            raise outcome
        if outcome == "broken_stream":
            class BrokenStream(httpx.AsyncByteStream):
                async def __aiter__(self):
                    yield b': connected\n\n'
                    raise httpx.ReadError("stream interrupted")
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=BrokenStream())
        if outcome != 200:
            return httpx.Response(outcome, json={"error": {"type": "api_error", "message": "failed"}})
        path = request.url.path
        if path.endswith("count_tokens"):
            return httpx.Response(200, json={"input_tokens": 12})
        if path.endswith("/responses"):
            frames = [{"type": "response.completed", "response": {
                "id": "r1", "object": "response", "status": "completed", "output": [],
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            }}]
        elif path.endswith("/messages"):
            frames = [
                {"type": "message_start", "message": {"id": "m1", "type": "message", "role": "assistant",
                 "model": "test", "content": [], "usage": {"input_tokens": 1, "output_tokens": 0}}},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}},
                {"type": "message_stop"},
            ]
        else:
            frames = [{"choices": [{"index": 0, "delta": {"content": "OK"}, "finish_reason": "stop"}]}]
        text = "".join(f"event: {frame.get('type', 'message')}\ndata: {json.dumps(frame)}\n\n" for frame in frames)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=text)

    class Client(client_class):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    monkeypatch.setattr("crabcode_core.api.network.sdk_http_client", lambda config, sdk: Client())
    monkeypatch.setattr("crabcode_core.api.network.asyncio.sleep", AsyncMock())
    return SimpleNamespace(requests=requests, outcomes=outcomes)


def api_config(provider, **overrides):
    return ApiConfig(**{
        "provider": provider, "model": "test-model", "api_key_env": "FORWARD_TEST_KEY",
        "base_url": "https://provider.invalid/v1", "network_mode": "direct",
        "http_headers": {"x-route": "route-one"}, "timeout": 91,
        "request_max_retries": 0, **overrides,
    })


def collect(config, call=None, *, oauth=False):
    async def run():
        adapter = create_adapter(config)
        if oauth:
            adapter._using_codex_oauth = True
        try:
            return [chunk async for chunk in adapter.stream_message(
                [create_user_message("hello")], ["system"], [], call or ModelConfig(model=config.model),
            )]
        finally:
            await adapter.client.close()
    return asyncio.run(run())


@pytest.mark.parametrize("provider,transport,oauth", [
    ("openai", "auto", False), ("anthropic", "sdk", False),
    ("anthropic", "httpx", False), ("codex", "auto", False), ("codex", "auto", True),
])
@pytest.mark.parametrize("timeout,expected", [(None, 91), (7, 7)])
def test_per_call_timeout_and_headers_reach_transport(wire, provider, transport, oauth, timeout, expected):
    config = api_config(provider, anthropic_stream_transport=transport)
    chunks = collect(config, ModelConfig(model="test", timeout=timeout), oauth=oauth)
    assert not any(chunk.type == "error" for chunk in chunks)
    request = wire.requests[-1]
    assert request.extensions["timeout"]["read"] == expected
    assert request.extensions["timeout"]["connect"] == min(expected, 10)
    assert request.headers["x-route"] == "route-one"
    assert "timeout" not in json.loads(request.content)


def test_openai_stops_cache_and_custom_parameters(wire):
    config = api_config("openai", prompt_cache_key="cache-one", prompt_cache_retention="24h",
                        extra_body={"seed": 42, "top_p": 0.5, "custom": {"value": "保留"}})
    original = deepcopy(config.extra_body)
    collect(config, ModelConfig(model="test", temperature=0, max_tokens=432, stop_sequences=["END"]))
    body = json.loads(wire.requests[-1].content)
    assert body["stop"] == ["END"]
    assert body["temperature"] == 0 and body["max_tokens"] == 432
    assert body["prompt_cache_key"] == "cache-one" and body["prompt_cache_retention"] == "24h"
    assert all(body[key] == value for key, value in original.items())
    assert "extra_body" not in body and config.extra_body == original


@pytest.mark.parametrize("transport", ["sdk", "httpx"])
def test_anthropic_stops_effort_and_custom_body(wire, transport):
    config = api_config("anthropic", anthropic_stream_transport=transport, reasoning_effort="high",
                        extra_body={"metadata": {"user_id": "test"}, "max_tokens": 888,
                                    "custom": {"value": "保留"}})
    original = deepcopy(config.extra_body)
    collect(config, ModelConfig(model="test", temperature=0, stop_sequences=["END"], thinking_budget=2048))
    body = json.loads(wire.requests[-1].content)
    assert body["output_config"] == {"effort": "high"}
    assert body["thinking"] == {"type": "enabled", "budget_tokens": 2048}
    assert body["stop_sequences"] == ["END"] and body["temperature"] == 0
    assert all(body[key] == value for key, value in original.items())
    assert "extra_body" not in body and config.extra_body == original


def test_anthropic_count_uses_overridden_input_without_generation_fields(wire):
    config = api_config("anthropic", extra_body={"system": "override", "max_tokens": 888,
                                               "metadata": {"user_id": "test"}})
    async def run():
        adapter = create_adapter(config)
        try:
            return await adapter.count_input_tokens([], ["original"], [], ModelConfig(stop_sequences=["END"]))
        finally:
            await adapter.client.close()
    assert asyncio.run(run()) == 12
    body = json.loads(wire.requests[-1].content)
    assert body["system"] == "override"
    assert not ({"stop_sequences", "max_tokens", "extra_body", "metadata"} & body.keys())


@pytest.mark.parametrize("provider", ["anthropic", "codex"])
def test_explicit_none_disables_reasoning_on_wire(wire, provider):
    collect(api_config(provider, reasoning_effort="high"), ModelConfig(model="test", reasoning_effort="none"))
    body = json.loads(wire.requests[-1].content)
    if provider == "codex":
        assert body["reasoning"] == {"effort": "none"}
    else:
        assert body["thinking"] == {"type": "disabled"}
        assert "output_config" not in body


def test_responses_summary_opt_out_preserves_explicit_effort(wire):
    collect(api_config("codex", reasoning_effort="low"), ModelConfig(model="test", thinking_enabled=False))
    assert json.loads(wire.requests[-1].content)["reasoning"] == {"effort": "low"}


def test_responses_unsupported_stop_is_reported_instead_of_silently_lost(wire):
    with pytest.raises(ValueError, match="does not support stop_sequences"):
        collect(api_config("codex"), ModelConfig(model="test", stop_sequences=["END"]))
    assert not wire.requests


def test_anthropic_minimal_maps_to_its_lowest_supported_effort(wire):
    collect(api_config("anthropic"), ModelConfig(model="test", reasoning_effort="minimal"))
    assert json.loads(wire.requests[-1].content)["output_config"] == {"effort": "low"}


def test_gemini_preserves_camelcase_summary_opt_out(wire):
    collect(api_config("gemini", model="gemini-4-argon", extra_body={
        "extra_body": {"google": {"thinking_config": {"includeThoughts": False}}},
    }))
    assert json.loads(wire.requests[-1].content)["extra_body"]["google"]["thinking_config"] == {"includeThoughts": False}


def test_azure_dedicated_fields_control_actual_url_and_deployment(wire, monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://env.invalid")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "env-deployment")
    monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "env-version")
    collect(api_config("azure", azure_endpoint="https://azure.invalid", azure_deployment="configured",
                       azure_api_version="2024-10-21"))
    request = wire.requests[-1]
    assert request.url.host == "azure.invalid"
    assert request.url.path == "/openai/deployments/configured/chat/completions"
    assert request.url.params["api-version"] == "2024-10-21"
    assert json.loads(request.content)["model"] == "configured"


def test_azure_environment_fallback_is_preserved(wire, monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://env.invalid")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "env-deployment")
    monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
    collect(api_config("azure"))
    assert wire.requests[-1].url.host == "env.invalid"
    assert "/deployments/env-deployment/" in wire.requests[-1].url.path


@pytest.mark.parametrize("enabled,effort,expected", [(True, "high", "high"), (False, None, "none")])
def test_ollama_preserves_thinking_and_does_not_require_or_leak_openai_key(wire, monkeypatch, enabled, effort, expected):
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-key")
    collect(api_config("ollama", api_key_env=None), ModelConfig(model="local", thinking_enabled=enabled, reasoning_effort=effort))
    assert wire.requests[-1].headers["authorization"] == "Bearer ollama"
    assert json.loads(wire.requests[-1].content)["reasoning_effort"] == expected


@pytest.mark.parametrize("effort", [None, "low"])
def test_gemini_25_budget_is_forwarded_without_conflicting_with_effort(wire, effort):
    collect(api_config("gemini", model="gemini-2.5-flash"),
            ModelConfig(model="gemini-2.5-flash", thinking_budget=1234, reasoning_effort=effort))
    body = json.loads(wire.requests[-1].content)
    thinking = body["extra_body"]["google"]["thinking_config"]
    if effort is None:
        assert thinking["thinking_budget"] == 1234 and "reasoning_effort" not in body
    else:
        assert "thinking_budget" not in thinking and body["reasoning_effort"] == effort


@pytest.mark.parametrize("provider,transport", [("openai", "auto"), ("anthropic", "sdk"), ("anthropic", "httpx")])
@pytest.mark.parametrize("failure,retries,expected", [(500, 2, 3), (500, 0, 1), (429, 2, 1), (400, 2, 1), ("connect", 2, 3), ("certificate", 2, 1)])
def test_request_retry_budget_is_used_only_for_connection_and_server_failures(wire, provider, transport, failure, retries, expected):
    if failure == "connect":
        failure = httpx.ConnectError("offline")
    elif failure == "certificate":
        failure = httpx.ConnectError("TLS failed")
        failure.__cause__ = ssl.SSLCertVerificationError("CERTIFICATE_VERIFY_FAILED")
    wire.outcomes[:] = [failure] * 4
    config = api_config(provider, anthropic_stream_transport=transport, request_max_retries=retries)
    try:
        chunks = collect(config)
    except Exception as exc:
        assert type(exc).__name__ in {"InternalServerError", "RateLimitError", "BadRequestError", "APIConnectionError", "ConnectError"}
    else:
        assert any(chunk.type == "error" for chunk in chunks)
    assert len(wire.requests) == expected


@pytest.mark.parametrize("provider,transport", [("openai", "auto"), ("anthropic", "sdk"), ("anthropic", "httpx")])
def test_request_retries_do_not_replay_a_started_stream(wire, provider, transport):
    wire.outcomes[:] = ["broken_stream"]
    with pytest.raises(httpx.ReadError, match="stream interrupted"):
        collect(api_config(provider, anthropic_stream_transport=transport, request_max_retries=2))
    assert len(wire.requests) == 1


@pytest.mark.parametrize("provider,sdk_name,credential", [
    ("bedrock", "AsyncAnthropicBedrock", "api_key"), ("vertex", "AsyncAnthropicVertex", "access_token"),
])
def test_cloud_adapters_forward_configuration_and_keep_authenticated_sdk_transport(wire, monkeypatch, provider, sdk_name, credential):
    captured = {}
    def client(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(close=AsyncMock())
    monkeypatch.setattr(f"crabcode_core.api.anthropic_adapter.anthropic.{sdk_name}", client)
    config = api_config(provider, anthropic_stream_transport="httpx")
    adapter = create_adapter(config)
    assert captured["base_url"] == config.base_url
    assert captured["default_headers"] == config.http_headers
    assert captured[credential] == "test-key"
    async def sdk(params):
        captured["params"] = params
        yield StreamChunk(type="message_stop")
    monkeypatch.setattr(adapter, "_stream_message_sdk", sdk)
    async def run():
        return [chunk async for chunk in adapter.stream_message([], [], [], ModelConfig(timeout=7))]
    assert asyncio.run(run())[-1].type == "message_stop"
    assert captured["params"]["timeout"].read == 7


def test_cli_project_reload_preserves_all_api_fields(wire, monkeypatch, tmp_path):
    from typer.testing import CliRunner
    from crabcode_cli.app import app
    from crabcode_core.events import CoreSession
    settings = CrabCodeSettings(api=api_config("azure", thinking_budget=2048, timeout=43, max_tokens=999,
        prompt_cache_key="cache", prompt_cache_retention="24h", prompt_caching="disabled",
        request_max_retries=7, azure_endpoint="https://azure.invalid", azure_deployment="dep",
        azure_api_version="2024-10-21", extra_body={"seed": 42}))
    captured = {}
    async def run_pipe(prompt, **kwargs):
        captured.update(kwargs)
    monkeypatch.setattr("crabcode_core.config.manager.ConfigManager.load", lambda self: settings)
    monkeypatch.setattr("crabcode_cli.pipe.run_pipe", run_pipe)
    monkeypatch.setattr("crabcode_cli.app.sys.stdin", StringIO(""))
    monkeypatch.setattr("crabcode_cli.app.configure_logging", lambda *args: None)
    monkeypatch.setattr("crabcode_cli.app.os._exit", lambda code: None)
    result = CliRunner().invoke(app, ["main", "--cwd", str(tmp_path), "-p", "hello"])
    assert result.exit_code == 0, result.output
    session = CoreSession(cwd=str(tmp_path), settings=captured["settings"])
    assert session._merge_project_settings(settings).api.model_dump() == settings.api.model_dump()


def test_gateway_model_mutation_preserves_all_config_fields(wire, monkeypatch, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from crabcode_core.config.manager import ConfigManager
    from crabcode_gateway.routes import config as routes
    app = FastAPI()
    app.state.sessions = {}
    app.include_router(routes.router)
    monkeypatch.setattr(routes, "_resolve_model_settings_cwd", lambda request, cwd: cwd)
    config = api_config("azure", thinking_budget=2048, timeout=43, max_tokens=999,
        prompt_cache_key="cache", prompt_cache_retention="24h", prompt_caching="disabled",
        request_max_retries=7, azure_endpoint="https://azure.invalid", azure_deployment="dep",
        azure_api_version="2024-10-21", extra_body={"seed": 42})
    with TestClient(app) as client:
        response = client.post("/config/model-settings", json={"action": "upsert_model", "name": "test",
            "source": "projectSettings", "cwd": str(tmp_path), "config": config.model_dump()})
    assert response.status_code == 200, response.text
    assert ConfigManager(cwd=str(tmp_path)).load().get_api_config("test").model_dump() == config.model_dump()
