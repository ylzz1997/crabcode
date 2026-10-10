"""CLI Full Access overrides stay local to the current invocation."""

from io import StringIO
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from crabcode_cli.app import app
from crabcode_core.events import CoreSession
from crabcode_core.permissions.manager import PermissionManager, PermissionMode
from crabcode_core.tools.bash import BashTool
from crabcode_core.types.config import CrabCodeSettings
from crabcode_core.types.tool import PermissionBehavior


@pytest.fixture
def cli_run(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr("crabcode_cli.app.sys.stdin", StringIO(""))
    monkeypatch.setattr("crabcode_cli.app.configure_logging", lambda *args: None)
    monkeypatch.setattr("crabcode_cli.app.os._exit", lambda code: None)
    monkeypatch.setattr(
        "crabcode_core.session.storage.SessionStorage.from_session_id",
        lambda sid: SimpleNamespace(cwd=str(tmp_path)),
    )
    monkeypatch.setattr(
        "crabcode_core.session.storage.SessionStorage.list_sessions",
        lambda cwd: [{"session_id": "previous"}],
    )

    async def run_repl(**kwargs):
        captured.update(kwargs)

    async def run_pipe(prompt, **kwargs):
        captured.update(kwargs, prompt=prompt)

    monkeypatch.setattr("crabcode_cli.repl.run_repl", run_repl)
    monkeypatch.setattr("crabcode_cli.pipe.run_pipe", run_pipe)

    def invoke(args, mode="ask"):
        file_settings = CrabCodeSettings(permissions={
            "default_mode": mode,
            "deny": [{"tool": "Bash"}],
        })
        monkeypatch.setattr(
            "crabcode_core.config.manager.ConfigManager.load",
            lambda self: file_settings,
        )
        result = CliRunner().invoke(app, ["main", "--cwd", str(tmp_path), *args])
        assert result.exit_code == 0, result.output
        # No persistent config object is modified by the flag.
        assert file_settings.permissions.default_mode == mode
        return captured

    return invoke


@pytest.mark.parametrize("args,resume_id", [
    (["--yolo"], None),
    (["--yolo", "--continue"], "previous"),
    (["--yolo", "--resume", "saved"], "saved"),
    (["--yolo", "-p", "fix the bug"], None),
    (["--yolo", "fix the bug"], None),
])
def test_yolo_bypasses_configured_rules_and_survives_project_reloads(cli_run, args, resume_id):
    captured = cli_run(args, mode="ai_review")
    assert captured.get("resume_session_id") == resume_id
    session = CoreSession(settings=captured["settings"])
    # Initialization and cross-project resume both merge the target project's
    # settings with the original CLI overrides.
    for mode in ("ask", "ai_review"):
        merged = session._merge_project_settings(CrabCodeSettings(permissions={
            "default_mode": mode,
            "deny": [{"tool": "Bash"}],
        }))
        manager = PermissionManager(settings=merged.permissions)
        assert manager.check(BashTool(), {"command": "echo hello"}).behavior == PermissionBehavior.ALLOW
        manager.mode = PermissionMode.PLAN
        assert manager.check(BashTool(), {"command": "echo hello"}).behavior == PermissionBehavior.DENY


@pytest.mark.parametrize("mode", ["ask", "ai_review", "run_everything"])
def test_without_yolo_preserves_configured_permission_mode(cli_run, mode):
    settings = cli_run([], mode=mode)["settings"]
    assert settings.permissions.default_mode == mode
    assert settings._crabcode_explicit_settings.permissions.default_mode is None
