"""Hooks manager for running settings-defined shell hooks."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from fnmatch import fnmatch
from typing import Any


@dataclass
class HookRunResult:
    blocked: bool = False
    feedback: list[str] | None = None
    details: list[str] | None = None


class HookManager:
    """Execute hooks from settings for supported lifecycle events."""

    _ALIASES: dict[str, tuple[str, ...]] = {
        "user_prompt_submit": (
            "user_prompt_submit",
            "userPromptSubmit",
            "UserPromptSubmit",
            "user-prompt-submit",
            "user-prompt-submit-hook",
        ),
        "pre_tool_call": (
            "pre_tool_call",
            "preToolCall",
            "PreToolCall",
            "pre-tool-call",
            "pre_tool_use",
            "preToolUse",
            "PreToolUse",
            "pre-tool-use",
        ),
        "post_tool_call": (
            "post_tool_call",
            "postToolCall",
            "PostToolCall",
            "post-tool-call",
            "post_tool_use",
            "postToolUse",
            "PostToolUse",
            "post-tool-use",
        ),
    }

    def __init__(self, hooks: dict[str, list[dict[str, Any]]] | None = None):
        self._hooks = hooks or {}

    def set_hooks(self, hooks: dict[str, list[dict[str, Any]]] | None) -> None:
        self._hooks = hooks or {}

    async def run(
        self,
        event: str,
        payload: dict[str, Any],
        *,
        cwd: str,
        env: dict[str, str] | None = None,
    ) -> HookRunResult:
        canonical = self._canonical_event_name(event)
        raw_entries = self._gather_hooks(canonical)
        hooks = self._expand_entries(raw_entries)
        if not hooks:
            return HookRunResult(blocked=False, feedback=[], details=[])

        merged_env = {**os.environ, **(env or {})}
        blocked = False
        feedback: list[str] = []
        details: list[str] = []

        for cfg in hooks:
            command = self._extract_command(cfg)
            if not command:
                continue
            if not self._matches(cfg, payload):
                continue
            timeout = self._extract_timeout(cfg)
            continue_on_error = self._extract_continue_on_error(cfg)
            result = await self._run_command(
                command,
                payload=payload,
                event=canonical,
                cwd=cwd,
                env=merged_env,
                timeout=timeout,
            )
            details.append(result["detail"])
            if result["feedback"]:
                feedback.append(result["feedback"])
            if result["blocked"] and not continue_on_error:
                blocked = True
                # Mirror Claude behavior: stop execution on first blocking hook.
                break

        return HookRunResult(
            blocked=blocked,
            feedback=feedback,
            details=details,
        )

    def _canonical_event_name(self, event: str) -> str:
        for canonical, aliases in self._ALIASES.items():
            if event == canonical or event in aliases:
                return canonical
        return event

    def _gather_hooks(self, canonical_event: str) -> list[dict[str, Any]]:
        keys = set(self._ALIASES.get(canonical_event, (canonical_event,)))
        hooks: list[dict[str, Any]] = []
        for key in keys:
            entries = self._hooks.get(key, [])
            if isinstance(entries, list):
                hooks.extend(item for item in entries if isinstance(item, dict))
        return hooks

    def _extract_command(self, cfg: dict[str, Any]) -> str:
        cmd = cfg.get("command") or cfg.get("cmd") or cfg.get("run")
        return cmd if isinstance(cmd, str) else ""

    def _extract_timeout(self, cfg: dict[str, Any]) -> int:
        raw = cfg.get("timeout")
        if isinstance(raw, int) and raw > 0:
            return raw
        return 60

    def _extract_continue_on_error(self, cfg: dict[str, Any]) -> bool:
        value = cfg.get("continue_on_error")
        if value is None:
            value = cfg.get("continueOnError")
        return bool(value)

    def _expand_entries(self, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        expanded: list[dict[str, Any]] = []
        for entry in entries:
            nested = entry.get("hooks")
            if isinstance(nested, list):
                for item in nested:
                    if not isinstance(item, dict):
                        continue
                    merged = dict(item)
                    if "matcher" not in merged and "matcher" in entry:
                        merged["matcher"] = entry["matcher"]
                    if "timeout" not in merged and "timeout" in entry:
                        merged["timeout"] = entry["timeout"]
                    if (
                        "continue_on_error" not in merged
                        and "continue_on_error" in entry
                    ):
                        merged["continue_on_error"] = entry["continue_on_error"]
                    if (
                        "continueOnError" not in merged
                        and "continueOnError" in entry
                    ):
                        merged["continueOnError"] = entry["continueOnError"]
                    hook_type = str(merged.get("type", "command")).strip().lower()
                    if hook_type in {"", "command"}:
                        expanded.append(merged)
            else:
                expanded.append(entry)
        return expanded

    def _matches(self, cfg: dict[str, Any], payload: dict[str, Any]) -> bool:
        matcher = cfg.get("matcher")
        if not matcher:
            return True

        tool_name = str(payload.get("tool_name") or "")
        if isinstance(matcher, str):
            if not tool_name:
                return False
            return tool_name == matcher or fnmatch(tool_name, matcher)
        if isinstance(matcher, list):
            if not tool_name:
                return False
            return any(
                isinstance(item, str) and (tool_name == item or fnmatch(tool_name, item))
                for item in matcher
            )
        if isinstance(matcher, dict):
            for key in ("tool_name", "tool", "name"):
                pattern = matcher.get(key)
                if pattern is None:
                    continue
                if isinstance(pattern, str):
                    if tool_name != pattern and not fnmatch(tool_name, pattern):
                        return False
                elif isinstance(pattern, list):
                    found = False
                    for item in pattern:
                        if not isinstance(item, str):
                            continue
                        if tool_name == item or fnmatch(tool_name, item):
                            found = True
                            break
                    if not found:
                        return False
            return True
        return True

    async def _run_command(
        self,
        command: str,
        *,
        payload: dict[str, Any],
        event: str,
        cwd: str,
        env: dict[str, str],
        timeout: int,
    ) -> dict[str, Any]:
        encoded = json.dumps(payload, ensure_ascii=False)
        proc_env = {
            **env,
            "CRABCODE_HOOK_EVENT": event,
            "CRABCODE_HOOK_PAYLOAD": encoded,
            "CRABCODE_HOOK_TOOL_NAME": str(payload.get("tool_name", "")),
            "CRABCODE_HOOK_TOOL_USE_ID": str(payload.get("tool_use_id", "")),
            "CRABCODE_HOOK_AGENT_ID": str(payload.get("agent_id", "")),
        }

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=proc_env,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                timed_out = False
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                stdout_b = b""
                stderr_b = f"Hook timed out after {timeout}s".encode()
                timed_out = True
        except Exception as exc:
            return {
                "blocked": True,
                "feedback": f"Hook failed to start: {exc}",
                "detail": f"hook-start-error: {exc}",
            }

        stdout = stdout_b.decode("utf-8", errors="replace").strip()
        stderr = stderr_b.decode("utf-8", errors="replace").strip()
        exit_code = proc.returncode if proc.returncode is not None else 1

        if timed_out:
            return {
                "blocked": True,
                "feedback": stderr or "Hook timed out.",
                "detail": f"hook timeout; command={command}",
            }

        feedback = stdout or stderr
        blocked = exit_code != 0
        detail = f"hook exit={exit_code}; command={command}"
        if stderr and stderr != feedback:
            detail += f"; stderr={stderr}"
        return {"blocked": blocked, "feedback": feedback, "detail": detail}
