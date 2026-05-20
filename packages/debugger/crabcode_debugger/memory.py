"""Process memory inspection helpers."""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.wintypes
import platform
import re
import struct
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_NUMERIC_FORMATS: dict[str, str] = {
    "int8": "b",
    "uint8": "B",
    "int16": "h",
    "uint16": "H",
    "int32": "i",
    "uint32": "I",
    "int64": "q",
    "uint64": "Q",
    "float32": "f",
    "float64": "d",
}


class MemoryBackend:
    name = "unavailable"
    available = False
    note = "direct memory backend unavailable"

    def regions(self, pid: int) -> list["MemoryRegion"]:
        raise RuntimeError(self.note)

    def read(self, pid: int, address: int, size: int) -> bytes:
        raise RuntimeError(self.note)

    def write(self, pid: int, address: int, data: bytes) -> None:
        raise RuntimeError(self.note)

    def protect(self, pid: int, address: int, size: int, writable: bool, executable: bool) -> Any:
        raise RuntimeError("memory protection changes are unavailable on this backend")

    def restore_protection(self, pid: int, address: int, size: int, token: Any) -> None:
        return None

    def flush_instruction_cache(self, pid: int, address: int, size: int) -> None:
        return None


class LinuxProcfsMemoryBackend(MemoryBackend):
    name = "linux-procfs"
    available = True
    note = "Linux /proc/<pid>/mem backend"

    def regions(self, pid: int) -> list["MemoryRegion"]:
        maps = Path(f"/proc/{pid}/maps")
        text = maps.read_text(errors="replace")
        regions: list[MemoryRegion] = []
        for line in text.splitlines():
            parts = line.split(maxsplit=5)
            if len(parts) < 5:
                continue
            start_raw, end_raw = parts[0].split("-", 1)
            path = parts[5] if len(parts) > 5 else ""
            regions.append(
                MemoryRegion(
                    start=int(start_raw, 16),
                    end=int(end_raw, 16),
                    permissions=parts[1],
                    offset=int(parts[2], 16),
                    device=parts[3],
                    inode=parts[4],
                    path=path,
                )
            )
        return regions

    def read(self, pid: int, address: int, size: int) -> bytes:
        with open(f"/proc/{pid}/mem", "rb", buffering=0) as handle:
            handle.seek(address)
            return handle.read(size)

    def write(self, pid: int, address: int, data: bytes) -> None:
        with open(f"/proc/{pid}/mem", "r+b", buffering=0) as handle:
            handle.seek(address)
            handle.write(data)

    def protect(self, pid: int, address: int, size: int, writable: bool, executable: bool) -> Any:
        return None

    def restore_protection(self, pid: int, address: int, size: int, token: Any) -> None:
        return None


class WindowsMemoryBackend(MemoryBackend):
    name = "windows-kernel32"
    note = "Windows kernel32 ReadProcessMemory/WriteProcessMemory backend"

    PROCESS_QUERY_INFORMATION = 0x0400
    PROCESS_VM_OPERATION = 0x0008
    PROCESS_VM_READ = 0x0010
    PROCESS_VM_WRITE = 0x0020
    MEM_COMMIT = 0x1000
    PAGE_NOACCESS = 0x01
    PAGE_READONLY = 0x02
    PAGE_READWRITE = 0x04
    PAGE_WRITECOPY = 0x08
    PAGE_EXECUTE = 0x10
    PAGE_EXECUTE_READ = 0x20
    PAGE_EXECUTE_READWRITE = 0x40
    PAGE_EXECUTE_WRITECOPY = 0x80
    PAGE_GUARD = 0x100

    def __init__(self) -> None:
        self.available = platform.system().lower() == "windows"
        if not self.available:
            return
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._configure_api()

    def _configure_api(self) -> None:
        wintypes = ctypes.wintypes
        self.kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel32.OpenProcess.restype = wintypes.HANDLE
        self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel32.CloseHandle.restype = wintypes.BOOL
        self.kernel32.ReadProcessMemory.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        self.kernel32.ReadProcessMemory.restype = wintypes.BOOL
        self.kernel32.WriteProcessMemory.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        self.kernel32.WriteProcessMemory.restype = wintypes.BOOL
        self.kernel32.VirtualProtectEx.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            ctypes.c_size_t,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.kernel32.VirtualProtectEx.restype = wintypes.BOOL
        self.kernel32.VirtualQueryEx.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
        ]
        self.kernel32.VirtualQueryEx.restype = ctypes.c_size_t
        self.kernel32.FlushInstructionCache.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            ctypes.c_size_t,
        ]
        self.kernel32.FlushInstructionCache.restype = wintypes.BOOL

    def _open(self, pid: int, access: int) -> Any:
        handle = self.kernel32.OpenProcess(access, False, pid)
        if not handle:
            error_code = ctypes.get_last_error()
            raise OSError(
                error_code,
                f"OpenProcess failed for pid {pid}; try running CrabCode as Administrator, "
                "ensure the target process is not running with higher privileges, and note that "
                "protected/system processes may still deny memory access",
            )
        return handle

    def regions(self, pid: int) -> list["MemoryRegion"]:
        class MEMORY_BASIC_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BaseAddress", ctypes.c_void_p),
                ("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", ctypes.c_ulong),
                ("PartitionId", ctypes.c_ushort),
                ("RegionSize", ctypes.c_size_t),
                ("State", ctypes.c_ulong),
                ("Protect", ctypes.c_ulong),
                ("Type", ctypes.c_ulong),
            ]

        regions: list[MemoryRegion] = []
        handle = self._open(pid, self.PROCESS_QUERY_INFORMATION | self.PROCESS_VM_READ)
        try:
            address = 0
            max_address = (1 << (ctypes.sizeof(ctypes.c_void_p) * 8)) - 1
            mbi = MEMORY_BASIC_INFORMATION()
            mbi_size = ctypes.sizeof(mbi)
            while address < max_address:
                result = self.kernel32.VirtualQueryEx(
                    handle,
                    ctypes.c_void_p(address),
                    ctypes.byref(mbi),
                    mbi_size,
                )
                if not result:
                    break
                base = int(mbi.BaseAddress or 0)
                size = int(mbi.RegionSize or 0)
                if size <= 0:
                    address += 0x1000
                    continue
                if int(mbi.State) == self.MEM_COMMIT:
                    regions.append(
                        MemoryRegion(
                            start=base,
                            end=base + size,
                            permissions=self._protection_to_permissions(int(mbi.Protect)),
                            offset=0,
                            device="",
                            inode="",
                            path="",
                        )
                    )
                next_address = base + size
                if next_address <= address:
                    break
                address = next_address
        finally:
            self.kernel32.CloseHandle(handle)
        return regions

    def read(self, pid: int, address: int, size: int) -> bytes:
        handle = self._open(pid, self.PROCESS_VM_READ | self.PROCESS_QUERY_INFORMATION)
        try:
            buffer = ctypes.create_string_buffer(size)
            read = ctypes.c_size_t(0)
            ok = self.kernel32.ReadProcessMemory(
                handle,
                ctypes.c_void_p(address),
                buffer,
                size,
                ctypes.byref(read),
            )
            if not ok:
                raise OSError(ctypes.get_last_error(), f"ReadProcessMemory failed at {hex(address)}")
            return buffer.raw[: read.value]
        finally:
            self.kernel32.CloseHandle(handle)

    def write(self, pid: int, address: int, data: bytes) -> None:
        handle = self._open(
            pid,
            self.PROCESS_VM_WRITE | self.PROCESS_VM_OPERATION | self.PROCESS_QUERY_INFORMATION,
        )
        try:
            buffer = ctypes.create_string_buffer(data)
            written = ctypes.c_size_t(0)
            ok = self.kernel32.WriteProcessMemory(
                handle,
                ctypes.c_void_p(address),
                buffer,
                len(data),
                ctypes.byref(written),
            )
            if not ok or written.value != len(data):
                raise OSError(ctypes.get_last_error(), f"WriteProcessMemory failed at {hex(address)}")
        finally:
            self.kernel32.CloseHandle(handle)

    def protect(self, pid: int, address: int, size: int, writable: bool, executable: bool) -> Any:
        handle = self._open(pid, self.PROCESS_VM_OPERATION | self.PROCESS_QUERY_INFORMATION)
        new_protect = self.PAGE_EXECUTE_READWRITE if executable else self.PAGE_READWRITE
        old_protect = ctypes.wintypes.DWORD(0)
        try:
            ok = self.kernel32.VirtualProtectEx(
                handle,
                ctypes.c_void_p(address),
                size,
                new_protect,
                ctypes.byref(old_protect),
            )
            if not ok:
                raise OSError(ctypes.get_last_error(), f"VirtualProtectEx failed at {hex(address)}")
            return int(old_protect.value)
        finally:
            self.kernel32.CloseHandle(handle)

    def restore_protection(self, pid: int, address: int, size: int, token: Any) -> None:
        if token is None:
            return
        handle = self._open(pid, self.PROCESS_VM_OPERATION | self.PROCESS_QUERY_INFORMATION)
        old_protect = ctypes.wintypes.DWORD(0)
        try:
            self.kernel32.VirtualProtectEx(
                handle,
                ctypes.c_void_p(address),
                size,
                int(token),
                ctypes.byref(old_protect),
            )
        finally:
            self.kernel32.CloseHandle(handle)

    def flush_instruction_cache(self, pid: int, address: int, size: int) -> None:
        handle = self._open(pid, self.PROCESS_VM_OPERATION | self.PROCESS_QUERY_INFORMATION)
        try:
            ok = self.kernel32.FlushInstructionCache(handle, ctypes.c_void_p(address), size)
            if not ok:
                raise OSError(ctypes.get_last_error(), f"FlushInstructionCache failed at {hex(address)}")
        finally:
            self.kernel32.CloseHandle(handle)

    def _protection_to_permissions(self, protect: int) -> str:
        if protect & self.PAGE_GUARD or protect == self.PAGE_NOACCESS:
            return "---p"
        readable = protect in {
            self.PAGE_READONLY,
            self.PAGE_READWRITE,
            self.PAGE_WRITECOPY,
            self.PAGE_EXECUTE_READ,
            self.PAGE_EXECUTE_READWRITE,
            self.PAGE_EXECUTE_WRITECOPY,
        }
        writable = protect in {
            self.PAGE_READWRITE,
            self.PAGE_WRITECOPY,
            self.PAGE_EXECUTE_READWRITE,
            self.PAGE_EXECUTE_WRITECOPY,
        }
        executable = protect in {
            self.PAGE_EXECUTE,
            self.PAGE_EXECUTE_READ,
            self.PAGE_EXECUTE_READWRITE,
            self.PAGE_EXECUTE_WRITECOPY,
        }
        return (
            ("r" if readable else "-")
            + ("w" if writable else "-")
            + ("x" if executable else "-")
            + "p"
        )


class MacOSMachMemoryBackend(MemoryBackend):
    name = "macos-mach"
    note = "macOS Mach task memory backend"

    KERN_SUCCESS = 0
    VM_PROT_READ = 0x01
    VM_PROT_WRITE = 0x02
    VM_PROT_EXECUTE = 0x04

    def __init__(self) -> None:
        self.available = platform.system().lower() == "darwin"
        if not self.available:
            return
        self.lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        self._configure_api()

    def _configure_api(self) -> None:
        self.lib.mach_task_self.restype = ctypes.c_uint32
        self.lib.task_for_pid.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.POINTER(ctypes.c_uint32)]
        self.lib.task_for_pid.restype = ctypes.c_int
        self.lib.mach_port_deallocate.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
        self.lib.mach_vm_read_overwrite.argtypes = [
            ctypes.c_uint32,
            ctypes.c_uint64,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint64),
        ]
        self.lib.mach_vm_read_overwrite.restype = ctypes.c_int
        self.lib.mach_vm_write.argtypes = [
            ctypes.c_uint32,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        self.lib.mach_vm_write.restype = ctypes.c_int
        self.lib.mach_vm_protect.argtypes = [
            ctypes.c_uint32,
            ctypes.c_uint64,
            ctypes.c_uint64,
            ctypes.c_int,
            ctypes.c_int,
        ]
        self.lib.mach_vm_protect.restype = ctypes.c_int
        self.lib.mach_vm_region_recurse.argtypes = [
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        self.lib.mach_vm_region_recurse.restype = ctypes.c_int

    def _task_for_pid(self, pid: int) -> int:
        task = ctypes.c_uint32(0)
        kr = self.lib.task_for_pid(self.lib.mach_task_self(), int(pid), ctypes.byref(task))
        if kr != self.KERN_SUCCESS:
            raise PermissionError(
                f"task_for_pid failed for pid {pid} with kern_return={kr}; "
                "grant Developer Tools/debugging permission, run with sufficient privileges, "
                "and note that SIP/protected processes may still deny access"
            )
        return int(task.value)

    def _deallocate(self, task: int) -> None:
        try:
            self.lib.mach_port_deallocate(self.lib.mach_task_self(), task)
        except Exception:
            pass

    def regions(self, pid: int) -> list["MemoryRegion"]:
        class VMRegionSubmapInfo64(ctypes.Structure):
            _fields_ = [
                ("protection", ctypes.c_uint32),
                ("max_protection", ctypes.c_uint32),
                ("inheritance", ctypes.c_uint32),
                ("offset", ctypes.c_uint64),
                ("user_tag", ctypes.c_uint32),
                ("pages_resident", ctypes.c_uint32),
                ("pages_shared_now_private", ctypes.c_uint32),
                ("pages_swapped_out", ctypes.c_uint32),
                ("pages_dirtied", ctypes.c_uint32),
                ("ref_count", ctypes.c_uint32),
                ("shadow_depth", ctypes.c_uint16),
                ("external_pager", ctypes.c_uint8),
                ("share_mode", ctypes.c_uint8),
                ("is_submap", ctypes.c_uint32),
                ("behavior", ctypes.c_uint32),
                ("object_id", ctypes.c_uint32),
                ("user_wired_count", ctypes.c_uint16),
                ("pages_reusable", ctypes.c_uint32),
                ("object_id_full", ctypes.c_uint64),
            ]

        task = self._task_for_pid(pid)
        regions: list[MemoryRegion] = []
        try:
            address = ctypes.c_uint64(0)
            size = ctypes.c_uint64(0)
            depth = ctypes.c_uint32(0)
            max_regions = 20000
            for _ in range(max_regions):
                info = VMRegionSubmapInfo64()
                count = ctypes.c_uint32(ctypes.sizeof(info) // ctypes.sizeof(ctypes.c_uint32))
                kr = self.lib.mach_vm_region_recurse(
                    task,
                    ctypes.byref(address),
                    ctypes.byref(size),
                    ctypes.byref(depth),
                    ctypes.byref(info),
                    ctypes.byref(count),
                )
                if kr != self.KERN_SUCCESS:
                    break
                if info.is_submap:
                    depth.value += 1
                    continue
                start = int(address.value)
                region_size = int(size.value)
                regions.append(
                    MemoryRegion(
                        start=start,
                        end=start + region_size,
                        permissions=self._protection_to_permissions(int(info.protection)),
                        offset=int(info.offset),
                        device="",
                        inode="",
                        path="",
                    )
                )
                address.value = start + region_size
        finally:
            self._deallocate(task)
        return regions

    def read(self, pid: int, address: int, size: int) -> bytes:
        task = self._task_for_pid(pid)
        try:
            buffer = ctypes.create_string_buffer(size)
            out_size = ctypes.c_uint64(0)
            kr = self.lib.mach_vm_read_overwrite(
                task,
                ctypes.c_uint64(address),
                ctypes.c_uint64(size),
                buffer,
                ctypes.byref(out_size),
            )
            if kr != self.KERN_SUCCESS:
                raise OSError(f"mach_vm_read_overwrite failed at {hex(address)} with kern_return={kr}")
            return buffer.raw[: int(out_size.value)]
        finally:
            self._deallocate(task)

    def write(self, pid: int, address: int, data: bytes) -> None:
        task = self._task_for_pid(pid)
        try:
            buffer = ctypes.create_string_buffer(data)
            kr = self.lib.mach_vm_write(
                task,
                ctypes.c_uint64(address),
                buffer,
                ctypes.c_uint32(len(data)),
            )
            if kr != self.KERN_SUCCESS:
                raise OSError(f"mach_vm_write failed at {hex(address)} with kern_return={kr}")
        finally:
            self._deallocate(task)

    def protect(self, pid: int, address: int, size: int, writable: bool, executable: bool) -> Any:
        old_protection = self._protection_for_address(pid, address)
        task = self._task_for_pid(pid)
        protection = self.VM_PROT_READ
        if writable:
            protection |= self.VM_PROT_WRITE
        if executable:
            protection |= self.VM_PROT_EXECUTE
        try:
            kr = self.lib.mach_vm_protect(
                task,
                ctypes.c_uint64(address),
                ctypes.c_uint64(size),
                0,
                protection,
            )
            if kr != self.KERN_SUCCESS:
                raise OSError(f"mach_vm_protect failed at {hex(address)} with kern_return={kr}")
        finally:
            self._deallocate(task)
        return old_protection

    def restore_protection(self, pid: int, address: int, size: int, token: Any) -> None:
        if token is None:
            return
        task = self._task_for_pid(pid)
        try:
            self.lib.mach_vm_protect(
                task,
                ctypes.c_uint64(address),
                ctypes.c_uint64(size),
                0,
                int(token),
            )
        finally:
            self._deallocate(task)

    def _protection_for_address(self, pid: int, address: int) -> int | None:
        for region in self.regions(pid):
            if region.start <= address < region.end:
                protection = 0
                if "r" in region.permissions:
                    protection |= self.VM_PROT_READ
                if "w" in region.permissions:
                    protection |= self.VM_PROT_WRITE
                if "x" in region.permissions:
                    protection |= self.VM_PROT_EXECUTE
                return protection
        return None

    def _protection_to_permissions(self, protection: int) -> str:
        return (
            ("r" if protection & self.VM_PROT_READ else "-")
            + ("w" if protection & self.VM_PROT_WRITE else "-")
            + ("x" if protection & self.VM_PROT_EXECUTE else "-")
            + "p"
        )


def _select_memory_backend() -> MemoryBackend:
    system = platform.system().lower()
    if system == "linux" and Path("/proc").exists():
        return LinuxProcfsMemoryBackend()
    if system == "windows":
        return WindowsMemoryBackend()
    if system == "darwin":
        return MacOSMachMemoryBackend()
    return MemoryBackend()


@dataclass
class MemoryRegion:
    start: int
    end: int
    permissions: str
    offset: int
    device: str
    inode: str
    path: str

    @property
    def size(self) -> int:
        return max(0, self.end - self.start)

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": hex(self.start),
            "end": hex(self.end),
            "size": self.size,
            "permissions": self.permissions,
            "readable": "r" in self.permissions,
            "writable": "w" in self.permissions,
            "executable": "x" in self.permissions,
            "private": "p" in self.permissions,
            "shared": "s" in self.permissions,
            "offset": hex(self.offset),
            "device": self.device,
            "inode": self.inode,
            "path": self.path,
        }


class MemoryInspector:
    """Process memory search/read/write for supported platforms.

    Linux uses /proc/<pid>/maps and /proc/<pid>/mem, Windows uses documented
    kernel32 process-memory APIs, and macOS uses Mach task-memory APIs.
    """

    def __init__(self, *, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self.backend = _select_memory_backend()
        self.max_search_results = int(self.config.get("max_search_results", 100))
        self.max_search_bytes = int(self.config.get("max_search_bytes", 128 * 1024 * 1024))
        self.chunk_size = int(self.config.get("memory_search_chunk_bytes", 1024 * 1024))
        self._searches: dict[str, dict[str, Any]] = {}
        self._freezes: dict[str, dict[str, Any]] = {}
        self._aob_scans: dict[str, dict[str, Any]] = {}
        self._patches: dict[str, dict[str, Any]] = {}

    async def close(self) -> None:
        await self.unfreeze(all_freezes=True)

    def capabilities(self) -> dict[str, bool | str]:
        available = bool(self.backend.available)
        return {
            "memory_regions": available,
            "memory_read": available,
            "memory_search": available,
            "memory_refine": available,
            "memory_write": available,
            "memory_freeze": available,
            "memory_unfreeze": True,
            "memory_freezes": True,
            "aob_scan": available,
            "pointer_scan": available,
            "pointer_resolve": available,
            "code_read": available,
            "code_patch": available,
            "code_restore": True,
            "code_patches": True,
            "backend": self.backend.name,
            "backend_note": self.backend.note,
        }

    def regions(
        self,
        pid: int,
        *,
        readable: bool = False,
        writable: bool = False,
        executable: bool | None = None,
        limit: int = 500,
    ) -> dict[str, Any]:
        if not self.backend.available:
            return {"error": f"memory_regions unavailable on {platform.system()}"}
        try:
            parsed = self._linux_regions(pid)
        except Exception as exc:
            return {"pid": pid, "error": str(exc)}
        filtered: list[MemoryRegion] = []
        for region in parsed:
            if readable and "r" not in region.permissions:
                continue
            if writable and "w" not in region.permissions:
                continue
            if executable is not None and ("x" in region.permissions) != executable:
                continue
            filtered.append(region)
        return {
            "pid": pid,
            "regions": [region.to_dict() for region in filtered[: max(1, limit)]],
            "total_regions": len(filtered),
        }

    def read(self, pid: int, *, address: int | str, size: int) -> dict[str, Any]:
        parsed_address = parse_address(address)
        read_size = max(1, min(int(size), int(self.config.get("max_memory_read_bytes", 4096))))
        try:
            data = self._read_memory(pid, parsed_address, read_size)
        except Exception as exc:
            return {"pid": pid, "address": hex(parsed_address), "error": str(exc)}
        return {
            "pid": pid,
            "address": hex(parsed_address),
            "size": len(data),
            "hex": data.hex(),
            "ascii": _ascii_preview(data),
        }

    def write(
        self,
        pid: int,
        *,
        address: int | str,
        value_type: str,
        value: Any = None,
        value_hex: str | None = None,
        endian: str = "little",
    ) -> dict[str, Any]:
        parsed_address = parse_address(address)
        new_bytes = encode_value(value_type, value=value, value_hex=value_hex, endian=endian)
        max_write = int(self.config.get("max_memory_write_bytes", 64))
        if len(new_bytes) > max_write:
            return {
                "pid": pid,
                "address": hex(parsed_address),
                "error": f"write size {len(new_bytes)} exceeds max_memory_write_bytes={max_write}",
            }
        try:
            old_bytes = self._read_memory(pid, parsed_address, len(new_bytes))
            self._write_memory(pid, parsed_address, new_bytes)
            verify = self._read_memory(pid, parsed_address, len(new_bytes))
        except Exception as exc:
            return {"pid": pid, "address": hex(parsed_address), "error": str(exc)}
        return {
            "pid": pid,
            "address": hex(parsed_address),
            "value_type": value_type,
            "old_hex": old_bytes.hex(),
            "written_hex": new_bytes.hex(),
            "verify_hex": verify.hex(),
            "verified": verify == new_bytes,
        }

    async def freeze(
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
        max_freezes = int(self.config.get("max_memory_freezes", 32))
        if len(self._freezes) >= max_freezes:
            return {"error": f"max_memory_freezes reached: {max_freezes}"}

        parsed_address = parse_address(address)
        data = encode_value(value_type, value=value, value_hex=value_hex, endian=endian)
        max_write = int(self.config.get("max_memory_write_bytes", 64))
        if len(data) > max_write:
            return {
                "pid": pid,
                "address": hex(parsed_address),
                "error": f"freeze write size {len(data)} exceeds max_memory_write_bytes={max_write}",
            }

        try:
            old_bytes = self._read_memory(pid, parsed_address, len(data))
            self._write_memory(pid, parsed_address, data)
            verify = self._read_memory(pid, parsed_address, len(data))
        except Exception as exc:
            return {"pid": pid, "address": hex(parsed_address), "error": str(exc)}

        min_interval = float(self.config.get("min_memory_freeze_interval_seconds", 0.05))
        default_interval = float(self.config.get("memory_freeze_interval_seconds", 0.25))
        interval = max(min_interval, float(interval_seconds or default_interval))
        freeze_id = f"freeze-{uuid.uuid4().hex[:10]}"
        state: dict[str, Any] = {
            "freeze_id": freeze_id,
            "pid": pid,
            "address": hex(parsed_address),
            "value_type": value_type,
            "value_hex": data.hex(),
            "endian": endian,
            "interval_seconds": interval,
            "writes": 1,
            "active": True,
            "last_error": None,
            "old_hex": old_bytes.hex(),
            "verify_hex": verify.hex(),
            "verified": verify == data,
        }

        async def _loop() -> None:
            max_errors = int(self.config.get("max_memory_freeze_errors", 3))
            consecutive_errors = 0
            while state.get("active"):
                await asyncio.sleep(interval)
                try:
                    self._write_memory(pid, parsed_address, data)
                    state["writes"] = int(state.get("writes", 0)) + 1
                    state["last_error"] = None
                    consecutive_errors = 0
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    consecutive_errors += 1
                    state["last_error"] = str(exc)
                    if consecutive_errors >= max_errors:
                        state["active"] = False
                        break

        task = asyncio.create_task(_loop())
        state["task"] = task
        self._freezes[freeze_id] = state
        return self._freeze_snapshot(state, include_task=False)

    def freezes(self) -> dict[str, Any]:
        return {
            "freezes": [
                self._freeze_snapshot(state, include_task=False)
                for state in self._freezes.values()
            ]
        }

    async def unfreeze(
        self,
        freeze_id: str | None = None,
        *,
        all_freezes: bool = False,
    ) -> dict[str, Any]:
        if all_freezes:
            target_ids = list(self._freezes)
        elif freeze_id:
            target_ids = [freeze_id]
        else:
            return {"error": "freeze_id is required unless all_freezes=true"}

        stopped: list[dict[str, Any]] = []
        missing: list[str] = []
        for target_id in target_ids:
            state = self._freezes.pop(target_id, None)
            if not state:
                missing.append(target_id)
                continue
            state["active"] = False
            task = state.get("task")
            if isinstance(task, asyncio.Task) and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            stopped.append(self._freeze_snapshot(state, include_task=False))
        return {"stopped": stopped, "missing": missing}

    def search(
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
        if not self.backend.available:
            return {"error": f"memory_search unavailable on {platform.system()}"}
        needle = encode_value(value_type, value=value, value_hex=value_hex, endian=endian)
        if not needle:
            return {"error": "search value encoded to empty bytes"}
        limit_results = max(1, int(max_results or self.max_search_results))
        limit_bytes = max(1, int(max_scan_bytes or self.max_search_bytes))
        try:
            regions = [
                region
                for region in self._linux_regions(pid)
                if "r" in region.permissions and (not writable_only or "w" in region.permissions)
            ]
        except Exception as exc:
            return {"pid": pid, "error": str(exc)}
        results, scanned, stopped_reason = self._search_regions(
            pid,
            regions,
            needle,
            limit_results=limit_results,
            limit_bytes=limit_bytes,
            value_type=value_type,
            endian=endian,
        )
        search_id = f"mem-{uuid.uuid4().hex[:10]}"
        self._searches[search_id] = {
            "pid": pid,
            "value_type": value_type,
            "endian": endian,
            "bytes": needle.hex(),
            "results": results,
        }
        return {
            "pid": pid,
            "search_id": search_id,
            "value_type": value_type,
            "pattern_hex": needle.hex(),
            "writable_only": writable_only,
            "scanned_bytes": scanned,
            "stopped_reason": stopped_reason,
            "result_count": len(results),
            "results": results,
        }

    def refine(
        self,
        search_id: str,
        *,
        comparison: str = "changed",
        value: Any = None,
        value_hex: str | None = None,
        max_results: int | None = None,
    ) -> dict[str, Any]:
        previous = self._searches.get(search_id)
        if not previous:
            return {"error": f"unknown memory search_id: {search_id}"}
        pid = int(previous["pid"])
        value_type = str(previous["value_type"])
        endian = str(previous["endian"])
        width = len(bytes.fromhex(str(previous["bytes"])))
        expected = None
        if comparison == "equals":
            expected = encode_value(value_type, value=value, value_hex=value_hex, endian=endian)
        limit_results = max(1, int(max_results or self.max_search_results))
        refined: list[dict[str, Any]] = []
        for result in previous["results"]:
            if len(refined) >= limit_results:
                break
            address = parse_address(result["address"])
            try:
                current = self._read_memory(pid, address, width)
            except Exception:
                continue
            old = bytes.fromhex(str(result["bytes_hex"]))
            if self._matches_refine(
                comparison=comparison,
                old=old,
                current=current,
                expected=expected,
                value_type=value_type,
                endian=endian,
            ):
                refined.append(self._result_entry(address, current, value_type, endian))
        new_search_id = f"mem-{uuid.uuid4().hex[:10]}"
        self._searches[new_search_id] = {
            "pid": pid,
            "value_type": value_type,
            "endian": endian,
            "bytes": (expected or bytes.fromhex(str(previous["bytes"]))).hex(),
            "results": refined,
        }
        return {
            "pid": pid,
            "source_search_id": search_id,
            "search_id": new_search_id,
            "comparison": comparison,
            "result_count": len(refined),
            "results": refined,
        }

    def aob_scan(
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
        if not self.backend.available:
            return {"error": f"aob_scan unavailable on {platform.system()}"}
        parsed = parse_aob_pattern(pattern)
        if not parsed:
            return {"error": "pattern is empty"}
        limit_results = max(1, int(max_results or self.max_search_results))
        limit_bytes = max(1, int(max_scan_bytes or self.max_search_bytes))
        regions = []
        try:
            for region in self._linux_regions(pid):
                if "r" not in region.permissions:
                    continue
                if executable_only and "x" not in region.permissions:
                    continue
                if writable_only and "w" not in region.permissions:
                    continue
                if module_filter and module_filter not in region.path:
                    continue
                regions.append(region)
        except Exception as exc:
            return {"pid": pid, "error": str(exc)}
        results, scanned, stopped_reason = self._search_aob_regions(
            pid,
            regions,
            parsed,
            limit_results=limit_results,
            limit_bytes=limit_bytes,
        )
        aob_id = f"aob-{uuid.uuid4().hex[:10]}"
        self._aob_scans[aob_id] = {
            "pid": pid,
            "pattern": pattern,
            "results": results,
        }
        return {
            "pid": pid,
            "aob_id": aob_id,
            "pattern": pattern,
            "executable_only": executable_only,
            "writable_only": writable_only,
            "module_filter": module_filter,
            "scanned_bytes": scanned,
            "stopped_reason": stopped_reason,
            "result_count": len(results),
            "results": results,
        }

    def pointer_scan(
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
        if not self.backend.available:
            return {"error": f"pointer_scan unavailable on {platform.system()}"}
        target = parse_address(target_address)
        ptr_size = pointer_size or struct.calcsize("P")
        if ptr_size not in {4, 8}:
            return {"error": "pointer_size must be 4 or 8"}
        step = max(1, int(align or ptr_size))
        depth_limit = max(1, min(int(max_depth), int(self.config.get("max_pointer_scan_depth", 5))))
        offset_limit = max(0, int(max_offset))
        limit_results = max(1, int(max_results or self.config.get("max_pointer_scan_results", 100)))
        limit_bytes = max(1, int(max_scan_bytes or self.max_search_bytes))
        endian = str(self.config.get("pointer_endian", "little"))

        try:
            regions = [
                region
                for region in self._linux_regions(pid)
                if "r" in region.permissions and (not writable_only or "w" in region.permissions)
            ]
        except Exception as exc:
            return {"pid": pid, "error": str(exc)}
        module_bases = self._module_bases(regions)
        frontier: list[dict[str, Any]] = [
            {
                "target": target,
                "offsets": [],
                "depth": 0,
            }
        ]
        results: list[dict[str, Any]] = []
        scanned_total = 0
        stopped_reason = "completed"
        for depth in range(1, depth_limit + 1):
            if len(results) >= limit_results or not frontier:
                break
            target_nodes = frontier[: int(self.config.get("max_pointer_scan_frontier", 256))]
            next_frontier: list[dict[str, Any]] = []
            candidates, scanned, reason = self._scan_pointer_level(
                pid,
                regions,
                target_nodes,
                pointer_size=ptr_size,
                align=step,
                max_offset=offset_limit,
                max_results=limit_results - len(results),
                max_scan_bytes=max(1, limit_bytes - scanned_total),
                endian=endian,
                module_bases=module_bases,
            )
            scanned_total += scanned
            for candidate in candidates:
                results.append(candidate)
                raw_offsets = [parse_address(v) for v in candidate["offsets"]]
                next_frontier.append(
                    {
                        "target": parse_address(candidate["base_address"]),
                        "offsets": raw_offsets,
                        "depth": depth,
                    }
                )
                if len(results) >= limit_results:
                    stopped_reason = "max_results"
                    break
            if scanned_total >= limit_bytes:
                stopped_reason = "max_scan_bytes"
                break
            if reason != "completed":
                stopped_reason = reason
                if reason == "max_results":
                    break
            frontier = next_frontier
        return {
            "pid": pid,
            "target_address": hex(target),
            "pointer_size": ptr_size,
            "max_depth": depth_limit,
            "max_offset": offset_limit,
            "writable_only": writable_only,
            "scanned_bytes": scanned_total,
            "stopped_reason": stopped_reason,
            "result_count": len(results),
            "chains": results[:limit_results],
        }

    def pointer_resolve(
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
        if not self.backend.available:
            return {"error": f"pointer_resolve unavailable on {platform.system()}"}
        ptr_size = pointer_size or struct.calcsize("P")
        if ptr_size not in {4, 8}:
            return {"error": "pointer_size must be 4 or 8"}
        try:
            base = self._resolve_base_address(
                pid,
                base_address=base_address,
                module_path=module_path,
                module_offset=module_offset,
            )
        except Exception as exc:
            return {"pid": pid, "error": str(exc)}
        parsed_offsets = [parse_address(v) for v in offsets or []]
        current = base
        steps: list[dict[str, Any]] = []
        for offset in parsed_offsets:
            try:
                ptr_bytes = self._read_memory(pid, current, ptr_size)
            except Exception as exc:
                return {"pid": pid, "base_address": hex(base), "error": str(exc), "steps": steps}
            pointee = int.from_bytes(ptr_bytes, endian)
            next_address = pointee + offset
            steps.append(
                {
                    "read_address": hex(current),
                    "pointer_value": hex(pointee),
                    "offset": hex(offset),
                    "next_address": hex(next_address),
                }
            )
            current = next_address
        return {
            "pid": pid,
            "base_address": hex(base),
            "offsets": [hex(v) for v in parsed_offsets],
            "address": hex(current),
            "steps": steps,
        }

    def code_read(self, pid: int, *, address: int | str, size: int) -> dict[str, Any]:
        return self.read(pid, address=address, size=size)

    def code_patch(
        self,
        pid: int,
        *,
        address: int | str,
        patch_hex: str,
        expected_hex: str | None = None,
        patch_id: str | None = None,
    ) -> dict[str, Any]:
        parsed_address = parse_address(address)
        patch = encode_value("bytes", value_hex=patch_hex)
        max_patch = int(self.config.get("max_code_patch_bytes", 64))
        if len(patch) > max_patch:
            return {"pid": pid, "address": hex(parsed_address), "error": f"patch size {len(patch)} exceeds max_code_patch_bytes={max_patch}"}
        expected = encode_value("bytes", value_hex=expected_hex) if expected_hex else None
        if expected is not None and len(expected) != len(patch):
            return {"pid": pid, "address": hex(parsed_address), "error": "expected_hex length must match patch_hex length"}
        try:
            old = self._read_memory(pid, parsed_address, len(patch))
            if expected is not None and old != expected:
                return {
                    "pid": pid,
                    "address": hex(parsed_address),
                    "error": "expected bytes did not match current process memory",
                    "expected_hex": expected.hex(),
                    "actual_hex": old.hex(),
                }
            token, protect_error = self._try_protect_memory(
                pid,
                parsed_address,
                len(patch),
                writable=True,
                executable=True,
            )
            try:
                self._write_memory(pid, parsed_address, patch)
                flush_error = self._try_flush_instruction_cache(pid, parsed_address, len(patch))
                verify = self._read_memory(pid, parsed_address, len(patch))
            except Exception as exc:
                if protect_error is not None:
                    raise RuntimeError(f"{exc}; protection change also failed: {protect_error}") from exc
                raise
            finally:
                if token is not None:
                    self._restore_protection(pid, parsed_address, len(patch), token)
        except Exception as exc:
            return {"pid": pid, "address": hex(parsed_address), "error": str(exc)}
        saved_patch_id = patch_id or f"patch-{uuid.uuid4().hex[:10]}"
        record = {
            "patch_id": saved_patch_id,
            "pid": pid,
            "address": hex(parsed_address),
            "old_hex": old.hex(),
            "patch_hex": patch.hex(),
            "verify_hex": verify.hex(),
            "verified": verify == patch,
            "active": verify == patch,
        }
        if flush_error is not None:
            record["flush_warning"] = str(flush_error)
        self._patches[saved_patch_id] = record
        return dict(record)

    def code_patches(self) -> dict[str, Any]:
        return {"patches": list(self._patches.values())}

    def code_restore(
        self,
        *,
        patch_id: str | None = None,
        all_patches: bool = False,
    ) -> dict[str, Any]:
        if all_patches:
            target_ids = list(self._patches)
        elif patch_id:
            target_ids = [patch_id]
        else:
            return {"error": "patch_id is required unless all_patches=true"}
        restored: list[dict[str, Any]] = []
        missing: list[str] = []
        for target_id in target_ids:
            record = self._patches.get(target_id)
            if not record:
                missing.append(target_id)
                continue
            pid = int(record["pid"])
            address = parse_address(record["address"])
            old = bytes.fromhex(str(record["old_hex"]))
            try:
                token, protect_error = self._try_protect_memory(
                    pid,
                    address,
                    len(old),
                    writable=True,
                    executable=True,
                )
                try:
                    self._write_memory(pid, address, old)
                    flush_error = self._try_flush_instruction_cache(pid, address, len(old))
                    verify = self._read_memory(pid, address, len(old))
                except Exception as exc:
                    if protect_error is not None:
                        raise RuntimeError(f"{exc}; protection change also failed: {protect_error}") from exc
                    raise
                finally:
                    if token is not None:
                        self._restore_protection(pid, address, len(old), token)
            except Exception as exc:
                restored.append({**record, "restored": False, "error": str(exc)})
                continue
            record["active"] = False
            restored_record = {**record, "restored": verify == old, "verify_hex": verify.hex()}
            if flush_error is not None:
                restored_record["flush_warning"] = str(flush_error)
            restored.append(restored_record)
            self._patches.pop(target_id, None)
        return {"restored": restored, "missing": missing}

    def _linux_regions(self, pid: int) -> list[MemoryRegion]:
        return self.backend.regions(pid)

    def _read_memory(self, pid: int, address: int, size: int) -> bytes:
        return self.backend.read(pid, address, size)

    def _write_memory(self, pid: int, address: int, data: bytes) -> None:
        self.backend.write(pid, address, data)

    def _protect_memory(self, pid: int, address: int, size: int, *, writable: bool, executable: bool) -> Any:
        return self.backend.protect(pid, address, size, writable=writable, executable=executable)

    def _try_protect_memory(self, pid: int, address: int, size: int, *, writable: bool, executable: bool) -> tuple[Any, Exception | None]:
        try:
            return self._protect_memory(pid, address, size, writable=writable, executable=executable), None
        except Exception as exc:
            return None, exc

    def _restore_protection(self, pid: int, address: int, size: int, token: Any) -> None:
        self.backend.restore_protection(pid, address, size, token)

    def _flush_instruction_cache(self, pid: int, address: int, size: int) -> None:
        self.backend.flush_instruction_cache(pid, address, size)

    def _try_flush_instruction_cache(self, pid: int, address: int, size: int) -> Exception | None:
        try:
            self._flush_instruction_cache(pid, address, size)
        except Exception as exc:
            return exc
        return None

    def _search_regions(
        self,
        pid: int,
        regions: list[MemoryRegion],
        needle: bytes,
        *,
        limit_results: int,
        limit_bytes: int,
        value_type: str,
        endian: str,
    ) -> tuple[list[dict[str, Any]], int, str]:
        results: list[dict[str, Any]] = []
        scanned = 0
        stopped_reason = "completed"
        overlap = max(0, len(needle) - 1)
        for region in regions:
            if scanned >= limit_bytes or len(results) >= limit_results:
                break
            region_remaining = min(region.size, limit_bytes - scanned)
            offset = 0
            tail = b""
            while offset < region_remaining:
                if len(results) >= limit_results:
                    stopped_reason = "max_results"
                    break
                read_size = min(self.chunk_size, region_remaining - offset)
                try:
                    chunk = self._read_memory(pid, region.start + offset, read_size)
                except Exception:
                    offset += read_size
                    scanned += read_size
                    tail = b""
                    continue
                haystack = tail + chunk
                base = region.start + offset - len(tail)
                index = haystack.find(needle)
                while index != -1:
                    address = base + index
                    if address >= region.start + offset or not tail:
                        results.append(self._result_entry(address, needle, value_type, endian))
                        if len(results) >= limit_results:
                            stopped_reason = "max_results"
                            break
                    index = haystack.find(needle, index + 1)
                if stopped_reason == "max_results":
                    break
                tail = haystack[-overlap:] if overlap else b""
                offset += read_size
                scanned += read_size
            if scanned >= limit_bytes and stopped_reason == "completed":
                stopped_reason = "max_scan_bytes"
        return results, scanned, stopped_reason

    def _search_aob_regions(
        self,
        pid: int,
        regions: list[MemoryRegion],
        pattern: list[int | None],
        *,
        limit_results: int,
        limit_bytes: int,
    ) -> tuple[list[dict[str, Any]], int, str]:
        results: list[dict[str, Any]] = []
        scanned = 0
        stopped_reason = "completed"
        overlap = max(0, len(pattern) - 1)
        for region in regions:
            if scanned >= limit_bytes or len(results) >= limit_results:
                break
            region_remaining = min(region.size, limit_bytes - scanned)
            offset = 0
            tail = b""
            while offset < region_remaining:
                if len(results) >= limit_results:
                    stopped_reason = "max_results"
                    break
                read_size = min(self.chunk_size, region_remaining - offset)
                try:
                    chunk = self._read_memory(pid, region.start + offset, read_size)
                except Exception:
                    offset += read_size
                    scanned += read_size
                    tail = b""
                    continue
                haystack = tail + chunk
                base = region.start + offset - len(tail)
                for index in find_aob_matches(haystack, pattern):
                    address = base + index
                    if address < region.start + offset and tail:
                        continue
                    matched = haystack[index : index + len(pattern)]
                    results.append(
                        {
                            "address": hex(address),
                            "matched_hex": matched.hex(),
                            "region": region.to_dict(),
                        }
                    )
                    if len(results) >= limit_results:
                        stopped_reason = "max_results"
                        break
                if stopped_reason == "max_results":
                    break
                tail = haystack[-overlap:] if overlap else b""
                offset += read_size
                scanned += read_size
            if scanned >= limit_bytes and stopped_reason == "completed":
                stopped_reason = "max_scan_bytes"
        return results, scanned, stopped_reason

    def _scan_pointer_level(
        self,
        pid: int,
        regions: list[MemoryRegion],
        target_nodes: list[dict[str, Any]],
        *,
        pointer_size: int,
        align: int,
        max_offset: int,
        max_results: int,
        max_scan_bytes: int,
        endian: str,
        module_bases: dict[str, int],
    ) -> tuple[list[dict[str, Any]], int, str]:
        candidates: list[dict[str, Any]] = []
        scanned = 0
        stopped_reason = "completed"
        for region in regions:
            if scanned >= max_scan_bytes or len(candidates) >= max_results:
                break
            region_remaining = min(region.size, max_scan_bytes - scanned)
            offset = 0
            while offset + pointer_size <= region_remaining:
                read_size = min(self.chunk_size, region_remaining - offset)
                try:
                    chunk = self._read_memory(pid, region.start + offset, read_size)
                except Exception:
                    offset += read_size
                    scanned += read_size
                    continue
                for chunk_offset in range(0, max(0, len(chunk) - pointer_size + 1), align):
                    pointer_address = region.start + offset + chunk_offset
                    pointer_value = int.from_bytes(chunk[chunk_offset : chunk_offset + pointer_size], endian)
                    for node in target_nodes:
                        target = int(node["target"])
                        delta = target - pointer_value
                        if 0 <= delta <= max_offset:
                            offsets = [delta, *[parse_address(v) for v in node["offsets"]]]
                            module_ref = self._module_reference(pointer_address, region, module_bases)
                            candidates.append(
                                {
                                    "base_address": hex(pointer_address),
                                    "base_module": module_ref,
                                    "offsets": [hex(v) for v in offsets],
                                    "depth": len(offsets),
                                    "points_to": hex(target),
                                    "pointer_value": hex(pointer_value),
                                }
                            )
                            if len(candidates) >= max_results:
                                return candidates, scanned, "max_results"
                offset += read_size
                scanned += read_size
            if scanned >= max_scan_bytes:
                stopped_reason = "max_scan_bytes"
        return candidates, scanned, stopped_reason

    @staticmethod
    def _module_bases(regions: list[MemoryRegion]) -> dict[str, int]:
        bases: dict[str, int] = {}
        for region in regions:
            if not region.path or region.path.startswith("["):
                continue
            bases[region.path] = min(region.start, bases.get(region.path, region.start))
        return bases

    @staticmethod
    def _module_reference(
        address: int,
        region: MemoryRegion,
        module_bases: dict[str, int],
    ) -> dict[str, Any] | None:
        if not region.path or region.path.startswith("["):
            return None
        base = module_bases.get(region.path, region.start)
        return {
            "path": region.path,
            "base": hex(base),
            "offset": hex(address - base),
        }

    def _resolve_base_address(
        self,
        pid: int,
        *,
        base_address: int | str | None,
        module_path: str | None,
        module_offset: int | str | None,
    ) -> int:
        if base_address is not None:
            return parse_address(base_address)
        if not module_path or module_offset is None:
            raise ValueError("base_address or module_path+module_offset is required")
        offset = parse_address(module_offset)
        matches = [
            region
            for region in self._linux_regions(pid)
            if region.path and module_path in region.path
        ]
        if not matches:
            raise ValueError(f"module not found: {module_path}")
        base = min(region.start for region in matches)
        return base + offset

    @staticmethod
    def _result_entry(address: int, data: bytes, value_type: str, endian: str) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "address": hex(address),
            "bytes_hex": data.hex(),
        }
        decoded = decode_value(value_type, data, endian=endian)
        if decoded is not None:
            entry["value"] = decoded
        return entry

    @staticmethod
    def _matches_refine(
        *,
        comparison: str,
        old: bytes,
        current: bytes,
        expected: bytes | None,
        value_type: str,
        endian: str,
    ) -> bool:
        if comparison == "equals":
            return expected is not None and current == expected
        if comparison == "changed":
            return current != old
        if comparison == "unchanged":
            return current == old
        old_value = decode_value(value_type, old, endian=endian)
        current_value = decode_value(value_type, current, endian=endian)
        if not isinstance(old_value, (int, float)) or not isinstance(current_value, (int, float)):
            return False
        if comparison == "increased":
            return current_value > old_value
        if comparison == "decreased":
            return current_value < old_value
        return False

    @staticmethod
    def _freeze_snapshot(state: dict[str, Any], *, include_task: bool) -> dict[str, Any]:
        if include_task:
            return dict(state)
        return {key: value for key, value in state.items() if key != "task"}


def parse_address(value: int | str) -> int:
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        raise ValueError("address is required")
    return int(text, 0)


def parse_aob_pattern(pattern: str) -> list[int | None]:
    text = pattern.strip()
    if not text:
        return []
    if re.search(r"\s", text):
        tokens = re.split(r"[\s,]+", text)
    else:
        compact = text.replace("0x", "").replace("0X", "")
        if len(compact) % 2:
            raise ValueError("compact AOB pattern must contain an even number of hex/wildcard characters")
        tokens = [compact[i : i + 2] for i in range(0, len(compact), 2)]
    parsed: list[int | None] = []
    for token in tokens:
        if not token:
            continue
        token = token.strip()
        if set(token) <= {"?"}:
            parsed.append(None)
            continue
        if "?" in token:
            if len(token) != 2:
                raise ValueError(f"invalid wildcard token: {token}")
            parsed.append(None)
            continue
        parsed.append(int(token.removeprefix("0x").removeprefix("0X"), 16))
    return parsed


def find_aob_matches(data: bytes, pattern: list[int | None]) -> list[int]:
    if not pattern or len(data) < len(pattern):
        return []
    matches: list[int] = []
    width = len(pattern)
    first = pattern[0]
    start = 0
    while start <= len(data) - width:
        if first is None:
            index = start
        else:
            index = data.find(bytes([first]), start, len(data) - width + 1)
            if index == -1:
                break
        for pattern_index, expected in enumerate(pattern):
            if expected is not None and data[index + pattern_index] != expected:
                break
        else:
            matches.append(index)
        start = index + 1
    return matches


def encode_value(
    value_type: str,
    *,
    value: Any = None,
    value_hex: str | None = None,
    endian: str = "little",
) -> bytes:
    normalized = value_type.lower()
    if normalized == "bytes":
        raw = value_hex if value_hex is not None else str(value or "")
        cleaned = re.sub(r"0[xX]", "", raw)
        cleaned = re.sub(r"[^0-9a-fA-F]", "", cleaned)
        if len(cleaned) % 2:
            cleaned = "0" + cleaned
        return bytes.fromhex(cleaned)
    if normalized == "string":
        return str(value or "").encode("utf-8")
    fmt = _NUMERIC_FORMATS.get(normalized)
    if not fmt:
        raise ValueError(f"unsupported value_type: {value_type}")
    prefix = "<" if endian.lower() == "little" else ">"
    if normalized.startswith("float"):
        typed_value: int | float = float(value)
    else:
        typed_value = int(str(value), 0)
    return struct.pack(prefix + fmt, typed_value)


def decode_value(value_type: str, data: bytes, *, endian: str = "little") -> Any:
    normalized = value_type.lower()
    if normalized == "bytes":
        return data.hex()
    if normalized == "string":
        return data.decode("utf-8", errors="replace")
    fmt = _NUMERIC_FORMATS.get(normalized)
    if not fmt:
        return None
    prefix = "<" if endian.lower() == "little" else ">"
    size = struct.calcsize(prefix + fmt)
    if len(data) < size:
        return None
    return struct.unpack(prefix + fmt, data[:size])[0]


def _ascii_preview(data: bytes) -> str:
    return "".join(chr(b) if 32 <= b < 127 else "." for b in data)
