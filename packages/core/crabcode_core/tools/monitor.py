"""Background command and WebSocket monitors."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import signal
import socket
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from xml.sax.saxutils import escape

from crabcode_core.session.storage import get_task_output_path
from crabcode_core.types.tool import Tool, ToolContext, ToolResult


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class MonitorSnapshot:
    task_id: str
    session_id: str
    description: str
    source: str
    status: str
    output_file: str
    timeout_ms: int
    persistent: bool
    tool_use_id: str | None = None
    created_at: str = field(default_factory=_now_iso)
    started_at: str | None = None
    finished_at: str | None = None
    updated_at: str = field(default_factory=_now_iso)
    sequence: int = 0
    error: str = ""
    exit_code: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "session_id": self.session_id,
            "description": self.description,
            "task_type": "local_bash",
            "source": self.source,
            "status": self.status,
            "output_file": self.output_file,
            "timeout_ms": self.timeout_ms,
            "persistent": self.persistent,
            "tool_use_id": self.tool_use_id,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "updated_at": self.updated_at,
            "sequence": self.sequence,
            "error": self.error,
            "exit_code": self.exit_code,
        }


@dataclass
class _MonitorRun:
    snapshot: MonitorSnapshot
    session: Any
    task: asyncio.Task[None] | None = None
    process: asyncio.subprocess.Process | None = None
    stop_reason: str = ""
    suppress_notification: bool = False


class MonitorManager:
    """Own session-scoped monitors and their output files."""

    def __init__(self) -> None:
        self._runs: dict[str, _MonitorRun] = {}
        self._closed = False

    def list_tasks(self, session_id: str | None = None) -> list[MonitorSnapshot]:
        snapshots = [
            run.snapshot
            for run in self._runs.values()
            if session_id is None or run.snapshot.session_id == session_id
        ]
        return sorted(snapshots, key=lambda item: item.created_at, reverse=True)

    def get_task(self, task_id: str) -> MonitorSnapshot | None:
        run = self._runs.get(task_id)
        return run.snapshot if run is not None else None

    def resolve_task_id(self, task_id: str) -> str | None:
        if task_id in self._runs:
            return task_id
        matches = [
            candidate for candidate in self._runs if candidate.startswith(task_id)
        ]
        return matches[0] if len(matches) == 1 else None

    async def start(
        self,
        *,
        context: ToolContext,
        description: str,
        command: str | None,
        ws: dict[str, Any] | None,
        timeout_ms: int,
        persistent: bool,
    ) -> MonitorSnapshot:
        if self._closed:
            raise RuntimeError("Monitor manager is closed")
        if context.session is None or not context.session_id:
            raise RuntimeError("Monitor requires an active main session")

        task_id = str(uuid.uuid4())
        output_path = get_task_output_path(context.cwd, context.session_id, task_id)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.touch(exist_ok=True)
        snapshot = MonitorSnapshot(
            task_id=task_id,
            session_id=context.session_id,
            description=description,
            source="websocket" if ws is not None else "command",
            status="running",
            output_file=str(output_path),
            timeout_ms=0 if persistent else timeout_ms,
            persistent=persistent,
            tool_use_id=context.tool_use_id,
            started_at=_now_iso(),
        )
        run = _MonitorRun(snapshot=snapshot, session=context.session)
        self._runs[task_id] = run
        run.task = asyncio.create_task(
            self._run_source(
                run,
                command=command,
                ws=ws,
                env={**os.environ, **context.env},
                cwd=context.cwd,
            )
        )
        return snapshot

    async def stop_task(self, task_id: str) -> bool:
        resolved = self.resolve_task_id(task_id)
        run = self._runs.get(resolved or "")
        if run is None or run.task is None or run.task.done():
            return False
        run.stop_reason = "stopped by request"
        run.task.cancel()
        await asyncio.gather(run.task, return_exceptions=True)
        # A task cancelled before its coroutine is first scheduled never enters
        # _run_source(), so its finally block cannot update the snapshot.
        if run.snapshot.status == "running":
            await self._finish(
                run,
                status="stopped",
                error=run.stop_reason,
                exit_code=None,
            )
        return True

    def cancel_session_now(self, session_id: str, reason: str) -> None:
        for run in self._runs.values():
            if (
                run.snapshot.session_id != session_id
                or run.task is None
                or run.task.done()
            ):
                continue
            run.stop_reason = reason
            run.suppress_notification = True
            self._mark_finished(
                run,
                status="stopped",
                error=reason,
                exit_code=None,
            )
            run.task.cancel()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        tasks: list[asyncio.Task[None]] = []
        for run in self._runs.values():
            if run.task is None or run.task.done():
                continue
            run.stop_reason = "session ended"
            run.suppress_notification = True
            self._mark_finished(
                run,
                status="stopped",
                error=run.stop_reason,
                exit_code=None,
            )
            run.task.cancel()
            tasks.append(run.task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_source(
        self,
        run: _MonitorRun,
        *,
        command: str | None,
        ws: dict[str, Any] | None,
        env: dict[str, str],
        cwd: str,
    ) -> None:
        status = "completed"
        error = ""
        exit_code: int | None = None
        try:
            source = (
                self._consume_websocket(run, ws or {})
                if ws is not None
                else self._consume_command(run, command or "", env=env, cwd=cwd)
            )
            if run.snapshot.persistent:
                exit_code = await source
            else:
                exit_code = await asyncio.wait_for(
                    source,
                    timeout=max(0.001, run.snapshot.timeout_ms / 1000.0),
                )
            if command is not None and exit_code not in {None, 0}:
                status = "failed"
                error = f"command exited with status {exit_code}"
        except asyncio.TimeoutError:
            status = "stopped"
            error = f"monitor reached its {run.snapshot.timeout_ms}ms timeout"
        except asyncio.CancelledError:
            status = "stopped"
            error = run.stop_reason or "stopped"
        except Exception as exc:
            status = "failed"
            error = str(exc)
        finally:
            await self._terminate_process(run)
            await self._finish(run, status=status, error=error, exit_code=exit_code)

    async def _consume_command(
        self,
        run: _MonitorRun,
        command: str,
        *,
        env: dict[str, str],
        cwd: str,
    ) -> int:
        subprocess_options: dict[str, Any] = {}
        if os.name != "nt":
            subprocess_options["start_new_session"] = True
        run.process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=cwd,
            env=env,
            **subprocess_options,
        )
        assert run.process.stdout is not None
        while True:
            raw = await run.process.stdout.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            await self._emit_line(run, line)
        return await run.process.wait()

    async def _consume_websocket(
        self,
        run: _MonitorRun,
        ws: dict[str, Any],
    ) -> None:
        url = str(ws.get("url") or "")
        protocols = ws.get("protocols")
        await self._validate_websocket_target(url)
        try:
            from websockets.asyncio.client import connect
        except ImportError as exc:  # pragma: no cover - dependency packaging guard
            raise RuntimeError(
                "WebSocket monitoring requires the 'websockets' package"
            ) from exc

        async with connect(url, subprotocols=protocols) as websocket:
            async for message in websocket:
                if isinstance(message, str):
                    line = message
                else:
                    line = f"[binary frame, {len(message)} bytes]"
                if len(line.encode("utf-8", errors="replace")) > 1024 * 1024:
                    raise RuntimeError(
                        "WebSocket message exceeded the 1 MiB monitor limit"
                    )
                await self._emit_line(run, line)
        return None

    @staticmethod
    async def _validate_websocket_target(url: str) -> None:
        if any(char.isspace() for char in url):
            raise ValueError("WebSocket URL must not contain whitespace")
        try:
            url.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError(
                "WebSocket URL must contain ASCII characters only"
            ) from exc
        parsed = urlsplit(url)
        if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
            raise ValueError("WebSocket URL must use ws:// or wss://")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("WebSocket URL must not contain credentials")

        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        loop = asyncio.get_running_loop()
        addresses = await loop.getaddrinfo(
            parsed.hostname,
            port,
            type=socket.SOCK_STREAM,
        )
        for address in addresses:
            ip = ipaddress.ip_address(address[4][0])
            if not ip.is_global:
                raise ValueError(
                    "WebSocket monitor refuses private, local, or reserved addresses"
                )

    async def _emit_line(self, run: _MonitorRun, line: str) -> None:
        output_path = Path(run.snapshot.output_file)
        with open(output_path, "a", encoding="utf-8") as output:
            output.write(line + "\n")
            output.flush()

        run.snapshot.sequence += 1
        run.snapshot.updated_at = _now_iso()
        safe_line = line
        if len(safe_line) > 100_000:
            safe_line = (
                safe_line[:100_000] + "… [event truncated; full line in output file]"
            )
        notification = "\n".join(
            [
                "<monitor-event>",
                f"<task-id>{run.snapshot.task_id}</task-id>",
                f"<description>{escape(run.snapshot.description)}</description>",
                f"<sequence>{run.snapshot.sequence}</sequence>",
                f"<output-file>{escape(run.snapshot.output_file)}</output-file>",
                f"<timestamp>{run.snapshot.updated_at}</timestamp>",
                f"<event>{escape(safe_line)}</event>",
                "</monitor-event>",
            ]
        )
        await run.session.enqueue_monitor_notification(
            notification,
            session_id=run.snapshot.session_id,
        )

    async def _finish(
        self,
        run: _MonitorRun,
        *,
        status: str,
        error: str,
        exit_code: int | None,
    ) -> None:
        self._mark_finished(
            run,
            status=status,
            error=error,
            exit_code=exit_code,
        )
        if run.suppress_notification:
            return
        summary = error or f"{run.snapshot.description} {status}"
        notification = "\n".join(
            [
                "<task-notification>",
                f"<task-id>{run.snapshot.task_id}</task-id>",
                f"<session-id>{run.snapshot.session_id}</session-id>",
                f"<uuid>{uuid.uuid4()}</uuid>",
                *(
                    [f"<tool-use-id>{escape(run.snapshot.tool_use_id)}</tool-use-id>"]
                    if run.snapshot.tool_use_id
                    else []
                ),
                "<task-type>local_bash</task-type>",
                f"<status>{status}</status>",
                f"<output-file>{escape(run.snapshot.output_file)}</output-file>",
                f"<summary>{escape(summary)}</summary>",
                "</task-notification>",
            ]
        )
        await run.session.enqueue_monitor_notification(
            notification,
            session_id=run.snapshot.session_id,
        )

    @staticmethod
    def _mark_finished(
        run: _MonitorRun,
        *,
        status: str,
        error: str,
        exit_code: int | None,
    ) -> None:
        run.snapshot.status = status
        run.snapshot.error = error
        run.snapshot.exit_code = exit_code
        run.snapshot.updated_at = _now_iso()
        run.snapshot.finished_at = run.snapshot.updated_at

    @staticmethod
    async def _terminate_process(run: _MonitorRun) -> None:
        process = run.process
        if process is None or process.returncode is not None:
            return
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        else:  # pragma: no cover - exercised on Windows
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=2)
        except asyncio.TimeoutError:
            if os.name != "nt":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:  # pragma: no cover - exercised on Windows
                process.kill()
            await process.wait()


class MonitorTool(Tool):
    name = "Monitor"
    description = (
        "Watch a command or WebSocket in the background. Every output line or text "
        "message is delivered as an event that automatically resumes the conversation."
    )
    is_read_only = False
    is_concurrency_safe = True
    input_schema = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Shell command; every stdout/stderr line becomes an event.",
            },
            "ws": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "protocols": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["url"],
                "additionalProperties": False,
            },
            "description": {
                "type": "string",
                "description": "Short description shown in event notifications.",
            },
            "timeout_ms": {
                "type": "integer",
                "minimum": 1,
                "maximum": 3_600_000,
                "default": 300_000,
            },
            "persistent": {
                "type": "boolean",
                "description": "Run until TaskStop or session end.",
                "default": False,
            },
        },
        "required": ["description"],
        "additionalProperties": False,
    }

    def __init__(self, manager: MonitorManager | None = None) -> None:
        self.manager = manager or MonitorManager()

    async def validate_input(self, tool_input: dict[str, Any]) -> str | None:
        command = tool_input.get("command")
        ws = tool_input.get("ws")
        if bool(command) == bool(ws):
            return "Provide exactly one of command or ws."
        if command is not None and not str(command).strip():
            return "command must not be empty."
        if not str(tool_input.get("description") or "").strip():
            return "description is required."
        timeout_ms = tool_input.get("timeout_ms", 300_000)
        if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool):
            return "timeout_ms must be an integer."
        if timeout_ms < 1 or timeout_ms > 3_600_000:
            return "timeout_ms must be between 1 and 3600000."
        if ws is not None and not isinstance(ws, dict):
            return "ws must be an object."
        if isinstance(ws, dict):
            url = ws.get("url")
            if not isinstance(url, str) or not url:
                return "ws.url is required."
            if any(char.isspace() for char in url):
                return "ws.url must not contain whitespace."
            try:
                url.encode("ascii")
            except UnicodeEncodeError:
                return "ws.url must contain ASCII characters only."
            parsed = urlsplit(url)
            if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
                return "ws.url must use ws:// or wss://."
            if parsed.username is not None or parsed.password is not None:
                return "ws.url must not contain credentials."
            protocols = ws.get("protocols")
            if protocols is not None:
                if not isinstance(protocols, list) or not all(
                    isinstance(protocol, str) for protocol in protocols
                ):
                    return "ws.protocols must be an array of strings."
                if len(set(protocols)) != len(protocols):
                    return "ws.protocols must not contain duplicates."
                token = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
                if any(not token.fullmatch(protocol) for protocol in protocols):
                    return "ws.protocols contains an invalid subprotocol token."
        return None

    async def call(
        self,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        validation_error = await self.validate_input(tool_input)
        if validation_error:
            return ToolResult(
                result_for_model=f"Error: {validation_error}", is_error=True
            )
        # A monitor is a long-lived watcher, not a file-mutating Bash call.  Do
        # not take the synchronous working-tree snapshot used by BashTool here:
        # for a non-git cwd (for example a user's home directory) it recursively
        # copies the tree before the monitor is even registered, blocking the
        # event loop and hiding the task ID from the frontend.
        try:
            snapshot = await self.manager.start(
                context=context,
                description=str(tool_input["description"]).strip(),
                command=(
                    str(tool_input["command"])
                    if tool_input.get("command") is not None
                    else None
                ),
                ws=tool_input.get("ws"),
                timeout_ms=int(tool_input.get("timeout_ms", 300_000)),
                persistent=bool(tool_input.get("persistent", False)),
            )
        except (OSError, RuntimeError, ValueError) as exc:
            return ToolResult(result_for_model=f"Error: {exc}", is_error=True)
        return ToolResult(
            data={
                "taskId": snapshot.task_id,
                "timeoutMs": snapshot.timeout_ms,
                "persistent": snapshot.persistent,
                "outputFile": snapshot.output_file,
            },
            result_for_model=(
                f"taskId: {snapshot.task_id}\n"
                f"timeoutMs: {snapshot.timeout_ms}\n"
                f"persistent: {str(snapshot.persistent).lower()}\n"
                f"outputFile: {snapshot.output_file}"
            ),
        )

    async def close(self) -> None:
        await self.manager.close()


class TaskListTool(Tool):
    name = "TaskList"
    description = "List background monitors and managed sub-agents in this session."
    is_read_only = True
    is_concurrency_safe = True
    input_schema = {"type": "object", "properties": {}}

    def __init__(self, manager: MonitorManager) -> None:
        self.manager = manager

    async def call(
        self,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        tasks: list[dict[str, Any]] = [
            snapshot.to_dict()
            for snapshot in self.manager.list_tasks(context.session_id or None)
        ]
        if context.agent_manager is not None:
            tasks.extend(
                {
                    "task_id": snapshot.agent_id,
                    "session_id": snapshot.session_id,
                    "description": snapshot.title,
                    "task_type": "local_agent",
                    "status": snapshot.status,
                    "output_file": snapshot.transcript_path,
                    "created_at": snapshot.created_at,
                    "finished_at": snapshot.finished_at,
                    "error": snapshot.error,
                }
                for snapshot in context.agent_manager.list_agents()
                if not context.session_id or snapshot.session_id == context.session_id
            )
        if not tasks:
            return ToolResult(
                data={"tasks": []}, result_for_model="No background tasks."
            )
        lines = [
            f"{item['task_id']} · {item['status']} · {item['task_type']} · "
            f"{item['description']}"
            for item in tasks
        ]
        return ToolResult(data={"tasks": tasks}, result_for_model="\n".join(lines))


class TaskStopTool(Tool):
    name = "TaskStop"
    description = "Stop a running background monitor or managed sub-agent by ID."
    is_read_only = False
    is_concurrency_safe = True
    uses_tool_permission_policy = True
    input_schema = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Background task ID."}
        },
        "required": ["task_id"],
    }

    def __init__(self, manager: MonitorManager) -> None:
        self.manager = manager

    async def call(
        self,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        task_id = str(tool_input.get("task_id") or "").strip()
        if not task_id:
            return ToolResult(
                result_for_model="Error: task_id is required", is_error=True
            )
        resolved_monitor_id = self.manager.resolve_task_id(task_id)
        if resolved_monitor_id and await self.manager.stop_task(resolved_monitor_id):
            snapshot = self.manager.get_task(resolved_monitor_id)
            return ToolResult(
                data={"task_id": resolved_monitor_id, "stopped": True},
                result_for_model=(
                    f"Stopped task: {resolved_monitor_id}"
                    + (f"\noutputFile: {snapshot.output_file}" if snapshot else "")
                ),
            )
        agent_id = task_id
        if context.agent_manager is not None:
            matches = [
                snapshot.agent_id
                for snapshot in context.agent_manager.list_agents()
                if snapshot.agent_id.startswith(task_id)
            ]
            if len(matches) == 1:
                agent_id = matches[0]
        if (
            context.agent_manager is not None
            and await context.agent_manager.cancel_agent(agent_id)
        ):
            return ToolResult(
                data={"task_id": agent_id, "stopped": True},
                result_for_model=f"Stopped agent task: {agent_id}",
            )
        return ToolResult(
            data={"task_id": task_id, "stopped": False},
            result_for_model=f"Error: no running background task {task_id}",
            is_error=True,
        )
