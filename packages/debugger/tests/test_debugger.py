from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core"))
sys.path.insert(0, str(ROOT / "debugger"))

from crabcode_core.types.tool import PermissionBehavior, ToolContext

from crabcode_debugger.adapters import AdapterRegistry
from crabcode_debugger.memory import MemoryInspector, decode_value, encode_value
from crabcode_debugger.process import ProcessInspector
from crabcode_debugger.sessions import DebugSessionManager
from crabcode_debugger.tool import ProcessDebuggerTool


FAKE_DAP_ADAPTER = r"""
import json
import sys

seq = 1
pending_launch = None


def read_msg():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        key, value = line.decode().strip().split(":", 1)
        headers[key.lower()] = value.strip()
    body = sys.stdin.buffer.read(int(headers["content-length"]))
    return json.loads(body.decode())


def send(msg):
    body = json.dumps(msg, separators=(",", ":")).encode()
    sys.stdout.buffer.write(b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
    sys.stdout.buffer.flush()


def respond(req, body=None):
    global seq
    send({
        "seq": seq,
        "type": "response",
        "request_seq": req["seq"],
        "success": True,
        "command": req["command"],
        "body": body or {},
    })
    seq += 1


def event(name, body=None):
    global seq
    send({"seq": seq, "type": "event", "event": name, "body": body or {}})
    seq += 1


while True:
    req = read_msg()
    if req is None:
        break
    cmd = req.get("command")
    if cmd == "initialize":
        respond(req, {"supportsConfigurationDoneRequest": True})
    elif cmd == "launch":
        pending_launch = req
        event("initialized")
    elif cmd == "setBreakpoints":
        respond(req, {"breakpoints": [{"verified": True, "line": 1}]})
    elif cmd == "configurationDone":
        respond(req)
        if pending_launch:
            respond(pending_launch)
            event("stopped", {"threadId": 7, "reason": "breakpoint"})
    elif cmd == "threads":
        respond(req, {"threads": [{"id": 7, "name": "main"}]})
    elif cmd == "stackTrace":
        respond(req, {
            "stackFrames": [{"id": 1, "name": "main", "line": 1, "column": 1}],
            "totalFrames": 1,
        })
    elif cmd == "disconnect":
        respond(req)
        break
    else:
        respond(req)
"""


class AdapterRegistryTests(unittest.TestCase):
    def test_configured_adapter_is_preferred(self) -> None:
        registry = AdapterRegistry(
            {
                "adapters": {
                    "python": {
                        "command": [sys.executable, "-c", "pass"],
                        "adapter_id": "test-python",
                    }
                }
            }
        )

        adapter = registry.resolve("python")

        self.assertIsNotNone(adapter)
        assert adapter is not None
        self.assertEqual(adapter.adapter_id, "test-python")


class DebugSessionManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_launch_can_wait_for_configuration_done(self) -> None:
        manager = DebugSessionManager(
            cwd=str(Path.cwd()),
            config={
                "adapters": {
                    "python": {
                        "command": [sys.executable, "-c", FAKE_DAP_ADAPTER],
                        "adapter_id": "fake",
                    }
                }
            },
        )

        started = await manager.start(language="python", program="example.py")
        session_id = started["session_id"]
        breakpoints = await manager.set_breakpoints(session_id, path="example.py", lines=[1])
        done = await manager.configuration_done(session_id)
        threads = await manager.threads(session_id)
        stack = await manager.stack(session_id)
        await manager.stop(session_id)

        self.assertTrue(started["session_id"])
        self.assertTrue(breakpoints["breakpoints"][0]["verified"])
        self.assertEqual(done["events"][0]["event"], "stopped")
        self.assertEqual(threads["threads"][0]["id"], 7)
        self.assertEqual(stack["totalFrames"], 1)


class ProcessInspectorTests(unittest.TestCase):
    def test_capabilities_reports_platform(self) -> None:
        capabilities = ProcessInspector(cwd=str(Path.cwd())).capabilities()

        self.assertIn("platform", capabilities)
        self.assertIn("actions", capabilities)
        self.assertTrue(capabilities["actions"]["list_processes"])


class MemoryEncodingTests(unittest.TestCase):
    def test_numeric_values_round_trip(self) -> None:
        encoded = encode_value("uint32", value="0x12345678")

        self.assertEqual(encoded.hex(), "78563412")
        self.assertEqual(decode_value("uint32", encoded), 0x12345678)

    def test_bytes_value_accepts_spaced_hex(self) -> None:
        encoded = encode_value("bytes", value="0Xde ad be ef")

        self.assertEqual(encoded, bytes.fromhex("deadbeef"))

    def test_string_value_uses_utf8(self) -> None:
        encoded = encode_value("string", value="needle")

        self.assertEqual(encoded, b"needle")


class MemoryFreezeTests(unittest.IsolatedAsyncioTestCase):
    async def test_freeze_rewrites_until_unfreeze(self) -> None:
        memory = MemoryInspector(
            config={
                "memory_freeze_interval_seconds": 0.01,
                "min_memory_freeze_interval_seconds": 0.001,
            }
        )
        writes: list[bytes] = []
        memory._read_memory = lambda pid, address, size: b"\x00" * size  # type: ignore[method-assign]
        memory._write_memory = lambda pid, address, data: writes.append(data)  # type: ignore[method-assign]

        frozen = await memory.freeze(
            123,
            address="0x10",
            value_type="uint8",
            value=7,
        )
        await asyncio.sleep(0.03)
        freezes = memory.freezes()
        stopped = await memory.unfreeze(frozen["freeze_id"])

        self.assertEqual(freezes["freezes"][0]["freeze_id"], frozen["freeze_id"])
        self.assertGreaterEqual(len(writes), 2)
        self.assertEqual(stopped["stopped"][0]["freeze_id"], frozen["freeze_id"])


class PermissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_process_actions_default_to_ask(self) -> None:
        tool = ProcessDebuggerTool()
        context = ToolContext(cwd=str(Path.cwd()), env={}, tool_config={})
        await tool.setup(context)

        perm = await tool.check_permissions({"action": "list_processes"}, context)

        self.assertEqual(perm.behavior, PermissionBehavior.ASK)

    async def test_capabilities_is_allowed(self) -> None:
        tool = ProcessDebuggerTool()
        context = ToolContext(cwd=str(Path.cwd()), env={}, tool_config={})
        await tool.setup(context)

        perm = await tool.check_permissions({"action": "capabilities"}, context)

        self.assertEqual(perm.behavior, PermissionBehavior.ALLOW)


if __name__ == "__main__":
    unittest.main()
