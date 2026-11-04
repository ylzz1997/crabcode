"""Automatic continuation defaults, validation, layering, and live session reload."""

import asyncio
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from crabcode_core.config.manager import ConfigManager
from crabcode_core.events import CoreSession
from crabcode_core.api.base import StreamChunk
from crabcode_core.types.config import ApiConfig, CrabCodeSettings
from crabcode_core.types.event import StreamRetryEvent, TurnCompleteEvent
from crabcode_gateway.routes import config as routes


@pytest.mark.parametrize("value", [0, -1, 11, 1.5, True, "2", None])
def test_invalid_auto_continue_attempts_are_rejected(value):
    with pytest.raises(ValidationError):
        CrabCodeSettings(auto_continue_max_attempts=value)


def test_auto_continue_settings_round_trip_layers_and_reload(tmp_path, monkeypatch):
    home, project = tmp_path / "home", tmp_path / "project"
    home.mkdir()
    project.mkdir()
    monkeypatch.setenv("CRABCODE_HOME", str(home))
    monkeypatch.setattr(routes, "_resolve_model_settings_cwd", lambda request, cwd: cwd or str(project))
    project_file = project / ".crabcode" / "settings.json"
    project_file.parent.mkdir()
    project_file.write_text(json.dumps({"env": {"PRESERVE": "yes"}}))
    session = CoreSession(cwd=str(project))
    app = FastAPI()
    app.state.sessions = {session.session_id: session}
    app.include_router(routes.router)
    with TestClient(app) as client:
        initial = client.get("/config/runtime-settings").json()
        assert initial["auto_continue_enabled"] is True
        assert initial["auto_continue_max_attempts"] == 2

        def save(source="projectSettings", **changes):
            return client.post("/config/runtime-settings", json={
                "action": "set_auto_continue", "source": source, **changes,
            })

        assert save("userSettings", auto_continue_enabled=False, auto_continue_max_attempts=4).status_code == 200
        assert session.settings.auto_continue_enabled is False
        assert session.settings.auto_continue_max_attempts == 4
        saved = save(auto_continue_enabled=True)
        assert saved.status_code == 200
        assert saved.json()["auto_continue_max_attempts"] == 4
        assert session.settings.auto_continue_enabled is True
        assert save(auto_continue_max_attempts=1).status_code == 200
        persisted = ConfigManager(cwd=str(project)).load()
        assert persisted.auto_continue_enabled is True
        assert persisted.auto_continue_max_attempts == 1
        assert json.loads(project_file.read_text())["env"] == {"PRESERVE": "yes"}

        before = project_file.read_text()
        for changes in ({}, {"auto_continue_max_attempts": 0}, {"auto_continue_max_attempts": 11},
                        {"auto_continue_max_attempts": 1.5}, {"auto_continue_enabled": None}):
            assert save(**changes).status_code == 422
        assert project_file.read_text() == before


def test_auto_continue_reload_preserves_explicit_session_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("CRABCODE_HOME", str(tmp_path))
    session = CoreSession(cwd=str(tmp_path), settings=CrabCodeSettings(
        auto_continue_enabled=False, auto_continue_max_attempts=5,
    ))
    session.reload_auto_continue_settings()
    assert session.settings.auto_continue_enabled is False
    assert session.settings.auto_continue_max_attempts == 5


def test_core_session_applies_file_settings_and_reloads_on_the_next_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("CRABCODE_HOME", str(tmp_path / "home"))
    config_path = tmp_path / ".crabcode" / "settings.json"
    config_path.parent.mkdir()
    config_path.write_text(json.dumps({"auto_continue_max_attempts": 1}))

    class EmptyAdapter:
        config = ApiConfig(model="test", thinking_enabled=False, max_retries=0)
        requests = 0

        async def stream_message(self, *args, **kwargs):
            self.requests += 1
            yield StreamChunk(type="message_stop", stop_reason="end_turn")

    session = CoreSession(cwd=str(tmp_path))
    session._initialized = True
    session._api_adapter = adapter = EmptyAdapter()

    async def collect():
        try:
            first = [event async for event in session.send_message("first task")]
            assert adapter.requests == 2
            assert len([event for event in first if isinstance(event, StreamRetryEvent)]) == 1
            assert len([event for event in first if isinstance(event, TurnCompleteEvent)]) == 1
            assert first[-1].turn_details["request_count"] == 2
            config_path.write_text(json.dumps({"auto_continue_enabled": False, "auto_continue_max_attempts": 4}))
            second = [event async for event in session.send_message("second task")]
            assert adapter.requests == 3
            assert not any(isinstance(event, StreamRetryEvent) for event in second)
            assert session.settings.auto_continue_enabled is False
            assert session.settings.auto_continue_max_attempts == 4
            assert [message.text_content for message in session.messages] == ["first task", "second task"]
        finally:
            await session.close()

    asyncio.run(collect())
