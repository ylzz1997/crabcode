"""Permission management — controls tool execution authorization."""

from __future__ import annotations

import fnmatch
from enum import Enum
from typing import Any

from crabcode_core.types.config import PermissionRule, PermissionsSettings
from crabcode_core.types.tool import PermissionBehavior, PermissionResult, Tool


class PermissionMode(str, Enum):
    DEFAULT = "default"
    ACCEPT_EDITS = "acceptEdits"
    BYPASS = "bypassPermissions"
    PLAN = "plan"
    DONT_ASK = "dontAsk"
    AI_REVIEW = "aiReview"


def mode_from_default_mode(default_mode: str | None) -> PermissionMode:
    mode = (default_mode or "ask").strip()
    if mode in {"ask", "default"}:
        return PermissionMode.DEFAULT
    if mode in {"run_everything", "bypassPermissions"}:
        return PermissionMode.BYPASS
    if mode in {"aiReview", "ai_review"}:
        return PermissionMode.AI_REVIEW
    return PermissionMode.DEFAULT


class PermissionManager:
    """Manages tool permissions based on settings and mode."""

    def __init__(
        self,
        settings: PermissionsSettings | None = None,
        mode: PermissionMode = PermissionMode.DEFAULT,
    ):
        self.settings = settings or PermissionsSettings()
        self._runtime_allow_keys: set[str] = set()
        if self.settings.run_everything:
            self._configured_mode = PermissionMode.BYPASS
        elif self.settings.default_mode is not None:
            self._configured_mode = mode_from_default_mode(self.settings.default_mode)
        else:
            self._configured_mode = mode
        self.mode = self._configured_mode

    def reset_mode(self) -> None:
        """Restore the mode selected by the loaded settings."""
        self.mode = self._configured_mode

    def check(
        self,
        tool: Tool,
        tool_input: dict[str, Any],
    ) -> PermissionResult:
        """Check if a tool can be used with the given input."""
        if self.mode == PermissionMode.BYPASS:
            return PermissionResult(behavior=PermissionBehavior.ALLOW)

        if self.mode == PermissionMode.PLAN:
            if not tool.is_read_only:
                return PermissionResult(
                    behavior=PermissionBehavior.DENY,
                    reason="Plan mode: write operations are not allowed",
                )

        for rule in self.settings.deny:
            if self._matches_rule(rule, tool, tool_input):
                return PermissionResult(
                    behavior=PermissionBehavior.DENY,
                    reason=f"Denied by rule: {rule.tool}",
                )

        for rule in self.settings.allow:
            if self._matches_rule(rule, tool, tool_input):
                return PermissionResult(behavior=PermissionBehavior.ALLOW)

        for rule in self.settings.ask:
            if self._matches_rule(rule, tool, tool_input):
                return PermissionResult(
                    behavior=PermissionBehavior.ASK,
                    reason=f"Requires confirmation: {rule.tool}",
                )

        if tool.uses_tool_permission_policy:
            return PermissionResult(behavior=PermissionBehavior.ALLOW)

        if tool.is_read_only:
            return PermissionResult(behavior=PermissionBehavior.ALLOW)

        if self.mode == PermissionMode.ACCEPT_EDITS:
            return PermissionResult(behavior=PermissionBehavior.ALLOW)

        if self.mode == PermissionMode.DONT_ASK:
            return PermissionResult(
                behavior=PermissionBehavior.DENY,
                reason="dontAsk mode: denied by default",
            )

        return PermissionResult(behavior=PermissionBehavior.ASK)

    def add_allow_rule(self, permission_key: str) -> None:
        """Add a runtime allow rule (for 'always allow' during a session)."""
        self._runtime_allow_keys.add(permission_key)

    def has_explicit_allow(
        self,
        tool: Tool,
        tool_input: dict[str, Any],
        permission_key: str | None = None,
    ) -> bool:
        """Return whether an explicit allow rule matches this tool call."""
        key = permission_key or tool.get_permission_key(tool_input)
        if key in self._runtime_allow_keys:
            return True
        for rule in self.settings.allow:
            if self._matches_rule(rule, tool, tool_input):
                return True
        return False

    def _matches_rule(
        self,
        rule: PermissionRule,
        tool: Tool,
        tool_input: dict[str, Any],
    ) -> bool:
        if rule.tool != tool.name and rule.tool != "*":
            return False

        if rule.path:
            file_path = tool_input.get("file_path", "") or tool_input.get("path", "")
            if file_path and not fnmatch.fnmatch(file_path, rule.path):
                return False

        if rule.command:
            command = tool_input.get("command", "")
            if command and not fnmatch.fnmatch(command, rule.command):
                return False

        return True
