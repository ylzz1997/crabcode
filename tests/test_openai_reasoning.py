"""Reasoning controls must reach the serialized provider request, not just UI state."""

import asyncio
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from types import SimpleNamespace

from fastapi import FastAPI
import httpx
import pytest

from crabcode_core.api.base import ModelConfig
from crabcode_core.api.registry import create_adapter
from crabcode_core.config.manager import ConfigManager
from crabcode_core.events import CoreSession
from crabcode_core.query.loop import QueryParams, query_loop
from crabcode_core.types.config import ApiConfig
from crabcode_core.types.event import ErrorEvent, ThinkingEvent, TurnCompleteEvent
from crabcode_core.types.message import create_user_message
from crabcode_core.types.tool import ToolContext


@pytest.fixture
def provider(tmp_path, monkeypatch):
    monkeypatch.setenv("CRABCODE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("REASONING_TEST_KEY", "test-key")
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            frames = [
                {"choices": [{"index": 0, "delta": {"reasoning_content": "Checked the calculation."}}]},
                {"choices": [{"index": 0, "delta": {"content": "42"}, "finish_reason": "stop"}]},
            ]
            data = "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames)
            data += "data: [DONE]\n\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data.encode())))
            self.end_headers()
            self.wfile.write(data.encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    config = ApiConfig(provider="openai", model="gpt-test", network_mode="direct",
                       base_url=f"http://127.0.0.1:{server.server_port}/v1",
                       api_key_env="REASONING_TEST_KEY", context_window=100000)
    try:
        yield config, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def stream(config, **overrides):
    async def run():
        adapter = create_adapter(config)
        try:
            return [chunk async for chunk in adapter.stream_message(
                [create_user_message("Calculate")], [], [],
                ModelConfig(model=config.model, **overrides),
            )]
        finally:
            await adapter.client.close()
    return asyncio.run(run())


@pytest.mark.parametrize("profile,override,expected", [
    (None, None, None), ("low", None, "low"), ("low", "high", "high"),
    ("high", "none", "none"), (None, "minimal", "minimal"),
    (None, "xhigh", "xhigh"), (None, "max", "max"),
])
def test_openai_effort_is_on_the_wire(provider, profile, override, expected):
    config, requests = provider
    config.reasoning_effort = profile
    stream(config, reasoning_effort=override)
    if expected is None:
        assert "reasoning_effort" not in requests[-1]
    else:
        assert requests[-1]["reasoning_effort"] == expected
    assert "extra_body" not in requests[-1]  # No Google fields for other providers.


@pytest.mark.parametrize("provider_name,fmt", [
    ("openai", None), ("gemini", None), ("router", "openai"), ("router", "gemini"),
])
@pytest.mark.parametrize("effort,expected", [(None, None), ("low", "low"), ("xhigh", "high"), ("max", "high")])
def test_gemini_routes_forward_effort_and_return_summaries(provider, provider_name, fmt, effort, expected):
    config, requests = provider
    config.provider, config.format, config.model = provider_name, fmt, "gemini-4-argon"
    chunks = stream(config, reasoning_effort=effort)
    body = requests[-1]
    assert body.get("reasoning_effort") == expected
    assert body["extra_body"]["google"]["thinking_config"] == {"include_thoughts": True}
    assert [(chunk.type, chunk.text) for chunk in chunks if chunk.type == "thinking"] == [
        ("thinking", "Checked the calculation."),
    ]
    assert config.thinking_enabled is True
    assert config.extra_body == {}


@pytest.mark.parametrize("native_key,value", [("thinking_level", "low"), ("thinking_budget", 1024)])
def test_explicit_gemini_settings_do_not_conflict_or_get_mutated(provider, native_key, value):
    config, requests = provider
    config.model = "gemini-4-argon"
    config.extra_body = {"extra_body": {"google": {
        "thinking_config": {native_key: value, "include_thoughts": False},
        "safety_settings": [],
    }}, "custom": {"keep": True}}
    original = deepcopy(config.extra_body)
    stream(config, reasoning_effort="high")
    assert "reasoning_effort" not in requests[-1]
    assert requests[-1]["extra_body"] == original["extra_body"]
    assert requests[-1]["custom"] == {"keep": True}
    assert config.extra_body == original


def test_gemini_summary_switch_is_separate_from_effort(provider):
    config, requests = provider
    config.model = "gemini-4-argon"
    stream(config, reasoning_effort="low", thinking_enabled=False)
    assert requests[-1]["reasoning_effort"] == "low"
    assert requests[-1]["extra_body"]["google"]["thinking_config"]["include_thoughts"] is False


@pytest.mark.parametrize("model", ["gemini-1.5-pro", "gemini-2.0-flash"])
def test_legacy_gemini_does_not_receive_unsupported_thinking_config(provider, model):
    config, requests = provider
    config.provider, config.model = "gemini", model
    stream(config)
    assert "extra_body" not in requests[-1]
    assert "reasoning_effort" not in requests[-1]


def test_explicit_extra_body_effort_keeps_existing_override_semantics(provider):
    config, requests = provider
    config.extra_body = {"reasoning_effort": "low", "custom": True}
    stream(config, reasoning_effort="high")
    assert requests[-1]["reasoning_effort"] == "low"
    assert requests[-1]["custom"] is True


@pytest.mark.parametrize("client", ["cli", "vscode_rest", "desktop_ws", "new_session"])
def test_client_controls_reach_query_loop_and_provider(provider, tmp_path, client):
    from crabcode_cli.repl import _handle_command
    from crabcode_gateway.routes import config as config_routes, event as event_routes
    from crabcode_gateway.routes.session import _apply_new_session_controls
    from crabcode_gateway.schemas import NewSessionRequest

    config, requests = provider
    config.model = "gemini-4-argon"
    config.reasoning_effort = "medium"
    project = tmp_path / "project"
    settings_path = project / ".crabcode" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"default_model": "test", "models": {"test": config.model_dump()}}))
    session = CoreSession(cwd=str(project), settings=ConfigManager(str(project)).load())
    session.session_id = "reasoning-test"
    session._initialized = True  # Isolate tool startup; use the real session and query loop.
    session._current_model_name = "test"
    active = session.settings.get_api_config("test")
    app = FastAPI()
    app.state.sessions = {session.session_id: session}
    app.include_router(config_routes.router)
    ws = SimpleNamespace(app=app, scope={})

    async def run():
        adapter = create_adapter(active)
        session._api_adapter = adapter
        try:
            for selected, expected in [("xhigh", "high"), ("low", "low"), ("auto", "medium")]:
                if client == "cli":
                    assert await _handle_command(f"/effort {selected}", session, session.settings)
                elif client == "vscode_rest":
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as http:
                        result = await http.post("/config/reasoning-effort", json={"session_id": session.session_id, "effort": selected})
                        assert result.status_code == 200
                elif client == "desktop_ws":
                    await event_routes._handle_set_reasoning_effort(ws, {
                        "type": "set_reasoning_effort", "session_id": session.session_id, "effort": selected,
                    })
                else:
                    if selected == "auto":
                        continue  # New sessions omit effort to use their configured default.
                    _apply_new_session_controls(session, NewSessionRequest(reasoning_effort=selected))
                events = [event async for event in query_loop(QueryParams(
                    messages=[create_user_message("Calculate")], system_prompt=[], user_context={}, system_context={},
                    tools=[], tool_context=ToolContext(cwd=str(project)), api_adapter=adapter,
                    api_config=active, context_window=100000,
                ))]
                assert not any(isinstance(event, ErrorEvent) for event in events)
                assert any(isinstance(event, ThinkingEvent) for event in events)
                assert any(isinstance(event, TurnCompleteEvent) for event in events)
                assert requests[-1]["reasoning_effort"] == expected
        finally:
            await adapter.client.close()
    asyncio.run(run())
