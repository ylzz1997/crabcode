"""CLI discovery/lifecycle and native IPC, without touching the user's desktop."""

import asyncio
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from typer.testing import CliRunner

from crabcode_cli import computer_use as cu
from crabcode_cli.app import app
from crabcode_core.tools.computer_use import ComputerUseTool
from crabcode_core.types.tool import ToolContext


CAPS = {"instance_id": "first", "host_protocol_version": 1, "gui_available": True,
        "input_available": True, "capture_available": True, "ax_available": True,
        "ax_protocol_version": 1, "delivery_policy_version": 1,
        "supported_modes": ["background_app", "foreground_desktop"]}


@asynccontextmanager
async def executor(tmp_path, reply=None):
    messages = []
    writers = []

    async def handle(reader, writer):
        writers.append(writer)
        message = json.loads(await reader.readline())
        messages.append(message)
        if reply is None:
            result = CAPS if message["method"] == "capabilities" else {"ok": True}
            wire = json.dumps({"ok": True, "result": result}).encode() + b"\n"
        else:
            wire = reply(message)
        if wire is not None:
            for index in range(0, len(wire), 1703):
                writer.write(wire[index:index + 1703])
            await writer.drain()
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    client = cu.NativeClient(tmp_path)
    endpoint = {"protocol_version": 1, "port": server.sockets[0].getsockname()[1],
                "token": "a" * 64, "instance_id": "first"}
    (tmp_path / "endpoint.json").write_text(json.dumps(endpoint))
    executable = cu.bundled_executable(tmp_path)
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.touch()
    client.start = AsyncMock()
    try:
        yield client, messages
    finally:
        for writer in writers:
            writer.close()
        server.close()
        await server.wait_closed()


def test_native_large_frames_and_authenticated_framing(tmp_path):
    async def scenario():
        result = {"screenshot": {"data": "a" * 256000}, "effect_verified": False,
                  "accessibility": {"snapshot_id": "snapshot"}}
        wire = json.dumps({"ok": True, "result": result}).encode() + b"\n"
        async with executor(tmp_path, lambda _: wire) as (client, messages):
            assert await client.request("execute", {"instanceId": "first"}, instance_id="first") == result
            assert len(messages) == 1
            assert messages[0]["token"] == "a" * 64
            assert messages[0]["instance_id"] == "first"
            assert messages[0]["params"] == {"instanceId": "first"}
            assert messages[0]["deadline_ms"] > 0
    asyncio.run(scenario())


@pytest.mark.parametrize("wire,error", [(b'{"ok":true,', "不完整"), (None, "超时"),
                                       (b'{"ok":false,"error":"stale"}\n', "stale")])
def test_native_errors_never_replay_input(tmp_path, wire, error):
    async def scenario():
        async with executor(tmp_path, lambda _: wire) as (client, messages):
            with pytest.raises(cu.ComputerUseError, match=error):
                await client.request("execute", timeout=0.05)
            assert len(messages) == 1
    asyncio.run(scenario())


def test_old_instance_and_oversize_requests_are_not_sent(tmp_path):
    async def scenario():
        async with executor(tmp_path) as (client, messages):
            with pytest.raises(cu.ComputerUseError, match="已重启"):
                await client.request("execute", instance_id="old")
            with pytest.raises(cu.ComputerUseError, match="大小限制"):
                await client.request("execute", {"text": "a" * 1048576})
            assert messages == []
    asyncio.run(scenario())


def test_automatic_binding_uses_core_policy_and_scopes_leases(tmp_path, monkeypatch):
    async def scenario():
        async with executor(tmp_path) as (client, messages):
            monkeypatch.setattr(cu, "NativeClient", lambda: client)
            session = SimpleNamespace(computer_use_mode="background_app",
                                      computer_use_delivery_policy="strict_background")
            backend = await cu.connect_session(session)
            assert session.computer_use_enabled
            tool = ComputerUseTool()
            context = ToolContext(session=session, session_id="session", agent_id="child")
            await tool.setup(context)
            assert tool.is_available(context)
            result = await tool.call({"action": "list_windows"}, context)
            assert not result.is_error
            sent = next(m["params"] for m in messages if m["method"] == "execute")
            assert sent["request"]["delivery_policy"] == "strict_background"
            assert sent["lease"] == sent["request"]["owner"] == {
                "host_id": backend.host_id, "connection_id": backend.connection_id,
                "session_id": "session", "agent_id": "child",
            }
            await backend.release(backend.host_id, session_id="session", agent_id="child")
            assert messages[-1]["params"]["allAgents"] is False
            await backend.close()
            assert messages[-1]["params"]["sessionId"] is None
            assert messages[-1]["params"]["allAgents"] is True
            assert not tool.is_available(context)
            assert backend._heartbeat is None
    asyncio.run(scenario())


def test_monitor_frames_removed_and_scope_guarded():
    async def scenario():
        client = SimpleNamespace(request=AsyncMock(return_value={
            "ok": True, "preview_screenshot": {"data": "private-preview"},
            "preview_screenshot_error": "preview", "screenshot": {"data": "model"},
        }))
        backend = cu.LocalComputerUse(client)
        backend._caps = dict(CAPS)
        result = await backend.execute(backend.host_id, session_id="session", agent_id=None,
                                       action={"action": "observe", "window_id": "1"})
        assert result == {"ok": True, "screenshot": {"data": "model"}}
        for options in ({"target_scope": "desktop"}, {"mode": "foreground_desktop", "target_scope": "app_window"},
                        {"delivery_policy": "invalid"}):
            with pytest.raises(cu.ComputerUseError):
                await backend.execute(backend.host_id, session_id="session", agent_id=None,
                                      action={"action": "click"}, **options)
        assert client.request.await_count == 1
    asyncio.run(scenario())


def test_restart_rotates_owner_and_reconnect_does_not_replay_input():
    async def scenario():
        client = SimpleNamespace(start=AsyncMock(), request=AsyncMock(return_value=dict(CAPS)))
        backend = cu.LocalComputerUse(client)
        await backend.refresh()
        connection = backend.connection_id
        await backend.refresh()
        assert backend.connection_id == connection
        client.request.return_value = {**CAPS, "instance_id": "second"}
        await backend.refresh()
        assert backend.connection_id != connection
        assert all(call.args[0] == "capabilities" for call in client.request.await_args_list)
    asyncio.run(scenario())


def test_absent_or_broken_component_does_not_break_cli(tmp_path, monkeypatch):
    async def scenario():
        client = cu.NativeClient(tmp_path)
        monkeypatch.setattr(cu, "NativeClient", lambda: client)
        session = SimpleNamespace(computer_use_enabled=False)
        assert await cu.connect_session(session) is None
        assert not session.computer_use_enabled
        executable = cu.bundled_executable(tmp_path)
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.touch()
        client.start = AsyncMock(side_effect=cu.ComputerUseError("unavailable"))
        assert await cu.connect_session(session) is None
        assert not session.computer_use_enabled
    asyncio.run(scenario())


def test_heartbeat_recovers_after_restart_without_replaying_input(monkeypatch):
    async def scenario():
        calls = []
        capability_reads = 0

        async def request(method, *args, **kwargs):
            nonlocal capability_reads
            calls.append(method)
            if method == "heartbeat" and capability_reads < 3:
                raise cu.ComputerUseError("disconnected")
            if method == "capabilities":
                capability_reads += 1
                if capability_reads == 2:
                    raise cu.ComputerUseError("restarting")
                return {**CAPS, "instance_id": "first" if capability_reads == 1 else "second"}

        client = SimpleNamespace(start=AsyncMock(), request=request)
        backend = cu.LocalComputerUse(client)
        monkeypatch.setattr(cu, "_HEARTBEAT_SECONDS", 0.01)
        await backend.connect()
        original_owner = backend.connection_id
        try:
            async def recovered():
                while backend.capabilities(backend.host_id).get("instance_id") != "second":
                    await asyncio.sleep(0.005)
            await asyncio.wait_for(recovered(), 2)
            assert backend.is_available(backend.host_id)
            assert backend.connection_id != original_owner
            assert "execute" not in calls
        finally:
            await backend.close()
    asyncio.run(scenario())


def test_repl_automatically_binds_and_cleans_up_on_eof(tmp_path, monkeypatch):
    from contextlib import nullcontext
    from io import StringIO
    from rich.console import Console
    from crabcode_cli import repl
    from crabcode_core.events import CoreSession

    async def scenario():
        async with executor(tmp_path) as (client, messages):
            monkeypatch.setattr(cu, "NativeClient", lambda: client)
            session = CoreSession(cwd=str(tmp_path))
            monkeypatch.setattr(repl, "CoreSession", lambda **kwargs: session)
            output = StringIO()
            monkeypatch.setattr(repl, "console", Console(file=output, width=160))
            monkeypatch.setattr(repl, "patch_stdout", lambda **kwargs: nullcontext())
            requested_status = False

            async def next_event():
                nonlocal requested_status
                while not session.computer_use_enabled:
                    await asyncio.sleep(0)
                assert session.computer_use_enabled
                assert session.computer_use_backend.is_available(session.computer_use_host_id)
                if not requested_status:
                    assert "Computer Use:" not in output.getvalue()
                    requested_status = True
                    return "submit", "/status"
                assert "Computer Use: ready" in output.getvalue()
                assert "capture=yes" in output.getvalue()
                return "eof", ""

            composer = SimpleNamespace(start=lambda: None, next_event=next_event, close=AsyncMock())
            monkeypatch.setattr(repl, "_PersistentComposer", lambda *args: composer)
            await repl.run_repl(cwd=str(tmp_path))
            assert not session.computer_use_backend.is_available(session.computer_use_host_id)
            assert messages[-1]["method"] == "release"
            assert messages[-1]["params"]["allAgents"] is True
            composer.close.assert_awaited_once()
    asyncio.run(scenario())


@pytest.mark.parametrize("outcome", ["ready", "cancelled", "failed"])
def test_repl_accepts_chat_while_native_startup_is_pending(tmp_path, monkeypatch, outcome):
    from io import StringIO
    from prompt_toolkit.application.current import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    from rich.console import Console
    from crabcode_cli import repl
    from crabcode_core.events import CoreSession
    from crabcode_core.types.event import TurnCompleteEvent

    async def scenario():
        async with executor(tmp_path) as (client, messages):
            started = asyncio.Event()
            finish_startup = asyncio.Event()
            startup_finished = asyncio.Event()

            async def slow_start():
                started.set()
                try:
                    await finish_startup.wait()
                    if outcome == "failed":
                        raise cu.ComputerUseError("startup failed")
                finally:
                    startup_finished.set()

            client.start = slow_start
            monkeypatch.setattr(cu, "NativeClient", lambda: client)
            session = CoreSession(cwd=str(tmp_path))
            completed = []

            async def send_message(text, **kwargs):
                completed.append(text)
                yield TurnCompleteEvent()

            session.send_message = send_message
            monkeypatch.setattr(repl, "CoreSession", lambda **kwargs: session)
            output = StringIO()
            monkeypatch.setattr(repl, "console", Console(file=output, width=160))
            composers = []
            make_composer = repl._PersistentComposer

            def composer(*args):
                result = make_composer(*args)
                composers.append(result)
                return result

            monkeypatch.setattr(repl, "_PersistentComposer", composer)

            async def wait_for(predicate):
                async def poll():
                    while not predicate():
                        await asyncio.sleep(0.01)
                await asyncio.wait_for(poll(), 3)

            with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
                task = asyncio.create_task(repl.run_repl(cwd=str(tmp_path)))
                try:
                    await wait_for(lambda: started.is_set() and composers
                                   and composers[0].prompt_session.app.is_running)
                    assert cu.session_status(session) == "starting"
                    pipe.send_text("hello\r")
                    await wait_for(lambda: completed == ["hello"] and not composers[0]._busy)
                    assert not startup_finished.is_set()
                    pipe.send_text("/status\r")
                    await wait_for(lambda: "Computer Use: starting" in output.getvalue())
                    if outcome != "cancelled":
                        finish_startup.set()
                        await wait_for(lambda: not session._computer_use_connecting)
                        if outcome == "ready":
                            assert session.computer_use_enabled
                            assert cu.session_status(session).startswith("ready")
                        else:
                            assert not session.computer_use_enabled
                            assert cu.session_status(session).startswith("unavailable")
                    pipe.send_text("\x04")
                    await asyncio.wait_for(task, 3)
                finally:
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            assert startup_finished.is_set()
            assert not session._computer_use_connecting
            if outcome == "ready":
                assert session.computer_use_backend._heartbeat is None
                assert not session.computer_use_backend.is_available(session.computer_use_host_id)
                assert messages[-1]["method"] == "release"
            else:
                assert session.computer_use_backend is None
                assert messages == []
    asyncio.run(scenario())


@pytest.mark.parametrize("fail", [False, True])
def test_pipe_auto_connects_without_flags_and_closes_after_session(tmp_path, monkeypatch, fail):
    from crabcode_cli.pipe import run_pipe

    async def scenario():
        async with executor(tmp_path) as (client, messages):
            monkeypatch.setattr(cu, "NativeClient", lambda: client)
            session = SimpleNamespace()

            async def send_message(*args, **kwargs):
                assert session.computer_use_enabled
                assert session.computer_use_backend.is_available(session.computer_use_host_id)
                if fail:
                    raise RuntimeError("turn failed")
                if False:
                    yield

            async def close():
                assert session.computer_use_backend.is_available(session.computer_use_host_id)
                await session.computer_use_backend.release(session.computer_use_host_id, session_id="session", all_agents=True)

            session.send_message = send_message
            session.close = close
            monkeypatch.setattr("crabcode_cli.pipe.CoreSession", lambda **kwargs: session)
            if fail:
                with pytest.raises(RuntimeError, match="turn failed"):
                    await run_pipe("hello")
            else:
                await run_pipe("hello")
            assert not session.computer_use_backend.is_available(session.computer_use_host_id)
            releases = [m["params"] for m in messages if m["method"] == "release"]
            assert [r["sessionId"] for r in releases] == ["session", None]
    asyncio.run(scenario())


def test_cli_status_and_permissions_do_not_require_a_session_flag(monkeypatch, tmp_path):
    client = SimpleNamespace(state_dir=tmp_path, start=AsyncMock(), request=AsyncMock(return_value=CAPS))
    monkeypatch.setattr(cu, "NativeClient", lambda: client)
    runner = CliRunner()
    result = runner.invoke(app, ["computer-use", "status", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["input_available"] is True
    result = runner.invoke(app, ["computer-use", "permissions"])
    assert result.exit_code == 0, result.output
    client.start.assert_awaited_with()
    assert client.request.await_args_list[-2].args == ("permissions",)
    result = runner.invoke(app, ["computer-use", "permissions", "--screen-recording"])
    assert result.exit_code == 0, result.output
    assert client.request.await_args_list[-2].args == ("permissions", {"kind": "screen_recording"})
    result = runner.invoke(app, ["computer-use", "permissions", "--accessibility"])
    assert result.exit_code == 0, result.output
    assert client.request.await_args_list[-2].args == ("permissions", {"kind": "accessibility"})
    client.start.side_effect = cu.ComputerUseError("missing component")
    result = runner.invoke(app, ["computer-use", "status", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.output)["status"] == "unavailable"


def test_bundled_component_bootstraps_without_an_existing_installation(tmp_path, monkeypatch):
    async def scenario():
        async with executor(tmp_path) as (client, messages):
            cu.bundled_executable(tmp_path).unlink()
            package = tmp_path / "package"
            monkeypatch.setattr(cu, "__file__", str(package / "computer_use.py"))
            executable = cu.bundled_executable(package / "_native")
            executable.parent.mkdir(parents=True)
            executable.touch()
            assert not client.installed
            assert client.bundle == package / "_native"
            monkeypatch.setattr(cu, "NativeClient", lambda: client)
            session = SimpleNamespace()
            backend = await cu.connect_session(session)
            assert session.computer_use_enabled
            client.start.assert_awaited_once()
            await backend.close()
    asyncio.run(scenario())


def test_start_uses_packaged_binary_and_keeps_bootstrap_output_quiet(tmp_path, monkeypatch, capsys):
    async def scenario():
        package = tmp_path / "package"
        monkeypatch.setattr(cu, "__file__", str(package / "computer_use.py"))
        executable = cu.bundled_executable(package / "_native")
        executable.parent.mkdir(parents=True)
        executable.touch()
        process = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(None, b"")))
        spawn = AsyncMock(return_value=process)
        monkeypatch.setattr(cu.asyncio, "create_subprocess_exec", spawn)
        client = cu.NativeClient(tmp_path / "fresh-state")
        await client.start()
        assert spawn.await_args.args == (str(executable), "--install-and-start", "--state-dir", str(client.state_dir))
        assert spawn.await_args.kwargs["stdout"] == asyncio.subprocess.DEVNULL
        assert capsys.readouterr().out == ""
    asyncio.run(scenario())


@pytest.mark.parametrize("caps,enabled,expected", [
    (CAPS, True, "ready"),
    ({**CAPS, "input_available": False}, True, "limited"),
    ({**CAPS, "gui_available": False}, True, "unavailable"),
    ({**CAPS, "supported_modes": ["foreground_desktop"]}, True, "unavailable in background_app"),
    (CAPS, False, "disabled"),
])
def test_status_reports_effective_availability(caps, enabled, expected):
    backend = cu.LocalComputerUse(SimpleNamespace())
    backend._caps = dict(caps)
    session = SimpleNamespace(computer_use_backend=backend, computer_use_host_id=backend.host_id,
                              computer_use_enabled=enabled, computer_use_mode="background_app")
    assert cu.session_status(session).startswith(expected)
