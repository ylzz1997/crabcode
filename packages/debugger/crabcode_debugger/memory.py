"""Process memory inspection helpers."""

from __future__ import annotations

import asyncio
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
    """CE-style memory search/read/write for supported platforms.

    Linux is implemented directly through /proc/<pid>/maps and /proc/<pid>/mem.
    macOS and Windows are reported through capabilities as unavailable until a
    native Mach/DbgEng backend is added.
    """

    def __init__(self, *, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self.max_search_results = int(self.config.get("max_search_results", 100))
        self.max_search_bytes = int(self.config.get("max_search_bytes", 128 * 1024 * 1024))
        self.chunk_size = int(self.config.get("memory_search_chunk_bytes", 1024 * 1024))
        self._searches: dict[str, dict[str, Any]] = {}
        self._freezes: dict[str, dict[str, Any]] = {}

    async def close(self) -> None:
        await self.unfreeze(all_freezes=True)

    def capabilities(self) -> dict[str, bool | str]:
        system = platform.system().lower()
        linux = system == "linux" and Path("/proc").exists()
        return {
            "memory_regions": linux,
            "memory_read": linux,
            "memory_search": linux,
            "memory_refine": linux,
            "memory_write": linux,
            "memory_freeze": linux,
            "memory_unfreeze": True,
            "memory_freezes": True,
            "backend": "linux-procfs" if linux else "unavailable",
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
        if platform.system().lower() != "linux":
            return {"error": f"memory_regions unavailable on {platform.system()}"}
        parsed = self._linux_regions(pid)
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
        if platform.system().lower() != "linux":
            return {"error": f"memory_search unavailable on {platform.system()}"}
        needle = encode_value(value_type, value=value, value_hex=value_hex, endian=endian)
        if not needle:
            return {"error": "search value encoded to empty bytes"}
        limit_results = max(1, int(max_results or self.max_search_results))
        limit_bytes = max(1, int(max_scan_bytes or self.max_search_bytes))
        regions = [
            region
            for region in self._linux_regions(pid)
            if "r" in region.permissions and (not writable_only or "w" in region.permissions)
        ]
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

    def _linux_regions(self, pid: int) -> list[MemoryRegion]:
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

    def _read_memory(self, pid: int, address: int, size: int) -> bytes:
        if platform.system().lower() != "linux":
            raise RuntimeError(f"memory_read unavailable on {platform.system()}")
        with open(f"/proc/{pid}/mem", "rb", buffering=0) as handle:
            handle.seek(address)
            return handle.read(size)

    def _write_memory(self, pid: int, address: int, data: bytes) -> None:
        if platform.system().lower() != "linux":
            raise RuntimeError(f"memory_write unavailable on {platform.system()}")
        with open(f"/proc/{pid}/mem", "r+b", buffering=0) as handle:
            handle.seek(address)
            handle.write(data)

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
