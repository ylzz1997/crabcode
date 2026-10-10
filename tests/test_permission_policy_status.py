"""Clients inspect the policy actually loaded by a session, including overrides."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from crabcode_core.events import CoreSession
from crabcode_core.permissions.manager import PermissionManager
from crabcode_core.types.config import CrabCodeSettings, PermissionsSettings
from crabcode_gateway.routes.config import _runtime_settings_from_files
from crabcode_gateway.routes.session import session_status


@pytest.mark.parametrize(("permissions", "expected"), [
    ({}, "default"),
    ({"default_mode": "ask"}, "default"),
    ({"default_mode": "ai_review"}, "aiReview"),
    ({"default_mode": "run_everything"}, "bypassPermissions"),
    ({"run_everything": True, "default_mode": "ask"}, "bypassPermissions"),
])
def test_configured_mode_matches_permission_manager(permissions, expected):
    manager = PermissionManager(PermissionsSettings(**permissions))
    assert manager.describe_policy()["configured_mode"] == expected
    assert manager.describe_policy()["effective_mode"] == manager.mode.value


def test_session_status_distinguishes_loaded_policy_override_and_plan(tmp_path):
    session = CoreSession(cwd=str(tmp_path), settings=CrabCodeSettings(permissions={
        "default_mode": "run_everything",
        "deny": [{"tool": "Bash", "command": "rm *"}],
        "allow": [{"tool": "Read", "path": "/work/*"}],
        "ask": [{"tool": "Write"}],
    }), tools=[])
    session.session_id = "test-permissions"
    state = SimpleNamespace(sessions={session.session_id: session})
    request = Request({"type": "http", "app": SimpleNamespace(state=state)})

    def status():
        return asyncio.run(session_status(request, session.session_id))

    # No initialized manager means no fabricated claim about the active policy.
    assert status().permission_policy is None
    session._permission_manager = PermissionManager(session.settings.permissions)
    session._permission_manager.add_allow_rule("private-runtime-command")
    policy = status().permission_policy
    assert policy.configured_mode == policy.effective_mode == "bypassPermissions"
    assert policy.deny[0].command == "rm *"
    assert policy.allow[0].path == "/work/*"
    assert policy.ask[0].tool == "Write"
    assert policy.runtime_allow_count == 1
    assert "private-runtime-command" not in policy.model_dump_json()

    session.set_client_permission_mode("ask")
    assert status().permission_policy.effective_mode == "default"
    assert status().permission_policy.configured_mode == "bypassPermissions"
    session.switch_mode("plan")
    assert status().permission_policy.effective_mode == "plan"
    session.set_client_permission_mode("default")
    assert status().permission_policy.effective_mode == "plan"
    session.switch_mode("agent")
    assert status().permission_policy.effective_mode == "bypassPermissions"


def test_runtime_settings_resolve_permission_layers(tmp_path, monkeypatch):
    home, project = tmp_path / "home", tmp_path / "project"
    monkeypatch.setenv("CRABCODE_HOME", str(home))
    home.mkdir()
    (project / ".crabcode").mkdir(parents=True)
    (home / "settings.json").write_text(json.dumps({"permissions": {
        "default_mode": "run_everything", "allow": [{"tool": "Read"}],
    }}))
    local = project / ".crabcode" / "settings.json"
    local.write_text(json.dumps({"permissions": {
        "default_mode": "ai_review", "deny": [{"tool": "Bash", "command": "rm *"}],
    }}))
    policy = _runtime_settings_from_files(str(project)).permission_policy
    assert policy.configured_mode == "aiReview"
    assert [rule.tool for rule in policy.allow] == ["Read"]
    assert [rule.tool for rule in policy.deny] == ["Bash"]
