"""Prompt templates and prompts appended to user input."""

import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from crabcode_core.events import CoreSession
from crabcode_core.prompts.library import (
    enabled_user_append_texts,
    resolve_prompt_profile,
)
from crabcode_core.prompts.system import get_system_prompt
from crabcode_core.prompts.profile import resolve_compact_prompt
from crabcode_core.prompts.templates import DEFAULT_COMPACT_PROMPT
from crabcode_core.query.loop import _append_user_prompts
from crabcode_core.types.config import (
    CrabCodeSettings,
    PromptTemplateConfig,
    UserAppendPromptConfig,
)
from crabcode_core.types.message import create_tool_result_message, create_user_message
from crabcode_gateway.routes import config as routes


def test_blank_sections_keep_builtin_defaults_and_templates_override_legacy_profiles():
    settings = CrabCodeSettings(
        prompt_profile={"intro": "legacy identity"},
        prompt_templates=[
            PromptTemplateConfig(
                id="care",
                name="客服",
                sections={"intro": "custom identity", "doing_tasks": "  ", "compact_prompt": "custom checkpoint rules"},
            )
        ],
        active_prompt_template="care",
    )

    profile = resolve_prompt_profile(settings)
    text = "\n".join(get_system_prompt([], "test", profile=profile))

    assert profile is not None
    assert profile.intro == "custom identity"
    assert profile.doing_tasks is None
    assert "custom identity" in text
    assert "legacy identity" not in text
    assert "# Doing tasks" in text
    assert resolve_compact_prompt(profile) == "custom checkpoint rules"
    assert "custom checkpoint rules" not in text

    fallback = CrabCodeSettings(prompt_profile={"intro": "legacy identity"})
    assert resolve_prompt_profile(fallback).intro == "legacy identity"
    assert resolve_prompt_profile(CrabCodeSettings()) is None


def test_only_checked_prompts_are_appended_and_the_transcript_stays_unchanged():
    settings = CrabCodeSettings(user_append_prompts=[
        UserAppendPromptConfig(id="p", text="one", enabled=False),
        UserAppendPromptConfig(id="p", text="two", enabled=True),
    ])
    assert enabled_user_append_texts(settings) == ["two"]

    original = create_user_message("hello")
    tool = create_tool_result_message("tool-1", "result")
    copied = _append_user_prompts([original, tool], ["用中文回答"])

    assert original.text_content == "hello"
    assert copied[1] is tool
    assert copied[0].text_content == "hello\n\n<user-rules>\n用中文回答\n</user-rules>"


def test_prompt_settings_round_trip(tmp_path, monkeypatch):
    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(routes, "_resolve_model_settings_cwd", lambda request, cwd: cwd or str(project))

    app = FastAPI()
    session = CoreSession(cwd=str(project))
    app.state.sessions = {session.session_id: session}
    app.include_router(routes.router)
    with TestClient(app) as client:
        initial = client.get("/config/prompt-settings", params={"cwd": str(project)})
        assert initial.status_code == 200
        body = initial.json()
        assert body["active_template_id"] is None
        assert body["templates"] == []
        assert body["sections"][0]["key"] == "prefix"
        assert any(section["key"] == "extra" for section in body["sections"])
        compact = next(section for section in body["sections"] if section["key"] == "compact_prompt")
        assert compact["label"] == "上下文压缩提示词"
        assert compact["default_text"] == DEFAULT_COMPACT_PROMPT

        saved = client.post("/config/prompt-settings", json={
            "action": "save_template",
            "source": "userSettings",
            "cwd": str(project),
            "template_name": "客服",
            "sections": {"intro": "custom identity", "doing_tasks": "  ", "compact_prompt": "保留目标和下一步", "unknown": "skip"},
        })
        assert saved.status_code == 200
        saved_body = saved.json()
        assert saved_body["active_template_id"]
        assert saved_body["templates"][0]["name"] == "客服"
        expected_sections = {"intro": "custom identity", "compact_prompt": "保留目标和下一步"}
        assert saved_body["templates"][0]["sections"] == expected_sections
        assert resolve_compact_prompt(session._prompt_profile) == "保留目标和下一步"

        stored = json.loads((home / ".crabcode" / "settings.json").read_text(encoding="utf-8"))
        assert stored["active_prompt_template"] == saved_body["active_template_id"]
        assert stored["prompt_templates"][0]["sections"] == expected_sections

        cleared = client.post("/config/prompt-settings", json={
            "action": "save_template", "source": "userSettings", "cwd": str(project),
            "template_id": saved_body["active_template_id"], "template_name": "客服",
            "sections": {"intro": "custom identity", "compact_prompt": "  \n"},
        })
        assert cleared.status_code == 200
        assert cleared.json()["templates"][0]["sections"] == {"intro": "custom identity"}
        assert resolve_compact_prompt(session._prompt_profile) == DEFAULT_COMPACT_PROMPT

        default = client.post("/config/prompt-settings", json={
            "action": "set_active_template",
            "source": "userSettings",
            "cwd": str(project),
            "template_id": None,
        })
        assert default.status_code == 200
        assert default.json()["active_template_id"] is None
        assert resolve_compact_prompt(session._prompt_profile) == DEFAULT_COMPACT_PROMPT

        added = client.post("/config/prompt-settings", json={
            "action": "add_user_prompt",
            "source": "userSettings",
            "cwd": str(project),
            "prompt_text": "用中文回答",
        })
        assert added.status_code == 200
        prompt = added.json()["user_prompts"][0]
        assert prompt["text"] == "用中文回答"
        assert prompt["enabled"] is True

        checked = client.post("/config/prompt-settings", json={
            "action": "set_user_prompt_enabled",
            "source": "userSettings",
            "cwd": str(project),
            "prompt_id": prompt["id"],
            "enabled": True,
        })
        assert checked.status_code == 200
        assert checked.json()["user_prompts"][0]["enabled"] is True

        rejected = client.post("/config/prompt-settings", json={
            "action": "save_template",
            "source": "projectSettings",
            "cwd": str(project),
            "template_name": "默认",
            "sections": {},
        })
        assert rejected.status_code == 400

        template_id = saved_body["templates"][0]["id"]
        prompt_id = checked.json()["user_prompts"][0]["id"]
        project_settings = project / ".crabcode" / "settings.json"
        project_settings.parent.mkdir(parents=True, exist_ok=True)
        project_settings.write_text(json.dumps({
            "prompt_templates": [{"id": template_id, "name": "客服", "sections": {"intro": "project copy"}}],
            "user_append_prompts": [{"id": prompt_id, "text": "用中文回答", "enabled": True}],
            "active_prompt_template": template_id,
        }), encoding="utf-8")

        deleted_template = client.post("/config/prompt-settings", json={
            "action": "delete_template",
            "source": "projectSettings",
            "cwd": str(project),
            "template_id": template_id,
        })
        assert deleted_template.status_code == 200
        assert deleted_template.json()["templates"] == []
        assert deleted_template.json()["active_template_id"] is None
        user_stored = json.loads((home / ".crabcode" / "settings.json").read_text(encoding="utf-8"))
        project_stored = json.loads(project_settings.read_text(encoding="utf-8"))
        assert "prompt_templates" not in user_stored
        assert "prompt_templates" not in project_stored
        assert project_stored["active_prompt_template"] is None

        deleted_prompt = client.post("/config/prompt-settings", json={
            "action": "delete_user_prompt",
            "source": "localSettings",
            "cwd": str(project),
            "prompt_id": prompt_id,
        })
        assert deleted_prompt.status_code == 200
        assert deleted_prompt.json()["user_prompts"] == []
        user_stored = json.loads((home / ".crabcode" / "settings.json").read_text(encoding="utf-8"))
        project_stored = json.loads(project_settings.read_text(encoding="utf-8"))
        assert "user_append_prompts" not in user_stored
        assert "user_append_prompts" not in project_stored
