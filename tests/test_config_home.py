"""Relocated global configuration must stay consistent across loaders and stores."""

import asyncio
import json
from pathlib import Path

import pytest

from crabcode_core.config.claudemd import discover_claude_md
from crabcode_core.config.manager import ConfigManager
from crabcode_core.logging_utils import get_log_path, get_logs_dir
from crabcode_core.mcp.config import load_mcp_configs
from crabcode_core.paths import get_config_home
from crabcode_core.prompts.context import get_user_context
from crabcode_core.schedule.store import ScheduleStore
from crabcode_core.session.meta_db import SessionMetaStore, reset_shared_connections
from crabcode_core.session.storage import SessionStorage, get_transcript_path
from crabcode_core.skills.loader import load_skills
from crabcode_core.tools.memory import MemoryTool, load_all_memories
from crabcode_core.types.config import LoggingSettings
from crabcode_core.types.message import create_user_message
from crabcode_core.types.tool import ToolContext
from crabcode_core.usage import UsageStore
from crabcode_gateway.document_engine import BABELDOC_VERSION, document_engine_root


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("CRABCODE_HOME", raising=False)
    yield home
    reset_shared_connections()


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.mark.parametrize("value", [None, "", " \t "])
def test_default_home_is_backwards_compatible(home, monkeypatch, value):
    if value is not None:
        monkeypatch.setenv("CRABCODE_HOME", value)
    assert get_config_home() == home / ".crabcode"
    assert not get_config_home().exists()


@pytest.mark.parametrize("value", ["~/custom settings", "~", "absolute"])
def test_custom_home_supports_tilde_spaces_and_nonexistent_directories(home, monkeypatch, value):
    expected = home if value == "~" else home / "custom settings"
    monkeypatch.setenv("CRABCODE_HOME", str(expected) if value == "absolute" else value)
    assert get_config_home() == expected
    assert not (home / "custom settings").exists()


@pytest.mark.parametrize("value", ["relative", "./config", "../config"])
def test_relative_home_is_rejected(home, monkeypatch, value):
    monkeypatch.setenv("CRABCODE_HOME", value)
    with pytest.raises(ValueError, match="CRABCODE_HOME must be an absolute"):
        ConfigManager().load()


def test_relocated_settings_preserve_precedence_and_write_destination(home, monkeypatch):
    custom = home / "custom settings"
    project = home / "project"
    monkeypatch.setenv("CRABCODE_HOME", str(custom))
    write(home / ".crabcode/settings.json", '{"api": {"model": "old-home"}}')
    layers = [
        (custom / "settings.json", "user"),
        (project / ".crabcode/settings.json", "project"),
        (project / ".crabcode/settings.local.json", "local"),
        (home / "flag.json", "flag"),
        (custom / "managed-settings.json", "policy"),
    ]
    for path, model in layers:
        write(path, json.dumps({"api": {"model": model}}))
    manager = ConfigManager(str(project), str(home / "flag.json"))
    for path, model in reversed(layers):
        assert manager.load().api.model == model
        path.unlink()

    assert manager.load().api.model is None
    manager.update_settings("userSettings", {"api": {"model": "saved"}})
    assert json.loads((custom / "settings.json").read_text())["api"]["model"] == "saved"
    assert json.loads((home / ".crabcode/settings.json").read_text())["api"]["model"] == "old-home"


def test_global_resources_use_custom_home_and_project_overrides_still_win(home, monkeypatch):
    custom = home / "custom"
    project = home / "project"
    project.mkdir()
    monkeypatch.setenv("CRABCODE_HOME", str(custom))
    write(home / ".crabcode/CLAUDE.md", "old home instructions")
    write(custom / "CLAUDE.md", "custom global instructions")
    write(project / "CLAUDE.md", "project instructions")
    write(home / ".crabcode/skills/old/SKILL.md", "old skill")
    write(custom / "skills/shared/SKILL.md", "global skill")
    write(custom / "skills/global-only/SKILL.md", "custom global skill")
    write(home / ".claude/skills/compat/SKILL.md", "compat skill")
    write(project / ".crabcode/skills/shared/SKILL.md", "project skill")
    write(home / ".crabcode/mcp_servers.json", '{"old": {"command": ["old-command"]}}')
    write(custom / "mcp_servers.json", '{"shared": {"command": ["global"]}, "global": {"command": ["custom"]}}')
    write(project / ".crabcode/mcp_servers.json", '{"shared": {"command": ["project"]}}')

    assert [item["content"] for item in discover_claude_md(str(project))] == [
        "custom global instructions", "project instructions",
    ]
    assert get_user_context(str(project))["claudeMd"] == (
        "custom global instructions\n\n---\n\nproject instructions"
    )
    skills = {skill.name: skill.content for skill in load_skills(str(project))}
    assert skills == {"shared": "project skill", "global-only": "custom global skill", "compat": "compat skill"}
    servers = load_mcp_configs(str(project))
    assert {name: config.command for name, config in servers.items()} == {
        "shared": ["project"], "global": ["custom"],
    }


def test_persisted_state_and_project_memory_are_isolated(home, monkeypatch):
    custom = home / "custom"
    project = home / "project"
    project.mkdir()
    monkeypatch.setenv("CRABCODE_HOME", str(custom))

    storage = SessionStorage(str(project), "relocated-session")
    storage.write_meta(model="test-model")
    storage.append_message(create_user_message("hello from custom home"))
    assert get_transcript_path(str(project), storage.session_id).is_relative_to(custom / "projects")
    assert SessionStorage(str(project), storage.session_id).load_messages()[0]["content"] == "hello from custom home"
    with_meta = SessionMetaStore()
    try:
        assert with_meta.list_by_cwd(str(project))[0]["id"] == storage.session_id
    finally:
        with_meta.close()
    assert (custom / "sessions.db").is_file()

    tool = MemoryTool()
    for scope in ("global", "project"):
        result = asyncio.run(tool.call({
            "action": "create", "scope": scope, "title": scope, "content": f"{scope} memory",
        }, ToolContext(cwd=str(project))))
        assert not result.is_error
    assert (custom / "memories.json").is_file()
    assert (project / ".crabcode/memories.json").is_file()
    assert {item["_scope"] for item in load_all_memories(str(project))} == {"global", "project"}

    UsageStore().mark_error()
    assert (custom / "usage-recording-error.txt").is_file()
    schedules = ScheduleStore()
    try:
        assert schedules.list_schedules() == []
    finally:
        schedules.close()
    assert (custom / "schedules.db").is_file()
    assert not (home / ".crabcode").exists()


def test_log_fallback_and_document_engine_use_custom_home(home, monkeypatch):
    custom = home / "custom"
    monkeypatch.setenv("CRABCODE_HOME", str(custom))
    monkeypatch.delenv("CRABCODE_DOCUMENT_ENGINE_HOME", raising=False)
    # A file in place of the project directory makes log creation fail on all OSes.
    blocked_project = home / "blocked"
    blocked_project.write_text("not a directory")
    assert get_logs_dir(str(blocked_project)) == custom / "logs"
    assert get_log_path(str(blocked_project), LoggingSettings(file="custom.log")) == custom / "logs/custom.log"
    assert document_engine_root() == custom / "engines/babeldoc" / BABELDOC_VERSION
    explicit_engine = home / "explicit-engine"
    monkeypatch.setenv("CRABCODE_DOCUMENT_ENGINE_HOME", str(explicit_engine))
    assert document_engine_root() == explicit_engine.resolve()
    assert not (home / ".crabcode").exists()
