"""Compaction settings persist by layer and reach live sessions and status."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.requests import Request

from crabcode_core.config.manager import ConfigManager
from crabcode_core.events import CoreSession
from crabcode_core.types.config import ApiConfig, CrabCodeSettings
from crabcode_gateway.routes import config as routes
from crabcode_gateway.routes.session import session_status


@pytest.mark.parametrize("value", [-1, 1.5, True, "1000", None])
def test_invalid_buffer_is_rejected(value):
    with pytest.raises(ValidationError):
        CrabCodeSettings(compact_buffer_tokens=value)


def test_compaction_settings_round_trip_and_live_reload(tmp_path, monkeypatch):
    home, project = tmp_path / "home", tmp_path / "project"
    home.mkdir()
    project.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
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
        assert initial["compact_buffer_tokens"] == 20_000
        assert initial["auto_compact_enabled"] is True
        assert initial["max_context_length"] is None

        def save(source="projectSettings", **changes):
            return client.post("/config/runtime-settings", json={
                "action": "set_compaction", "source": source, **changes,
            })

        saved = save("userSettings", compact_buffer_tokens=30_000, max_context_length=70_000)
        assert saved.status_code == 200
        assert session.settings.compact_buffer_tokens == 30_000
        saved = save(compact_buffer_tokens=0, auto_compact_enabled=False)
        assert saved.status_code == 200
        assert saved.json()["max_context_length"] == 70_000
        assert session.settings.compact_buffer_tokens == 0
        assert session.settings.auto_compact_enabled is False
        cleared = save(max_context_length=None)
        assert cleared.status_code == 200
        assert cleared.json()["max_context_length"] is None
        assert session.settings.max_context_length is None
        persisted = ConfigManager(cwd=str(project)).load()
        assert persisted.compact_buffer_tokens == 0
        assert persisted.auto_compact_enabled is False
        assert persisted.max_context_length is None
        assert json.loads(project_file.read_text())["env"] == {"PRESERVE": "yes"}

        before = project_file.read_text()
        for changes in ({}, {"compact_buffer_tokens": -1}, {"compact_buffer_tokens": 1.5},
                        {"max_context_length": 0}, {"compact_buffer_tokens": None}):
            assert save(**changes).status_code == 422
        assert project_file.read_text() == before


def test_reload_preserves_explicit_overrides(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    session = CoreSession(cwd=str(tmp_path), settings=CrabCodeSettings(compact_buffer_tokens=5000))
    session.reload_compaction_settings()
    assert session.settings.compact_buffer_tokens == 5000


def test_status_reports_effective_threshold_without_shrinking_window():
    settings = CrabCodeSettings(
        api=ApiConfig(model="example", context_window=100_000, max_tokens=40_000),
        compact_buffer_tokens=30_000, max_context_length=50_000,
    )
    session = SimpleNamespace(session_id="a", cwd=".", settings=settings, messages=[],
                              tools=[], _initialized=True, _current_model_name=None)
    state = SimpleNamespace(sessions={"a": session}, default_session_id="a")
    request = Request({"type": "http", "app": SimpleNamespace(state=state)})
    status = asyncio.run(session_status(request, "a"))
    assert status.context_window_tokens == 100_000
    assert status.compact_buffer_tokens == 30_000
    assert status.compact_input_limit == 50_000
    settings.max_context_length = None
    status = asyncio.run(session_status(request, "a"))
    assert status.compact_input_limit == 60_000
