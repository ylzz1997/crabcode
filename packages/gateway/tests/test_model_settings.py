from __future__ import annotations

import json
from pathlib import Path

from crabcode_gateway.routes.config import _model_settings_from_files


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_model_settings_reports_raw_inheritance_sources_and_redacts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    monkeypatch.setenv("HOME", str(home))

    _write_json(
        home / ".crabcode" / "settings.json",
        {
            "default_model": "gpt-5.6",
            "groups": {
                "sky-router": {
                    "provider": "codex",
                    "base_url": "https://router.example.com/v1",
                    "reasoning_effort": "high",
                    "api_key_env": "SKY_API_KEY",
                    "http_headers": {
                        "authorization": "Bearer should-not-leak",
                        "originator": "crab-desktop",
                    },
                }
            },
            "models": {
                "gpt-5.6": {"group": "sky-router", "model": "gpt-5.6-sol"},
            },
        },
    )
    _write_json(
        project / ".crabcode" / "settings.json",
        {
            "models": {
                "gpt-5.6": {
                    "reasoning_effort": "low",
                    "extra_body": {"secret_token": "should-not-leak"},
                },
            }
        },
    )

    response = _model_settings_from_files(str(project))

    assert response.cwd == str(project)
    assert response.default_model == "gpt-5.6"
    assert response.sources == [
        str(home / ".crabcode" / "settings.json"),
        str(project / ".crabcode" / "settings.json"),
    ]
    assert response.groups["sky-router"]["api_key_env"] == "SKY_API_KEY"
    assert response.groups["sky-router"]["http_headers"] == {
        "authorization": "[redacted]",
        "originator": "crab-desktop",
    }

    model = response.models[0]
    assert model.name == "gpt-5.6"
    assert model.group == "sky-router"
    assert model.is_default is True
    assert model.configured["reasoning_effort"] == "low"
    assert model.configured["extra_body"] == {"secret_token": "[redacted]"}
    assert model.effective["provider"] == "codex"
    assert model.effective["base_url"] == "https://router.example.com/v1"
    assert model.effective["reasoning_effort"] == "low"
    assert model.overridden_fields == ["model", "reasoning_effort", "extra_body"]
    assert model.sources == response.sources


def test_model_settings_warns_about_unknown_group(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _write_json(
        home / ".crabcode" / "settings.json",
        {"models": {"orphan": {"group": "missing", "model": "test-model"}}},
    )

    response = _model_settings_from_files(str(project))

    assert response.models[0].effective["model"] == "test-model"
    assert response.warnings == ["模型“orphan”引用了不存在的配置组“missing”"]
