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


class PermissionManager:
    """Manages tool permissions based on settings and mode."""

    def __init__(
        self,
        settings: PermissionsSettings | None = None,
        mode: PermissionMode = PermissionMode.DEFAULT,
    ):
        self.settings = settings or PermissionsSettings()
        if self.settings.run_everything:
            self.mode = PermissionMode.BYPASS
        else:
            self.mode = mode

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

    def add_allow_rule(self, tool_name: str) -> None:
        """Add a runtime allow rule (for 'always allow' during a session)."""
        self.settings.allow.append(PermissionRule(tool=tool_name))

    def has_explicit_allow(
        self,
        tool: Tool,
        tool_input: dict[str, Any],
    ) -> bool:
        """Return whether an explicit allow rule matches this tool call."""
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
