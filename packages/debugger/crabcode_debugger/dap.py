"""Small async Debug Adapter Protocol client."""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from typing import Any

from crabcode_core.subprocess_utils import (
    resolve_executable_command,
    managed_process_command,
    subprocess_group_options,
    terminate_process_tree,
)


class DAPError(Exception):
    """Raised when a DAP request fails."""


@dataclass
class DAPEvent:
    event: str
    body: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"event": self.event, "body": self.body}


class DAPClient:
    """Communicates with a debug adapter over stdio."""

    def __init__(
        self,
        command: list[str],
        *,
        cwd: str,
        env: dict[str, str] | None = None,
        timeout: float = 30.0,
        max_events: int = 10_000,
    ) -> None:
        self.command = command
        self.cwd = cwd
        self.env = {**os.environ, **(env or {})}
        if os.name == "nt":
            self.env = {key.upper(): value for key, value in self.env.items()}
        self.timeout = timeout
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._seq = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._pending_commands: dict[int, str] = {}
        self._write_lock = asyncio.Lock()
        self.events: asyncio.Queue[DAPEvent] = asyncio.Queue(maxsize=max(1, int(max_events)))
        self._dropped_events = 0

    async def start(self) -> None:
        if self._process is not None:
            if self._process.returncode is None:
                return
            raise DAPError(f"debug adapter exited with code {self._process.returncode}")
        launch_command = resolve_executable_command(self.command, env=self.env, cwd=self.cwd)
        self._process = await asyncio.create_subprocess_exec(
            *managed_process_command(launch_command),
            cwd=self.cwd,
            env=self.env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **subprocess_group_options(),
        )
        self._reader_task = asyncio.create_task(self._reader_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def close(self) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()
        self._pending.clear()
        self._pending_commands.clear()

        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        self._reader_task = None

        if self._stderr_task and not self._stderr_task.done():
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass
        self._stderr_task = None

        if self._process and self._process.returncode is None:
            await terminate_process_tree(self._process)
        self._process = None

    async def request(
        self,
        command: str,
        arguments: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        seq = await self.start_request(command, arguments)
        return await self.wait_response(seq, timeout=timeout)

    async def start_request(
        self,
        command: str,
        arguments: dict[str, Any] | None = None,
    ) -> int:
        """Send a DAP request and return its sequence without waiting.

        Some DAP requests, notably launch/attach, may not respond until after
        the client sends configurationDone. The session manager uses this to
        keep those requests pending while breakpoints are configured.
        """
        await self.start()
        if not self._process or not self._process.stdin:
            raise DAPError("debug adapter process is not running")

        self._seq += 1
        seq = self._seq
        payload = {
            "seq": seq,
            "type": "request",
            "command": command,
            "arguments": arguments or {},
        }
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[seq] = future
        self._pending_commands[seq] = command
        await self._send(payload)
        return seq

    async def wait_response(
        self,
        seq: int,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        future = self._pending.get(seq)
        if future is None:
            raise DAPError(f"unknown pending DAP request: {seq}")
        command = self._pending_commands.get(seq, f"seq {seq}")
        try:
            response = await asyncio.wait_for(future, timeout=timeout or self.timeout)
        finally:
            self._pending.pop(seq, None)
            self._pending_commands.pop(seq, None)
        if not response.get("success", False):
            error = response.get("body", {}).get("error", {})
            message = response.get("message") or error.get("format")
            raise DAPError(str(message or f"DAP request failed: {command}"))
        body = response.get("body", {})
        return body if isinstance(body, dict) else {"value": body}

    async def drain_events(self) -> list[DAPEvent]:
        drained: list[DAPEvent] = []
        if self._dropped_events:
            drained.append(
                DAPEvent(
                    event="output",
                    body={
                        "category": "console",
                        "output": f"CrabCode dropped {self._dropped_events} buffered DAP events.\n",
                    },
                )
            )
            self._dropped_events = 0
        while True:
            try:
                drained.append(self.events.get_nowait())
            except asyncio.QueueEmpty:
                break
        return drained

    async def wait_for_event(
        self,
        event_name: str,
        *,
        timeout: float | None = None,
    ) -> DAPEvent | None:
        deadline = time.monotonic() + (timeout if timeout is not None else self.timeout)
        unmatched: list[DAPEvent] = []
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                event = await asyncio.wait_for(self.events.get(), timeout=remaining)
                if event.event == event_name:
                    return event
                unmatched.append(event)
        except asyncio.TimeoutError:
            return None
        finally:
            for event in unmatched:
                self._queue_event(event)

    async def _send(self, payload: dict[str, Any]) -> None:
        if not self._process or not self._process.stdin:
            raise DAPError("debug adapter process is not running")
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        async with self._write_lock:
            self._process.stdin.write(header + body)
            await self._process.stdin.drain()

    async def _reader_loop(self) -> None:
        if not self._process or not self._process.stdout:
            return
        reader = self._process.stdout
        while True:
            try:
                headers: dict[str, str] = {}
                while True:
                    line = await reader.readline()
                    if not line:
                        raise EOFError("debug adapter closed stdout")
                    if line in {b"\r\n", b"\n"}:
                        break
                    text = line.decode("ascii", errors="replace").strip()
                    if ":" in text:
                        key, value = text.split(":", 1)
                        headers[key.lower()] = value.strip()
                length_raw = headers.get("content-length")
                if not length_raw:
                    continue
                body = await reader.readexactly(int(length_raw))
                message = json.loads(body.decode("utf-8", errors="replace"))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                for future in list(self._pending.values()):
                    if not future.done():
                        future.set_exception(DAPError(f"DAP reader failed: {exc}"))
                return
            await self._handle_message(message)

    async def _handle_message(self, message: dict[str, Any]) -> None:
        msg_type = message.get("type")
        if msg_type == "response":
            request_seq = message.get("request_seq")
            future = self._pending.get(request_seq)
            if future and not future.done():
                future.set_result(message)
            return
        if msg_type == "event":
            event = str(message.get("event", ""))
            body = message.get("body", {})
            event_body = body if isinstance(body, dict) else {}
            self._queue_event(DAPEvent(event=event, body=event_body))

    def _queue_event(self, event: DAPEvent) -> None:
        try:
            self.events.put_nowait(event)
            return
        except asyncio.QueueFull:
            pass

        if event.event == "output":
            self._dropped_events += 1
            return

        retained: list[DAPEvent] = []
        discarded = False
        while True:
            try:
                queued = self.events.get_nowait()
            except asyncio.QueueEmpty:
                break
            if not discarded and queued.event == "output":
                self._dropped_events += 1
                discarded = True
                continue
            retained.append(queued)

        if not discarded and retained:
            retained.pop(0)
            self._dropped_events += 1
        for queued in retained:
            self.events.put_nowait(queued)
        self.events.put_nowait(event)

    async def _drain_stderr(self) -> None:
        if not self._process or not self._process.stderr:
            return
        while True:
            line = await self._process.stderr.readline()
            if not line:
                return
