from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core"))
sys.path.insert(0, str(ROOT / "debugger"))

from crabcode_core.types.tool import PermissionBehavior, ToolContext

from crabcode_debugger.adapters import AdapterRegistry
from crabcode_debugger.memory import (
    MemoryRegion,
    MemoryInspector,
    decode_value,
    encode_value,
    find_aob_matches,
    parse_aob_pattern,
)
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

    def test_aob_pattern_supports_wildcards(self) -> None:
        pattern = parse_aob_pattern("48 8B ?? 89")

        self.assertEqual(pattern, [0x48, 0x8B, None, 0x89])
        self.assertEqual(find_aob_matches(bytes.fromhex("90488bff8990"), pattern), [1])


class MemoryBackendTests(unittest.TestCase):
    def test_capabilities_follow_selected_backend(self) -> None:
        memory = MemoryInspector()

        class FakeBackend:
            available = True
            name = "fake-native"
            note = "fake backend"

        memory.backend = FakeBackend()  # type: ignore[assignment]

        capabilities = memory.capabilities()

        self.assertTrue(capabilities["memory_read"])
        self.assertTrue(capabilities["code_patch"])
        self.assertEqual(capabilities["backend"], "fake-native")
        self.assertEqual(capabilities["backend_note"], "fake backend")


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

    async def test_code_patch_and_restore(self) -> None:
        memory = MemoryInspector()
        storage = {0x10: bytearray(bytes.fromhex("9090"))}

        def read_memory(pid: int, address: int, size: int) -> bytes:
            return bytes(storage[address][:size])

        def write_memory(pid: int, address: int, data: bytes) -> None:
            storage[address][: len(data)] = data

        memory._read_memory = read_memory  # type: ignore[method-assign]
        memory._write_memory = write_memory  # type: ignore[method-assign]

        patched = memory.code_patch(123, address="0x10", expected_hex="9090", patch_hex="9091")
        restored = memory.code_restore(patch_id=patched["patch_id"])

        self.assertTrue(patched["verified"])
        self.assertEqual(storage[0x10], bytearray(bytes.fromhex("9090")))
        self.assertTrue(restored["restored"][0]["restored"])

    async def test_code_patch_uses_memory_protection_when_available(self) -> None:
        memory = MemoryInspector()
        storage = {0x10: bytearray(bytes.fromhex("9090"))}
        protect_calls: list[tuple[int, int, int, bool, bool]] = []
        restore_calls: list[tuple[int, int, int, str]] = []
        flush_calls: list[tuple[int, int, int]] = []

        def read_memory(pid: int, address: int, size: int) -> bytes:
            return bytes(storage[address][:size])

        def write_memory(pid: int, address: int, data: bytes) -> None:
            storage[address][: len(data)] = data

        def protect_memory(pid: int, address: int, size: int, *, writable: bool, executable: bool) -> str:
            protect_calls.append((pid, address, size, writable, executable))
            return "old-protection"

        def restore_protection(pid: int, address: int, size: int, token: str) -> None:
            restore_calls.append((pid, address, size, token))

        def flush_instruction_cache(pid: int, address: int, size: int) -> None:
            flush_calls.append((pid, address, size))

        memory._read_memory = read_memory  # type: ignore[method-assign]
        memory._write_memory = write_memory  # type: ignore[method-assign]
        memory._protect_memory = protect_memory  # type: ignore[method-assign]
        memory._restore_protection = restore_protection  # type: ignore[method-assign]
        memory._flush_instruction_cache = flush_instruction_cache  # type: ignore[method-assign]

        patched = memory.code_patch(123, address="0x10", expected_hex="9090", patch_hex="9091")

        self.assertTrue(patched["verified"])
        self.assertEqual(protect_calls, [(123, 0x10, 2, True, True)])
        self.assertEqual(restore_calls, [(123, 0x10, 2, "old-protection")])
        self.assertEqual(flush_calls, [(123, 0x10, 2)])

    async def test_pointer_resolve_reads_chain(self) -> None:
        memory = MemoryInspector()
        values = {
            0x1000: (0x2000).to_bytes(8, "little"),
            0x2010: (0x3000).to_bytes(8, "little"),
        }
        memory._read_memory = lambda pid, address, size: values[address][:size]  # type: ignore[method-assign]

        with patch("crabcode_debugger.memory.platform.system", return_value="Linux"):
            resolved = memory.pointer_resolve(
                123,
                base_address="0x1000",
                offsets=["0x10", "0x20"],
                pointer_size=8,
            )

        self.assertEqual(resolved["address"], "0x3020")

    async def test_pointer_scan_finds_single_level_chain(self) -> None:
        memory = MemoryInspector(config={"max_pointer_scan_results": 10})
        region = MemoryRegion(
            start=0x1000,
            end=0x1010,
            permissions="rw-p",
            offset=0,
            device="00:00",
            inode="0",
            path="[heap]",
        )
        memory._linux_regions = lambda pid: [region]  # type: ignore[method-assign]
        memory._read_memory = lambda pid, address, size: (0x2000).to_bytes(8, "little") + b"\x00" * 8  # type: ignore[method-assign]

        with patch("crabcode_debugger.memory.platform.system", return_value="Linux"):
            result = memory.pointer_scan(
                123,
                target_address="0x2010",
                max_depth=1,
                max_offset=0x20,
                pointer_size=8,
                max_scan_bytes=16,
            )

        self.assertEqual(result["result_count"], 1)
        self.assertEqual(result["chains"][0]["offsets"], ["0x10"])


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
