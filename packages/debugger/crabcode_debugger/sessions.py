"""Debug session manager built on DAP."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from crabcode_debugger.adapters import AdapterRegistry, AdapterStatus, infer_language
from crabcode_debugger.dap import DAPClient, DAPEvent


@dataclass
class DebugSession:
    session_id: str
    language: str
    adapter: AdapterStatus
    client: DAPClient
    target: dict[str, Any]
    capabilities: dict[str, Any] = field(default_factory=dict)
    last_stopped_thread_id: int | None = None
    pending_configuration_seq: int | None = None


class DebugSessionManager:
    """Tracks live DAP sessions."""

    def __init__(
        self,
        *,
        cwd: str,
        env: dict[str, str] | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        self.cwd = str(Path(cwd).resolve())
        self.env = dict(env or {})
        self.config = config or {}
        self.registry = AdapterRegistry(self.config)
        self.sessions: dict[str, DebugSession] = {}
        self.timeout = float(self.config.get("default_timeout_seconds", 30))
        self.disconnect_timeout = float(self.config.get("disconnect_timeout_seconds", 10))

    def update_config(self, config: dict[str, Any] | None) -> None:
        """Merge later tool configuration into the shared manager."""
        if not config:
            return
        self.config = _merge_dicts(self.config, config)
        self.registry = AdapterRegistry(self.config)
        self.timeout = float(self.config.get("default_timeout_seconds", self.timeout))
        self.disconnect_timeout = float(
            self.config.get("disconnect_timeout_seconds", self.disconnect_timeout)
        )

    async def close(self) -> None:
        for session_id in list(self.sessions):
            try:
                await self.stop(session_id, terminate_debuggee=False)
            except Exception:
                continue

    async def start(
        self,
        *,
        language: str,
        program: str | None = None,
        args: list[str] | None = None,
        cwd: str | None = None,
        adapter_id: str | None = None,
        launch_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized = infer_language(language or program)
        if not normalized:
            return {"error": "language is required or could not be inferred"}
        adapter = self.registry.resolve(normalized, adapter_id)
        if not adapter:
            return {
                "error": f"no available debug adapter for {normalized}",
                "install_hints": self.registry.install_hints(normalized),
                "adapters": [s.to_dict() for s in self.registry.status(normalized)],
            }

        client = DAPClient(
            adapter.command,
            cwd=cwd or self.cwd,
            env=self.env,
            timeout=self.timeout,
            max_events=int(self.config.get("max_dap_events", 10_000)),
        )
        try:
            await client.start()
            capabilities = await client.request(
                "initialize",
                {
                    "clientID": "crabcode",
                    "clientName": "CrabCode",
                    "adapterID": adapter.adapter_id,
                    "pathFormat": "path",
                    "linesStartAt1": True,
                    "columnsStartAt1": True,
                    "supportsVariableType": True,
                    "supportsVariablePaging": True,
                    "supportsRunInTerminalRequest": False,
                },
            )

            launch_args: dict[str, Any] = dict(launch_config or {})
            launch_args.setdefault("cwd", cwd or self.cwd)
            if program:
                program_path = Path(program)
                if not program_path.is_absolute():
                    program_path = Path(cwd or self.cwd) / program_path
                launch_args.setdefault("program", str(program_path.resolve()))
            if args is not None:
                launch_args.setdefault("args", args)

            pending_seq = await client.start_request("launch", launch_args)
            session = DebugSession(
                session_id=f"dbg-{uuid.uuid4().hex[:10]}",
                language=normalized,
                adapter=adapter,
                client=client,
                target={"mode": "launch", "program": program, "cwd": cwd or self.cwd},
                capabilities=capabilities,
                pending_configuration_seq=pending_seq,
            )
            self.sessions[session.session_id] = session
        except BaseException:
            await self._close_failed_client(client)
            raise
        return {
            "session_id": session.session_id,
            "language": normalized,
            "adapter": adapter.to_dict(),
            "capabilities": capabilities,
            "events": [event.to_dict() for event in await self._drain_and_track(session)],
        }

    async def attach(
        self,
        *,
        language: str,
        pid: int,
        adapter_id: str | None = None,
        attach_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if isinstance(pid, bool) or pid <= 0:
            return {"error": "pid must be a positive integer for attach"}
        normalized = infer_language(language)
        if not normalized:
            return {"error": "language is required for attach"}
        adapter = self.registry.resolve(normalized, adapter_id)
        if not adapter:
            return {
                "error": f"no available debug adapter for {normalized}",
                "install_hints": self.registry.install_hints(normalized),
                "adapters": [s.to_dict() for s in self.registry.status(normalized)],
            }
        client = DAPClient(
            adapter.command,
            cwd=self.cwd,
            env=self.env,
            timeout=self.timeout,
            max_events=int(self.config.get("max_dap_events", 10_000)),
        )
        try:
            await client.start()
            capabilities = await client.request(
                "initialize",
                {
                    "clientID": "crabcode",
                    "clientName": "CrabCode",
                    "adapterID": adapter.adapter_id,
                    "pathFormat": "path",
                    "linesStartAt1": True,
                    "columnsStartAt1": True,
                    "supportsVariableType": True,
                },
            )
            args = dict(attach_config or {})
            args.setdefault("processId", pid)
            pending_seq = await client.start_request("attach", args)
            session = DebugSession(
                session_id=f"dbg-{uuid.uuid4().hex[:10]}",
                language=normalized,
                adapter=adapter,
                client=client,
                target={"mode": "attach", "pid": pid},
                capabilities=capabilities,
                pending_configuration_seq=pending_seq,
            )
            self.sessions[session.session_id] = session
        except BaseException:
            await self._close_failed_client(client)
            raise
        return {
            "session_id": session.session_id,
            "language": normalized,
            "adapter": adapter.to_dict(),
            "capabilities": capabilities,
            "events": [event.to_dict() for event in await self._drain_and_track(session)],
        }

    async def set_breakpoints(
        self,
        session_id: str,
        *,
        path: str,
        lines: list[int],
    ) -> dict[str, Any]:
        session = self._require(session_id)
        source_path = Path(path)
        if not source_path.is_absolute():
            source_path = Path(str(session.target.get("cwd") or self.cwd)) / source_path
        body = await session.client.request(
            "setBreakpoints",
            {
                "source": {"path": str(source_path)},
                "breakpoints": [{"line": int(line)} for line in lines],
            },
        )
        return {**body, "events": [event.to_dict() for event in await self._drain_and_track(session)]}

    async def configuration_done(self, session_id: str) -> dict[str, Any]:
        session = self._require(session_id)
        body = await session.client.request("configurationDone", {})
        pending_response: dict[str, Any] | None = None
        if session.pending_configuration_seq is not None:
            pending_response = await session.client.wait_response(
                session.pending_configuration_seq,
                timeout=self.timeout,
            )
            session.pending_configuration_seq = None
        if pending_response is not None:
            body["pending_configuration_response"] = pending_response
        return {**body, "events": [event.to_dict() for event in await self._drain_and_track(session)]}

    async def continue_thread(self, session_id: str, thread_id: int | None = None) -> dict[str, Any]:
        session = self._require(session_id)
        body = await session.client.request("continue", {"threadId": self._thread_id(session, thread_id)})
        return {**body, "events": [event.to_dict() for event in await self._drain_and_track(session)]}

    async def pause(self, session_id: str, thread_id: int | None = None) -> dict[str, Any]:
        session = self._require(session_id)
        body = await session.client.request("pause", {"threadId": self._thread_id(session, thread_id)})
        return {**body, "events": [event.to_dict() for event in await self._drain_and_track(session)]}

    async def step(self, session_id: str, command: str, thread_id: int | None = None) -> dict[str, Any]:
        session = self._require(session_id)
        dap_command = {
            "step_over": "next",
            "step_in": "stepIn",
            "step_out": "stepOut",
        }[command]
        body = await session.client.request(dap_command, {"threadId": self._thread_id(session, thread_id)})
        return {**body, "events": [event.to_dict() for event in await self._drain_and_track(session)]}

    async def threads(self, session_id: str) -> dict[str, Any]:
        session = self._require(session_id)
        body = await session.client.request("threads", {})
        return {**body, "events": [event.to_dict() for event in await self._drain_and_track(session)]}

    async def stack(self, session_id: str, thread_id: int | None = None, levels: int = 20) -> dict[str, Any]:
        session = self._require(session_id)
        body = await session.client.request(
            "stackTrace",
            {"threadId": self._thread_id(session, thread_id), "levels": levels},
        )
        return {**body, "events": [event.to_dict() for event in await self._drain_and_track(session)]}

    async def scopes(self, session_id: str, frame_id: int) -> dict[str, Any]:
        session = self._require(session_id)
        body = await session.client.request("scopes", {"frameId": frame_id})
        return {**body, "events": [event.to_dict() for event in await self._drain_and_track(session)]}

    async def variables(self, session_id: str, variables_reference: int, start: int = 0, count: int = 100) -> dict[str, Any]:
        session = self._require(session_id)
        body = await session.client.request(
            "variables",
            {
                "variablesReference": variables_reference,
                "start": start,
                "count": count,
            },
        )
        return {**body, "events": [event.to_dict() for event in await self._drain_and_track(session)]}

    async def evaluate(
        self,
        session_id: str,
        expression: str,
        *,
        frame_id: int | None = None,
        context: str = "repl",
    ) -> dict[str, Any]:
        session = self._require(session_id)
        args: dict[str, Any] = {"expression": expression, "context": context}
        if frame_id is not None:
            args["frameId"] = frame_id
        body = await session.client.request("evaluate", args)
        return {**body, "events": [event.to_dict() for event in await self._drain_and_track(session)]}

    async def events(self, session_id: str) -> dict[str, Any]:
        session = self._require(session_id)
        return {"events": [event.to_dict() for event in await self._drain_and_track(session)]}

    async def stop(self, session_id: str, *, terminate_debuggee: bool = False) -> dict[str, Any]:
        session = self._require(session_id)
        result: dict[str, Any] = {}
        disconnect_error: str | None = None
        try:
            result = await session.client.request(
                "disconnect",
                {"terminateDebuggee": terminate_debuggee},
                timeout=self.disconnect_timeout,
            )
        except Exception as exc:
            disconnect_error = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                await session.client.close()
            finally:
                self.sessions.pop(session_id, None)
        response = {
            "stopped": session_id,
            "terminate_debuggee": terminate_debuggee,
            "result": result,
        }
        if disconnect_error is not None:
            response["disconnect_warning"] = disconnect_error
        return response

    def list_sessions(self) -> dict[str, Any]:
        return {
            "sessions": [
                {
                    "session_id": s.session_id,
                    "language": s.language,
                    "adapter": s.adapter.to_dict(),
                    "target": s.target,
                    "last_stopped_thread_id": s.last_stopped_thread_id,
                }
                for s in self.sessions.values()
            ]
        }

    def _require(self, session_id: str) -> DebugSession:
        if session_id not in self.sessions:
            raise KeyError(f"unknown debug session: {session_id}")
        return self.sessions[session_id]

    @staticmethod
    def _thread_id(session: DebugSession, thread_id: int | None) -> int:
        if thread_id is not None:
            return int(thread_id)
        if session.last_stopped_thread_id is not None:
            return session.last_stopped_thread_id
        return 1

    async def _drain_and_track(self, session: DebugSession) -> list[DAPEvent]:
        events = await session.client.drain_events()
        for event in events:
            if event.event == "stopped":
                thread_id = event.body.get("threadId")
                if isinstance(thread_id, int):
                    session.last_stopped_thread_id = thread_id
            if event.event in {"terminated", "exited"}:
                session.last_stopped_thread_id = None
        return events

    @staticmethod
    async def _close_failed_client(client: DAPClient) -> None:
        try:
            await asyncio.shield(client.close())
        except Exception:
            pass


def _merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(merged.get(key), dict) and isinstance(value, dict):
            merged[key] = _merge_dicts(merged[key], value)
        else:
            merged[key] = value
    return merged
