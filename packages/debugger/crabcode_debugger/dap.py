"""Small async Debug Adapter Protocol client."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any


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
    ) -> None:
        self.command = command
        self.cwd = cwd
        self.env = {**os.environ, **(env or {})}
        self.timeout = timeout
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._seq = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._write_lock = asyncio.Lock()
        self.events: asyncio.Queue[DAPEvent] = asyncio.Queue()

    async def start(self) -> None:
        if self._process is not None:
            return
        self._process = await asyncio.create_subprocess_exec(
            *self.command,
            cwd=self.cwd,
            env=self.env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._reader_task = asyncio.create_task(self._reader_loop())
        asyncio.create_task(self._drain_stderr())

    async def close(self) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(DAPError("DAP client closed"))
        self._pending.clear()

        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass

        if self._process and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()
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
        try:
            response = await asyncio.wait_for(future, timeout=timeout or self.timeout)
        finally:
            self._pending.pop(seq, None)
        if not response.get("success", False):
            message = response.get("message") or response.get("body", {}).get("error", {}).get("format")
            raise DAPError(str(message or f"DAP request failed: {command}"))
        body = response.get("body", {})
        return body if isinstance(body, dict) else {"value": body}

    async def drain_events(self) -> list[DAPEvent]:
        drained: list[DAPEvent] = []
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
        deadline = timeout if timeout is not None else self.timeout
        try:
            while True:
                event = await asyncio.wait_for(self.events.get(), timeout=deadline)
                if event.event == event_name:
                    return event
        except asyncio.TimeoutError:
            return None

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
                        return
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
            await self.events.put(DAPEvent(event=event, body=body if isinstance(body, dict) else {}))

    async def _drain_stderr(self) -> None:
        if not self._process or not self._process.stderr:
            return
        while True:
            line = await self._process.stderr.readline()
            if not line:
                return
