"""CrabCode tool wrappers for debugging."""

from __future__ import annotations

import json
from typing import Any

from crabcode_core.types.tool import PermissionBehavior, PermissionResult, Tool, ToolContext, ToolResult

from crabcode_debugger.adapters import AdapterRegistry, infer_language
from crabcode_debugger.process import ProcessInspector
from crabcode_debugger.runtime import get_debug_session_manager, release_debug_session_manager


_DEBUGGER_ACTIONS = {
    "adapters",
    "sessions",
    "start",
    "attach",
    "set_breakpoints",
    "configuration_done",
    "continue",
    "pause",
    "step_over",
    "step_in",
    "step_out",
    "threads",
    "stack",
    "scopes",
    "variables",
    "evaluate",
    "events",
    "stop",
}

_PROCESS_ACTIONS = {
    "capabilities",
    "list_processes",
    "inspect_process",
    "attach_debugger",
    "sample_stack",
    "dump_core",
    "memory_maps",
    "memory_regions",
    "memory_read",
    "memory_search",
    "memory_refine",
    "memory_write",
    "memory_freeze",
    "memory_unfreeze",
    "memory_freezes",
    "aob_scan",
    "pointer_scan",
    "pointer_resolve",
    "code_read",
    "code_patch",
    "code_restore",
    "code_patches",
    "trace_syscalls",
    "detach",
    "terminate",
    "kill",
}

def _json_result(data: Any, *, max_chars: int = 20_000) -> str:
    text = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    if len(text) > max_chars:
        return text[:max_chars] + "\n... (truncated)"
    return text


def _as_int(value: Any, default: int | None = None) -> int | None:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _is_positive_pid(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    try:
        return int(value) > 0
    except (TypeError, ValueError):
        return False


def _debug_owner_id(context: ToolContext) -> str:
    if context.session_id:
        return context.session_id
    if context.session is not None:
        return f"session-object:{id(context.session)}"
    return f"tool-context:{id(context)}"


class DebuggerTool(Tool):
    name = "Debugger"
    description = "Debug programs through official Debug Adapter Protocol adapters."
    is_read_only = False
    is_concurrency_safe = False
    uses_tool_permission_policy = True
    input_schema = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": sorted(_DEBUGGER_ACTIONS)},
            "session_id": {"type": "string"},
            "language": {"type": "string"},
            "adapter_id": {"type": "string"},
            "program": {"type": "string"},
            "args": {"type": "array", "items": {"type": "string"}},
            "cwd": {"type": "string"},
            "pid": {"type": "integer"},
            "path": {"type": "string"},
            "lines": {"type": "array", "items": {"type": "integer"}},
            "thread_id": {"type": "integer"},
            "frame_id": {"type": "integer"},
            "levels": {"type": "integer"},
            "variables_reference": {"type": "integer"},
            "start": {"type": "integer"},
            "count": {"type": "integer"},
            "expression": {"type": "string"},
            "context": {"type": "string"},
            "terminate_debuggee": {"type": "boolean"},
            "launch_config": {"type": "object", "additionalProperties": True},
            "attach_config": {"type": "object", "additionalProperties": True},
        },
        "required": ["action"],
    }

    def __init__(self) -> None:
        self._config: dict[str, Any] = {}
        self._manager: Any = None
        self._max_chars = 20_000
        self._allow_evaluate = False
        self._manager_scope: tuple[str, str] | None = None

    async def setup(self, context: ToolContext) -> None:
        await super().setup(context)
        await self._release_manager()
        self._config = dict(context.tool_config)
        self._max_chars = int(self._config.get("max_output_chars", 20_000))
        self._allow_evaluate = bool(self._config.get("allow_evaluate", False))
        owner_id = _debug_owner_id(context)
        self._manager = get_debug_session_manager(
            cwd=context.cwd,
            owner_id=owner_id,
            env=context.env,
            config=self._config,
        )
        self._manager_scope = (context.cwd, owner_id)

    async def close(self) -> None:
        await self._release_manager()

    async def _release_manager(self) -> None:
        if self._manager is None or self._manager_scope is None:
            return
        cwd, owner_id = self._manager_scope
        manager = self._manager
        self._manager = None
        self._manager_scope = None
        await release_debug_session_manager(cwd=cwd, owner_id=owner_id, manager=manager)

    async def get_prompt(self, **kwargs: Any) -> str:
        return (
            "Use Debugger to run or attach to programs through official Debug Adapter Protocol adapters. "
            "Supported first-class languages are C/C++, Rust, Python, Go, Java, TypeScript, and JavaScript. "
            "Call action=adapters first when you need to know which adapters are installed. "
            "Use start or attach to create a session, set_breakpoints before configuration_done when the adapter requires it, "
            "then use continue, pause, step_over, step_in, step_out, stack, scopes, variables, and evaluate. "
            "Use events to drain stopped/terminated events and stop to disconnect."
        )

    async def validate_input(self, tool_input: dict[str, Any]) -> str | None:
        action = str(tool_input.get("action", "")).strip()
        if action not in _DEBUGGER_ACTIONS:
            return f"action must be one of: {', '.join(sorted(_DEBUGGER_ACTIONS))}"
        if action in _DEBUGGER_ACTIONS - {"adapters", "sessions", "start", "attach"}:
            if not str(tool_input.get("session_id", "")).strip():
                return "session_id is required for this action"
        if action == "start" and not (tool_input.get("language") or tool_input.get("program")):
            return "language or program is required for start"
        if action == "attach" and not _is_positive_pid(tool_input.get("pid")):
            return "pid must be a positive integer for attach"
        if action == "attach" and not tool_input.get("language"):
            return "language is required for attach"
        if action == "set_breakpoints" and not (tool_input.get("path") and tool_input.get("lines")):
            return "path and lines are required for set_breakpoints"
        if action == "scopes" and tool_input.get("frame_id") is None:
            return "frame_id is required for scopes"
        if action == "variables" and tool_input.get("variables_reference") is None:
            return "variables_reference is required for variables"
        if action == "evaluate" and not str(tool_input.get("expression", "")).strip():
            return "expression is required for evaluate"
        return None

    async def check_permissions(self, tool_input: dict[str, Any], context: ToolContext) -> PermissionResult:
        action = str(tool_input.get("action", ""))
        if action == "adapters":
            probe_commands = self._configured_probe_commands()
            if probe_commands:
                return PermissionResult(
                    behavior=PermissionBehavior.ASK,
                    reason=f"Adapter discovery will execute configured probe commands: {probe_commands}",
                    permission_key=self.get_permission_key(tool_input),
                )
            return PermissionResult(behavior=PermissionBehavior.ALLOW)
        if action in {"sessions", "events"}:
            return PermissionResult(behavior=PermissionBehavior.ALLOW)
        if action == "evaluate" and not self._allow_evaluate:
            return PermissionResult(
                behavior=PermissionBehavior.ASK,
                reason="Debugger evaluate can execute code in the debuggee process",
                permission_key=self.get_permission_key(tool_input),
            )
        if action == "stop" and bool(tool_input.get("terminate_debuggee", False)):
            return PermissionResult(
                behavior=PermissionBehavior.ASK,
                reason="Stopping with terminate_debuggee=true can terminate the debugged process",
                permission_key=self.get_permission_key(tool_input),
            )
        return PermissionResult(
            behavior=PermissionBehavior.ASK,
            reason=f"Debugger action '{action}' can inspect or control a debuggee process",
            permission_key=self.get_permission_key(tool_input),
        )

    def _configured_probe_commands(self) -> list[list[str]]:
        adapters = self._config.get("adapters", {})
        if not isinstance(adapters, dict):
            return []
        commands: list[list[str]] = []
        for raw in adapters.values():
            if not isinstance(raw, dict):
                continue
            probe = raw.get("probe_command")
            if isinstance(probe, list) and probe:
                commands.append([str(part) for part in probe])
        return commands

    def get_permission_key(self, tool_input: dict[str, Any]) -> str:
        action = str(tool_input.get("action", ""))
        if action in {"start", "attach"}:
            target = tool_input.get("program") or tool_input.get("pid") or "target"
            language = infer_language(str(tool_input.get("language") or target)) or "unknown"
            return f"Debugger:{action}:{language}:{target}"
        if action == "evaluate":
            return f"Debugger:evaluate:{tool_input.get('session_id', '')}"
        if action == "stop":
            return f"Debugger:stop:{tool_input.get('session_id', '')}"
        return f"Debugger:{action}"

    async def call(self, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        action = str(tool_input["action"])
        try:
            data = await self._call(action, tool_input)
            is_error = isinstance(data, dict) and "error" in data
            text = _json_result(data, max_chars=self._max_chars)
            return ToolResult(data=data, result_for_model=text, result_for_display=text, is_error=is_error)
        except Exception as exc:
            return ToolResult(result_for_model=f"Debugger error: {exc}", is_error=True)

    async def _call(self, action: str, tool_input: dict[str, Any]) -> Any:
        if action == "adapters":
            registry = AdapterRegistry(self._config)
            language = tool_input.get("language")
            return {"adapters": [status.to_dict() for status in registry.status(language)]}
        if action == "sessions":
            return self._manager.list_sessions()
        if action == "start":
            return await self._manager.start(
                language=str(tool_input.get("language") or tool_input.get("program") or ""),
                program=tool_input.get("program"),
                args=[str(a) for a in tool_input.get("args", [])],
                cwd=tool_input.get("cwd"),
                adapter_id=tool_input.get("adapter_id"),
                launch_config=tool_input.get("launch_config") if isinstance(tool_input.get("launch_config"), dict) else None,
            )
        if action == "attach":
            return await self._manager.attach(
                language=str(tool_input["language"]),
                pid=int(tool_input["pid"]),
                adapter_id=tool_input.get("adapter_id"),
                attach_config=tool_input.get("attach_config") if isinstance(tool_input.get("attach_config"), dict) else None,
            )
        session_id = str(tool_input.get("session_id", ""))
        if action == "set_breakpoints":
            return await self._manager.set_breakpoints(
                session_id,
                path=str(tool_input["path"]),
                lines=[int(line) for line in tool_input["lines"]],
            )
        if action == "configuration_done":
            return await self._manager.configuration_done(session_id)
        if action == "continue":
            return await self._manager.continue_thread(session_id, _as_int(tool_input.get("thread_id")))
        if action == "pause":
            return await self._manager.pause(session_id, _as_int(tool_input.get("thread_id")))
        if action in {"step_over", "step_in", "step_out"}:
            return await self._manager.step(session_id, action, _as_int(tool_input.get("thread_id")))
        if action == "threads":
            return await self._manager.threads(session_id)
        if action == "stack":
            return await self._manager.stack(
                session_id,
                _as_int(tool_input.get("thread_id")),
                levels=int(tool_input.get("levels", 20)),
            )
        if action == "scopes":
            return await self._manager.scopes(session_id, int(tool_input["frame_id"]))
        if action == "variables":
            return await self._manager.variables(
                session_id,
                int(tool_input["variables_reference"]),
                start=int(tool_input.get("start", 0)),
                count=int(tool_input.get("count", 100)),
            )
        if action == "evaluate":
            return await self._manager.evaluate(
                session_id,
                str(tool_input["expression"]),
                frame_id=_as_int(tool_input.get("frame_id")),
                context=str(tool_input.get("context", "repl")),
            )
        if action == "events":
            return await self._manager.events(session_id)
        if action == "stop":
            return await self._manager.stop(
                session_id,
                terminate_debuggee=bool(tool_input.get("terminate_debuggee", False)),
            )
        return {"error": f"unhandled action: {action}"}


class ProcessDebuggerTool(Tool):
    name = "ProcessDebugger"
    description = "Inspect, attach to, and diagnose operating-system processes."
    is_read_only = False
    is_concurrency_safe = False
    uses_tool_permission_policy = True
    input_schema = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": sorted(_PROCESS_ACTIONS)},
            "pid": {"type": "integer"},
            "query": {"type": "string"},
            "limit": {"type": "integer"},
            "language": {"type": "string"},
            "adapter_id": {"type": "string"},
            "session_id": {"type": "string"},
            "duration_seconds": {"type": "integer"},
            "interval_seconds": {"type": "number"},
            "output_path": {"type": "string"},
            "attach_config": {"type": "object", "additionalProperties": True},
            "pattern": {"type": "string"},
            "module_filter": {"type": "string"},
            "target_address": {
                "description": "Target address for pointer_scan.",
            },
            "max_depth": {"type": "integer"},
            "max_offset": {"type": "integer"},
            "pointer_size": {"type": "integer", "enum": [4, 8]},
            "align": {"type": "integer"},
            "offsets": {
                "type": "array",
                "items": {},
                "description": "Pointer offsets as integers or hex strings.",
            },
            "module_path": {"type": "string"},
            "module_offset": {
                "description": "Module-relative base offset as an integer or hex string.",
            },
            "patch_hex": {"type": "string"},
            "expected_hex": {"type": "string"},
            "patch_id": {"type": "string"},
            "address": {
                "description": "Memory address as an integer or hex string.",
            },
            "base_address": {
                "description": "Pointer-chain base address as an integer or hex string.",
            },
            "size": {"type": "integer"},
            "value_type": {
                "type": "string",
                "enum": [
                    "bytes",
                    "string",
                    "int8",
                    "uint8",
                    "int16",
                    "uint16",
                    "int32",
                    "uint32",
                    "int64",
                    "uint64",
                    "float32",
                    "float64",
                ],
            },
            "value": {
                "description": "Value for memory_search, memory_refine equals, or memory_write.",
            },
            "value_hex": {"type": "string"},
            "endian": {"type": "string", "enum": ["little", "big"]},
            "writable_only": {"type": "boolean"},
            "executable_only": {"type": "boolean"},
            "readable": {"type": "boolean"},
            "writable": {"type": "boolean"},
            "executable": {"type": "boolean"},
            "max_results": {"type": "integer"},
            "max_scan_bytes": {"type": "integer"},
            "search_id": {"type": "string"},
            "freeze_id": {"type": "string"},
            "all": {"type": "boolean"},
            "comparison": {
                "type": "string",
                "enum": ["equals", "changed", "unchanged", "increased", "decreased"],
            },
        },
        "required": ["action"],
    }

    def __init__(self) -> None:
        self._config: dict[str, Any] = {}
        self._inspector: ProcessInspector | None = None
        self._debug_manager: Any = None
        self._max_chars = 20_000
        self._manager_scope: tuple[str, str] | None = None

    async def setup(self, context: ToolContext) -> None:
        await super().setup(context)
        await self._release_manager()
        if self._inspector is not None:
            await self._inspector.close()
            self._inspector = None
        self._config = dict(context.tool_config)
        self._max_chars = int(self._config.get("max_output_chars", 20_000))
        self._inspector = ProcessInspector(cwd=context.cwd, config=self._config)
        debugger_config: dict[str, Any] = {}
        if isinstance(self._config.get("adapters"), dict):
            debugger_config["adapters"] = self._config["adapters"]
        if isinstance(self._config.get("debugger"), dict):
            debugger_config.update(self._config["debugger"])
        owner_id = _debug_owner_id(context)
        self._debug_manager = get_debug_session_manager(
            cwd=context.cwd,
            owner_id=owner_id,
            env=context.env,
            config=debugger_config,
        )
        self._manager_scope = (context.cwd, owner_id)

    async def close(self) -> None:
        if self._inspector is not None:
            await self._inspector.close()
            self._inspector = None
        await self._release_manager()

    async def _release_manager(self) -> None:
        if self._debug_manager is None or self._manager_scope is None:
            return
        cwd, owner_id = self._manager_scope
        manager = self._debug_manager
        self._debug_manager = None
        self._manager_scope = None
        await release_debug_session_manager(cwd=cwd, owner_id=owner_id, manager=manager)

    async def get_prompt(self, **kwargs: Any) -> str:
        return (
            "Use ProcessDebugger for process-level diagnostics: list_processes, inspect_process, "
            "sample_stack, dump_core, memory_maps, memory_regions, memory_read, memory_search, "
            "memory_refine, memory_write, memory_freeze, memory_unfreeze, memory_freezes, "
            "aob_scan, pointer_scan, pointer_resolve, code_read, code_patch, code_restore, "
            "code_patches, trace_syscalls, attach_debugger, detach, terminate, and kill. "
            "Use capabilities first to see what the current OS supports. "
            "Most actions ask for permission by default; default_mode=run_everything bypasses "
            "those confirmations through the normal CrabCode permission mode."
        )

    async def validate_input(self, tool_input: dict[str, Any]) -> str | None:
        action = str(tool_input.get("action", "")).strip()
        if action not in _PROCESS_ACTIONS:
            return f"action must be one of: {', '.join(sorted(_PROCESS_ACTIONS))}"
        if action == "memory_refine":
            if not str(tool_input.get("search_id", "")).strip():
                return "search_id is required for memory_refine"
            if tool_input.get("comparison") == "equals" and tool_input.get("value") is None and tool_input.get("value_hex") is None:
                return "value or value_hex is required for memory_refine comparison=equals"
            return None
        if action == "memory_unfreeze":
            if not str(tool_input.get("freeze_id", "")).strip() and not bool(tool_input.get("all", False)):
                return "freeze_id is required unless all=true"
            return None
        if action == "memory_freezes":
            return None
        if action == "code_restore":
            if not str(tool_input.get("patch_id", "")).strip() and not bool(tool_input.get("all", False)):
                return "patch_id is required unless all=true"
            return None
        if action == "code_patches":
            return None
        if action in _PROCESS_ACTIONS - {"capabilities", "list_processes"}:
            if action == "detach":
                if not str(tool_input.get("session_id", "")).strip():
                    return "session_id is required for detach"
            elif tool_input.get("pid") is None:
                return "pid is required for this action"
            elif not _is_positive_pid(tool_input.get("pid")):
                return "pid must be a positive integer for this action"
        if action == "attach_debugger" and not tool_input.get("language"):
            return "language is required for attach_debugger"
        if action == "memory_read":
            if tool_input.get("address") is None or tool_input.get("size") is None:
                return "address and size are required for memory_read"
        if action == "aob_scan" and not str(tool_input.get("pattern", "")).strip():
            return "pattern is required for aob_scan"
        if action == "pointer_scan" and tool_input.get("target_address") is None:
            return "target_address is required for pointer_scan"
        if action == "pointer_resolve":
            has_base = tool_input.get("base_address") is not None or tool_input.get("address") is not None
            has_module = tool_input.get("module_path") and tool_input.get("module_offset") is not None
            if not has_base and not has_module:
                return "base_address/address or module_path+module_offset is required for pointer_resolve"
        if action == "code_read":
            if tool_input.get("address") is None or tool_input.get("size") is None:
                return "address and size are required for code_read"
        if action == "code_patch":
            if tool_input.get("address") is None or not str(tool_input.get("patch_hex", "")).strip():
                return "address and patch_hex are required for code_patch"
        if action in {"memory_search", "memory_write", "memory_freeze"}:
            if not tool_input.get("value_type"):
                return "value_type is required"
            if tool_input.get("value") is None and tool_input.get("value_hex") is None:
                return "value or value_hex is required"
        if action in {"memory_write", "memory_freeze"} and tool_input.get("address") is None:
            return f"address is required for {action}"
        return None

    async def check_permissions(self, tool_input: dict[str, Any], context: ToolContext) -> PermissionResult:
        action = str(tool_input.get("action", ""))
        if action == "capabilities":
            return PermissionResult(behavior=PermissionBehavior.ALLOW)
        return PermissionResult(
            behavior=PermissionBehavior.ASK,
            reason=f"ProcessDebugger action '{action}' can inspect, attach to, read, or alter an OS process",
            permission_key=self.get_permission_key(tool_input),
        )

    def get_permission_key(self, tool_input: dict[str, Any]) -> str:
        action = str(tool_input.get("action", ""))
        if action.startswith("memory_") or action in {"aob_scan", "pointer_scan", "pointer_resolve", "code_read", "code_patch", "code_restore", "code_patches"}:
            target = (
                f"{tool_input.get('pid', 'state')}:"
                f"{tool_input.get('address') or tool_input.get('target_address') or tool_input.get('search_id') or tool_input.get('freeze_id') or tool_input.get('patch_id') or action}"
            )
            return f"ProcessDebugger:{action}:{target}"
        target = tool_input.get("pid") or tool_input.get("session_id") or tool_input.get("search_id") or "all"
        return f"ProcessDebugger:{action}:{target}"

    async def call(self, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        if self._inspector is None:
            return ToolResult(result_for_model="ProcessDebugger is not initialized", is_error=True)
        action = str(tool_input["action"])
        try:
            data = await self._call(action, tool_input)
            is_error = isinstance(data, dict) and "error" in data
            text = _json_result(data, max_chars=self._max_chars)
            return ToolResult(data=data, result_for_model=text, result_for_display=text, is_error=is_error)
        except Exception as exc:
            return ToolResult(result_for_model=f"ProcessDebugger error: {exc}", is_error=True)

    async def _call(self, action: str, tool_input: dict[str, Any]) -> Any:
        assert self._inspector is not None
        if action == "capabilities":
            return self._inspector.capabilities()
        if action == "list_processes":
            return await self._inspector.list_processes(
                limit=int(tool_input.get("limit", 100)),
                query=tool_input.get("query"),
            )
        if action == "inspect_process":
            return await self._inspector.inspect_process(int(tool_input["pid"]))
        if action == "attach_debugger":
            return await self._debug_manager.attach(
                language=str(tool_input["language"]),
                pid=int(tool_input["pid"]),
                adapter_id=tool_input.get("adapter_id"),
                attach_config=tool_input.get("attach_config") if isinstance(tool_input.get("attach_config"), dict) else None,
            )
        if action == "sample_stack":
            return await self._inspector.sample_stack(
                int(tool_input["pid"]),
                duration_seconds=int(tool_input.get("duration_seconds", 3)),
            )
        if action == "dump_core":
            return await self._inspector.dump_core(
                int(tool_input["pid"]),
                output_path=tool_input.get("output_path"),
            )
        if action == "memory_maps":
            return await self._inspector.memory_maps(int(tool_input["pid"]))
        if action == "memory_regions":
            return await self._inspector.memory_regions(
                int(tool_input["pid"]),
                readable=bool(tool_input.get("readable", False)),
                writable=bool(tool_input.get("writable", False)),
                executable=tool_input.get("executable") if isinstance(tool_input.get("executable"), bool) else None,
                limit=int(tool_input.get("limit", 500)),
            )
        if action == "memory_read":
            return await self._inspector.memory_read(
                int(tool_input["pid"]),
                address=tool_input["address"],
                size=int(tool_input["size"]),
            )
        if action == "memory_search":
            return await self._inspector.memory_search(
                int(tool_input["pid"]),
                value_type=str(tool_input["value_type"]),
                value=tool_input.get("value"),
                value_hex=tool_input.get("value_hex"),
                endian=str(tool_input.get("endian", "little")),
                writable_only=bool(tool_input.get("writable_only", True)),
                max_results=_as_int(tool_input.get("max_results")),
                max_scan_bytes=_as_int(tool_input.get("max_scan_bytes")),
            )
        if action == "memory_refine":
            return await self._inspector.memory_refine(
                str(tool_input["search_id"]),
                comparison=str(tool_input.get("comparison", "changed")),
                value=tool_input.get("value"),
                value_hex=tool_input.get("value_hex"),
                max_results=_as_int(tool_input.get("max_results")),
            )
        if action == "memory_write":
            return await self._inspector.memory_write(
                int(tool_input["pid"]),
                address=tool_input["address"],
                value_type=str(tool_input["value_type"]),
                value=tool_input.get("value"),
                value_hex=tool_input.get("value_hex"),
                endian=str(tool_input.get("endian", "little")),
            )
        if action == "memory_freeze":
            return await self._inspector.memory_freeze(
                int(tool_input["pid"]),
                address=tool_input["address"],
                value_type=str(tool_input["value_type"]),
                value=tool_input.get("value"),
                value_hex=tool_input.get("value_hex"),
                endian=str(tool_input.get("endian", "little")),
                interval_seconds=float(tool_input["interval_seconds"]) if tool_input.get("interval_seconds") is not None else None,
            )
        if action == "memory_unfreeze":
            return await self._inspector.memory_unfreeze(
                freeze_id=tool_input.get("freeze_id"),
                all_freezes=bool(tool_input.get("all", False)),
            )
        if action == "memory_freezes":
            return await self._inspector.memory_freezes()
        if action == "aob_scan":
            return await self._inspector.aob_scan(
                int(tool_input["pid"]),
                pattern=str(tool_input["pattern"]),
                executable_only=bool(tool_input.get("executable_only", True)),
                writable_only=bool(tool_input.get("writable_only", False)),
                module_filter=tool_input.get("module_filter"),
                max_results=_as_int(tool_input.get("max_results")),
                max_scan_bytes=_as_int(tool_input.get("max_scan_bytes")),
            )
        if action == "pointer_scan":
            return await self._inspector.pointer_scan(
                int(tool_input["pid"]),
                target_address=tool_input["target_address"],
                max_depth=int(tool_input.get("max_depth", 3)),
                max_offset=int(tool_input.get("max_offset", 4096)),
                pointer_size=_as_int(tool_input.get("pointer_size")),
                align=_as_int(tool_input.get("align")),
                writable_only=bool(tool_input.get("writable_only", True)),
                max_results=_as_int(tool_input.get("max_results")),
                max_scan_bytes=_as_int(tool_input.get("max_scan_bytes")),
            )
        if action == "pointer_resolve":
            return await self._inspector.pointer_resolve(
                int(tool_input["pid"]),
                base_address=tool_input.get("base_address", tool_input.get("address")),
                offsets=tool_input.get("offsets") if isinstance(tool_input.get("offsets"), list) else None,
                module_path=tool_input.get("module_path"),
                module_offset=tool_input.get("module_offset"),
                pointer_size=_as_int(tool_input.get("pointer_size")),
                endian=str(tool_input.get("endian", "little")),
            )
        if action == "code_read":
            return await self._inspector.code_read(
                int(tool_input["pid"]),
                address=tool_input["address"],
                size=int(tool_input["size"]),
            )
        if action == "code_patch":
            return await self._inspector.code_patch(
                int(tool_input["pid"]),
                address=tool_input["address"],
                patch_hex=str(tool_input["patch_hex"]),
                expected_hex=tool_input.get("expected_hex"),
                patch_id=tool_input.get("patch_id"),
            )
        if action == "code_restore":
            return await self._inspector.code_restore(
                patch_id=tool_input.get("patch_id"),
                all_patches=bool(tool_input.get("all", False)),
            )
        if action == "code_patches":
            return await self._inspector.code_patches()
        if action == "trace_syscalls":
            return await self._inspector.trace_syscalls(
                int(tool_input["pid"]),
                duration_seconds=int(tool_input.get("duration_seconds", 5)),
                output_path=tool_input.get("output_path"),
            )
        if action == "detach":
            return await self._debug_manager.stop(
                str(tool_input["session_id"]),
                terminate_debuggee=False,
            )
        if action == "terminate":
            return await self._inspector.signal_process(int(tool_input["pid"]), sig="terminate")
        if action == "kill":
            return await self._inspector.signal_process(int(tool_input["pid"]), sig="kill")
        return {"error": f"unhandled action: {action}"}
