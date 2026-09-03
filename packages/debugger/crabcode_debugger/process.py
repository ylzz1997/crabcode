"""Process-level debugging and diagnostics helpers."""

from __future__ import annotations

import asyncio
import os
import platform
import shutil
import signal
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from crabcode_core.subprocess_utils import (
    decode_subprocess_output,
    powershell_command,
    subprocess_group_options,
    terminate_process_tree,
)
from crabcode_debugger.memory import MemoryInspector


@dataclass
class ProcessInfo:
    pid: int
    ppid: int | None
    name: str
    command: str
    status: str | None = None
    cwd: str | None = None
    language_guess: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "ppid": self.ppid,
            "name": self.name,
            "command": self.command,
            "status": self.status,
            "cwd": self.cwd,
            "language_guess": self.language_guess,
        }


def _guess_language(command: str) -> str | None:
    lower = command.lower()
    markers = [
        ("python", "python"),
        ("debugpy", "python"),
        ("go test", "go"),
        ("dlv", "go"),
        ("java", "java"),
        ("node", "javascript"),
        ("deno", "typescript"),
        ("bun", "javascript"),
        ("cargo", "rust"),
        ("rust", "rust"),
        ("gdb", "cpp"),
        ("lldb", "cpp"),
    ]
    for marker, language in markers:
        if marker in lower:
            return language
    for suffix, language in {
        ".py": "python",
        ".go": "go",
        ".java": "java",
        ".js": "javascript",
        ".mjs": "javascript",
        ".cjs": "javascript",
        ".ts": "typescript",
        ".rs": "rust",
        ".cpp": "cpp",
        ".cc": "cpp",
        ".cxx": "cpp",
    }.items():
        if suffix in lower:
            return language
    return None


async def _run(
    command: list[str],
    *,
    cwd: str | None = None,
    timeout: float = 10,
) -> tuple[int, str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *command,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **subprocess_group_options(),
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return (
            proc.returncode or 0,
            decode_subprocess_output(stdout_bytes),
            decode_subprocess_output(stderr_bytes),
        )
    except asyncio.TimeoutError:
        await terminate_process_tree(proc)
        return -1, "", f"command timed out after {timeout}s"
    except FileNotFoundError:
        return -1, "", f"command not found: {command[0]}"
    except Exception as exc:
        return -1, "", str(exc)


class ProcessInspector:
    """Best-effort process inspection across macOS, Linux, and Windows."""

    def __init__(self, *, cwd: str, config: dict[str, Any] | None = None) -> None:
        self.cwd = cwd
        self.config = config or {}
        self.timeout = float(self.config.get("default_timeout_seconds", 15))
        self.dump_dir = Path(
            self.config.get("dump_dir")
            or Path(cwd) / ".crabcode" / "debugger" / "dumps"
        )
        self.memory = MemoryInspector(config=self.config)

    async def close(self) -> None:
        await self.memory.close()

    def capabilities(self) -> dict[str, Any]:
        system = platform.system().lower()
        memory_capabilities = self.memory.capabilities()
        tools = {
            name: shutil.which(name)
            for name in [
                "lldb",
                "lldb-dap",
                "gdb",
                "gcore",
                "strace",
                "perf",
                "pstack",
                "sample",
                "vmmap",
                "procdump",
                "cdb",
                "powershell",
                "pwsh",
                "taskkill",
            ]
        }
        actions = {
            "list_processes": True,
            "inspect_process": True,
            "attach_debugger": True,
            "sample_stack": bool(
                (system == "darwin" and tools.get("sample"))
                or (system == "linux" and (tools.get("gdb") or tools.get("pstack")))
                or (system == "windows" and (tools.get("cdb") or tools.get("procdump")))
            ),
            "dump_core": bool(
                (system == "darwin" and tools.get("lldb"))
                or (system == "linux" and (tools.get("gcore") or tools.get("gdb")))
                or (system == "windows" and tools.get("procdump"))
            ),
            "memory_maps": bool(
                system == "linux"
                or (system == "darwin" and tools.get("vmmap"))
            ),
            "trace_syscalls": bool(system == "linux" and tools.get("strace")),
            "memory_regions": bool(memory_capabilities.get("memory_regions")),
            "memory_read": bool(memory_capabilities.get("memory_read")),
            "memory_search": bool(memory_capabilities.get("memory_search")),
            "memory_refine": bool(memory_capabilities.get("memory_refine")),
            "memory_write": bool(memory_capabilities.get("memory_write")),
            "memory_freeze": bool(memory_capabilities.get("memory_freeze")),
            "memory_unfreeze": bool(memory_capabilities.get("memory_unfreeze")),
            "memory_freezes": bool(memory_capabilities.get("memory_freezes")),
            "aob_scan": bool(memory_capabilities.get("aob_scan")),
            "pointer_scan": bool(memory_capabilities.get("pointer_scan")),
            "pointer_resolve": bool(memory_capabilities.get("pointer_resolve")),
            "code_read": bool(memory_capabilities.get("code_read")),
            "code_patch": bool(memory_capabilities.get("code_patch")),
            "code_restore": bool(memory_capabilities.get("code_restore")),
            "code_patches": bool(memory_capabilities.get("code_patches")),
            "terminate": True,
            "kill": True,
        }
        return {
            "platform": {
                "system": platform.system(),
                "release": platform.release(),
                "machine": platform.machine(),
            },
            "tools": tools,
            "actions": actions,
            "memory": memory_capabilities,
        }

    async def list_processes(self, *, limit: int = 100, query: str | None = None) -> dict[str, Any]:
        system = platform.system().lower()
        if system == "windows":
            processes = await self._list_windows()
        else:
            processes = await self._list_posix()
        if query:
            q = query.lower()
            processes = [p for p in processes if q in p.command.lower() or q in p.name.lower()]
        return {"processes": [p.to_dict() for p in processes[: max(1, limit)]]}

    async def inspect_process(self, pid: int) -> dict[str, Any]:
        system = platform.system().lower()
        if system == "windows":
            return await self._inspect_windows(pid)
        return await self._inspect_posix(pid)

    async def sample_stack(self, pid: int, *, duration_seconds: int = 3) -> dict[str, Any]:
        system = platform.system().lower()
        duration = max(1, min(int(duration_seconds), 30))
        if system == "darwin":
            code, stdout, stderr = await _run(["sample", str(pid), str(duration)], timeout=duration + 10)
            return self._command_result("sample_stack", code, stdout, stderr)
        if system == "linux":
            if shutil.which("gdb"):
                code, stdout, stderr = await _run(
                    ["gdb", "-batch", "-ex", "thread apply all bt", "-p", str(pid)],
                    timeout=max(duration + 10, self.timeout),
                )
                return self._command_result("sample_stack", code, stdout, stderr)
            if shutil.which("pstack"):
                code, stdout, stderr = await _run(["pstack", str(pid)], timeout=self.timeout)
                return self._command_result("sample_stack", code, stdout, stderr)
        if system == "windows" and shutil.which("cdb"):
            code, stdout, stderr = await _run(
                ["cdb", "-pv", "-p", str(pid), "-c", "~*kb;q"],
                timeout=self.timeout,
            )
            return self._command_result("sample_stack", code, stdout, stderr)
        return {"error": f"sample_stack unavailable on {platform.system()} with current tools"}

    async def dump_core(self, pid: int, *, output_path: str | None = None) -> dict[str, Any]:
        system = platform.system().lower()
        self.dump_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        if output_path:
            target = Path(output_path)
            if not target.is_absolute():
                target = Path(self.cwd) / target
        else:
            suffix = ".dmp" if system == "windows" else ".core"
            target = self.dump_dir / f"process-{pid}-{timestamp}{suffix}"

        if system == "darwin" and shutil.which("lldb"):
            code, stdout, stderr = await _run(
                [
                    "lldb",
                    "-b",
                    "-p",
                    str(pid),
                    "-o",
                    f"process save-core {target}",
                    "-o",
                    "detach",
                    "-o",
                    "quit",
                ],
                timeout=max(self.timeout, 30),
            )
            return {**self._command_result("dump_core", code, stdout, stderr), "path": str(target)}
        if system == "linux":
            if shutil.which("gcore"):
                prefix = str(target.with_suffix(""))
                code, stdout, stderr = await _run(["gcore", "-o", prefix, str(pid)], timeout=max(self.timeout, 30))
                return {**self._command_result("dump_core", code, stdout, stderr), "path_prefix": prefix}
            if shutil.which("gdb"):
                code, stdout, stderr = await _run(
                    ["gdb", "-batch", "-ex", f"gcore {target}", "-ex", "detach", "-p", str(pid)],
                    timeout=max(self.timeout, 30),
                )
                return {**self._command_result("dump_core", code, stdout, stderr), "path": str(target)}
        if system == "windows" and shutil.which("procdump"):
            code, stdout, stderr = await _run(["procdump", "-ma", str(pid), str(target)], timeout=max(self.timeout, 30))
            return {**self._command_result("dump_core", code, stdout, stderr), "path": str(target)}
        return {"error": f"dump_core unavailable on {platform.system()} with current tools"}

    async def memory_maps(self, pid: int) -> dict[str, Any]:
        system = platform.system().lower()
        if system == "linux":
            path = Path(f"/proc/{pid}/maps")
            try:
                text = path.read_text(errors="replace")
            except OSError as exc:
                return {"error": str(exc)}
            return {"pid": pid, "maps": text}
        if system == "darwin" and shutil.which("vmmap"):
            code, stdout, stderr = await _run(["vmmap", str(pid)], timeout=self.timeout)
            return self._command_result("memory_maps", code, stdout, stderr)
        return {"error": f"memory_maps unavailable on {platform.system()} with current tools"}

    async def memory_regions(
        self,
        pid: int,
        *,
        readable: bool = False,
        writable: bool = False,
        executable: bool | None = None,
        limit: int = 500,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self.memory.regions,
            pid,
            readable=readable,
            writable=writable,
            executable=executable,
            limit=limit,
        )

    async def memory_read(self, pid: int, *, address: int | str, size: int) -> dict[str, Any]:
        return self.memory.read(pid, address=address, size=size)

    async def memory_search(
        self,
        pid: int,
        *,
        value_type: str,
        value: Any = None,
        value_hex: str | None = None,
        endian: str = "little",
        writable_only: bool = True,
        max_results: int | None = None,
        max_scan_bytes: int | None = None,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self.memory.search,
            pid,
            value_type=value_type,
            value=value,
            value_hex=value_hex,
            endian=endian,
            writable_only=writable_only,
            max_results=max_results,
            max_scan_bytes=max_scan_bytes,
        )

    async def memory_refine(
        self,
        search_id: str,
        *,
        comparison: str = "changed",
        value: Any = None,
        value_hex: str | None = None,
        max_results: int | None = None,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self.memory.refine,
            search_id,
            comparison=comparison,
            value=value,
            value_hex=value_hex,
            max_results=max_results,
        )

    async def memory_write(
        self,
        pid: int,
        *,
        address: int | str,
        value_type: str,
        value: Any = None,
        value_hex: str | None = None,
        endian: str = "little",
    ) -> dict[str, Any]:
        return self.memory.write(
            pid,
            address=address,
            value_type=value_type,
            value=value,
            value_hex=value_hex,
            endian=endian,
        )

    async def memory_freeze(
        self,
        pid: int,
        *,
        address: int | str,
        value_type: str,
        value: Any = None,
        value_hex: str | None = None,
        endian: str = "little",
        interval_seconds: float | None = None,
    ) -> dict[str, Any]:
        return await self.memory.freeze(
            pid,
            address=address,
            value_type=value_type,
            value=value,
            value_hex=value_hex,
            endian=endian,
            interval_seconds=interval_seconds,
        )

    async def memory_unfreeze(
        self,
        *,
        freeze_id: str | None = None,
        all_freezes: bool = False,
    ) -> dict[str, Any]:
        return await self.memory.unfreeze(freeze_id, all_freezes=all_freezes)

    async def memory_freezes(self) -> dict[str, Any]:
        return self.memory.freezes()

    async def aob_scan(
        self,
        pid: int,
        *,
        pattern: str,
        executable_only: bool = True,
        writable_only: bool = False,
        module_filter: str | None = None,
        max_results: int | None = None,
        max_scan_bytes: int | None = None,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self.memory.aob_scan,
            pid,
            pattern=pattern,
            executable_only=executable_only,
            writable_only=writable_only,
            module_filter=module_filter,
            max_results=max_results,
            max_scan_bytes=max_scan_bytes,
        )

    async def pointer_scan(
        self,
        pid: int,
        *,
        target_address: int | str,
        max_depth: int = 3,
        max_offset: int = 4096,
        pointer_size: int | None = None,
        align: int | None = None,
        writable_only: bool = True,
        max_results: int | None = None,
        max_scan_bytes: int | None = None,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self.memory.pointer_scan,
            pid,
            target_address=target_address,
            max_depth=max_depth,
            max_offset=max_offset,
            pointer_size=pointer_size,
            align=align,
            writable_only=writable_only,
            max_results=max_results,
            max_scan_bytes=max_scan_bytes,
        )

    async def pointer_resolve(
        self,
        pid: int,
        *,
        base_address: int | str | None = None,
        offsets: list[Any] | None = None,
        module_path: str | None = None,
        module_offset: int | str | None = None,
        pointer_size: int | None = None,
        endian: str = "little",
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self.memory.pointer_resolve,
            pid,
            base_address=base_address,
            offsets=offsets,
            module_path=module_path,
            module_offset=module_offset,
            pointer_size=pointer_size,
            endian=endian,
        )

    async def code_read(self, pid: int, *, address: int | str, size: int) -> dict[str, Any]:
        return self.memory.code_read(pid, address=address, size=size)

    async def code_patch(
        self,
        pid: int,
        *,
        address: int | str,
        patch_hex: str,
        expected_hex: str | None = None,
        patch_id: str | None = None,
    ) -> dict[str, Any]:
        return self.memory.code_patch(
            pid,
            address=address,
            patch_hex=patch_hex,
            expected_hex=expected_hex,
            patch_id=patch_id,
        )

    async def code_restore(
        self,
        *,
        patch_id: str | None = None,
        all_patches: bool = False,
    ) -> dict[str, Any]:
        return self.memory.code_restore(patch_id=patch_id, all_patches=all_patches)

    async def code_patches(self) -> dict[str, Any]:
        return self.memory.code_patches()

    async def trace_syscalls(self, pid: int, *, duration_seconds: int = 5, output_path: str | None = None) -> dict[str, Any]:
        system = platform.system().lower()
        duration = max(1, min(int(duration_seconds), 30))
        if system != "linux" or not shutil.which("strace"):
            return {"error": f"trace_syscalls unavailable on {platform.system()} with current tools"}
        out = Path(output_path) if output_path else self.dump_dir / f"strace-{pid}-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.log"
        if not out.is_absolute():
            out = Path(self.cwd) / out
        out.parent.mkdir(parents=True, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            "strace",
            "-p",
            str(pid),
            "-f",
            "-tt",
            "-T",
            "-o",
            str(out),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            await asyncio.wait_for(proc.communicate(), timeout=duration)
        except asyncio.TimeoutError:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        return {"pid": pid, "path": str(out), "duration_seconds": duration}

    async def signal_process(self, pid: int, *, sig: str) -> dict[str, Any]:
        if isinstance(pid, bool) or pid <= 0:
            return {"pid": pid, "error": "pid must be a positive integer"}
        system = platform.system().lower()
        normalized = sig.lower()
        if system == "windows":
            if normalized in {"terminate", "term", "sigterm"}:
                if shutil.which("taskkill"):
                    code, stdout, stderr = await _run(
                        ["taskkill", "/PID", str(pid), "/T"],
                        timeout=self.timeout,
                    )
                    return self._command_result("terminate", code, stdout, stderr)
                return {"error": "taskkill not found"}
            if shutil.which("taskkill"):
                code, stdout, stderr = await _run(
                    ["taskkill", "/F", "/PID", str(pid), "/T"],
                    timeout=self.timeout,
                )
                return self._command_result("kill", code, stdout, stderr)
            return {"error": "taskkill not found"}

        signum = signal.SIGTERM if normalized in {"terminate", "term", "sigterm"} else signal.SIGKILL
        try:
            os.kill(pid, signum)
            return {"pid": pid, "signal": signum.name, "sent": True}
        except OSError as exc:
            return {"pid": pid, "signal": signum.name, "sent": False, "error": str(exc)}

    async def _list_posix(self) -> list[ProcessInfo]:
        code, stdout, stderr = await _run(["ps", "-axo", "pid=,ppid=,stat=,comm=,args="], timeout=self.timeout)
        if code != 0:
            return [ProcessInfo(pid=-1, ppid=None, name="ps", command=stderr, status="error")]
        processes: list[ProcessInfo] = []
        for line in stdout.splitlines():
            parts = line.strip().split(None, 4)
            if len(parts) < 4:
                continue
            pid_raw, ppid_raw, stat, name = parts[:4]
            args = parts[4] if len(parts) > 4 else name
            try:
                pid = int(pid_raw)
                ppid = int(ppid_raw)
            except ValueError:
                continue
            processes.append(
                ProcessInfo(
                    pid=pid,
                    ppid=ppid,
                    name=name,
                    command=args,
                    status=stat,
                    cwd=self._linux_cwd(pid),
                    language_guess=_guess_language(args),
                )
            )
        return processes

    async def _list_windows(self) -> list[ProcessInfo]:
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if not shell:
            return []
        script = (
            "Get-CimInstance Win32_Process | "
            "Select-Object ProcessId,ParentProcessId,Name,CommandLine | ConvertTo-Json -Compress"
        )
        code, stdout, stderr = await _run(
            powershell_command(shell, script),
            timeout=self.timeout,
        )
        if code != 0:
            return [ProcessInfo(pid=-1, ppid=None, name="powershell", command=stderr, status="error")]
        import json

        try:
            data = json.loads(stdout or "[]")
        except json.JSONDecodeError:
            return []
        if isinstance(data, dict):
            data = [data]
        processes: list[ProcessInfo] = []
        for item in data if isinstance(data, list) else []:
            command = str(item.get("CommandLine") or item.get("Name") or "")
            processes.append(
                ProcessInfo(
                    pid=int(item.get("ProcessId") or 0),
                    ppid=int(item.get("ParentProcessId") or 0),
                    name=str(item.get("Name") or ""),
                    command=command,
                    language_guess=_guess_language(command),
                )
            )
        return processes

    async def _inspect_posix(self, pid: int) -> dict[str, Any]:
        code, stdout, stderr = await _run(
            ["ps", "-p", str(pid), "-o", "pid=,ppid=,stat=,%cpu=,%mem=,etime=,comm=,args="],
            timeout=self.timeout,
        )
        if code != 0:
            return {"pid": pid, "error": stderr.strip() or "process not found"}
        info: dict[str, Any] = {"pid": pid, "ps": stdout.strip()}
        proc_dir = Path(f"/proc/{pid}")
        if proc_dir.exists():
            info["cwd"] = self._linux_cwd(pid)
            try:
                info["cmdline"] = proc_dir.joinpath("cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
            except OSError:
                pass
            try:
                info["open_file_count"] = len(list(proc_dir.joinpath("fd").iterdir()))
            except OSError:
                pass
            try:
                info["status"] = proc_dir.joinpath("status").read_text(errors="replace")
            except OSError:
                pass
        return info

    async def _inspect_windows(self, pid: int) -> dict[str, Any]:
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if not shell:
            return {"pid": pid, "error": "powershell not found"}
        script = (
            f"Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\" | "
            "Select-Object ProcessId,ParentProcessId,Name,CommandLine,ExecutablePath | ConvertTo-Json -Compress"
        )
        code, stdout, stderr = await _run(
            powershell_command(shell, script),
            timeout=self.timeout,
        )
        if code != 0:
            return {"pid": pid, "error": stderr.strip()}
        return {"pid": pid, "process": stdout.strip()}

    @staticmethod
    def _linux_cwd(pid: int) -> str | None:
        link = Path(f"/proc/{pid}/cwd")
        if not link.exists():
            return None
        try:
            return os.readlink(link)
        except OSError:
            return None

    @staticmethod
    def _command_result(action: str, code: int, stdout: str, stderr: str) -> dict[str, Any]:
        return {
            "action": action,
            "exit_code": code,
            "stdout": stdout,
            "stderr": stderr,
            "ok": code == 0,
        }
