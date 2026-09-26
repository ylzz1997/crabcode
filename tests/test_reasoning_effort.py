"""Session reasoning effort can return to the configured default."""

import json
from pathlib import Path
from types import SimpleNamespace

from crabcode_core.config.manager import ConfigManager
from crabcode_core.events import CoreSession


def _isolate_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return project


def test_auto_restores_the_model_profile_effort(tmp_path, monkeypatch):
    project = _isolate_home(tmp_path, monkeypatch)
    settings_dir = project / ".crabcode"
    settings_dir.mkdir()
    (settings_dir / "settings.json").write_text(json.dumps({
        "default_model": "fast",
        "models": {
            "fast": {
                "provider": "openai",
                "model": "gpt-test",
                "reasoning_effort": "low",
            },
        },
    }), encoding="utf-8")

    session = CoreSession(cwd=str(project), settings=ConfigManager(cwd=str(project)).load())
    session._current_model_name = "fast"
    adapter_config = session.settings.get_api_config("fast").model_copy(deep=True)
    session._api_adapter = SimpleNamespace(config=adapter_config)

    assert session.set_reasoning_effort("high")
    assert session.reasoning_effort == "high"
    assert adapter_config.reasoning_effort == "high"

    assert session.set_reasoning_effort("auto")
    assert session._reasoning_effort_override is None
    assert session.reasoning_effort == "low"
    assert adapter_config.reasoning_effort == "low"


def test_auto_clears_an_unset_profile_back_to_provider_default(tmp_path, monkeypatch):
    project = _isolate_home(tmp_path, monkeypatch)
    session = CoreSession(cwd=str(project))
    adapter_config = session.settings.api.model_copy(deep=True)
    session._api_adapter = SimpleNamespace(config=adapter_config)

    assert session.set_reasoning_effort("max")
    assert session.reasoning_effort == "max"

    assert session.set_reasoning_effort(" AUTO ")
    assert session.reasoning_effort is None
    assert adapter_config.reasoning_effort is None
    assert session.set_reasoning_effort("ultra") is False
