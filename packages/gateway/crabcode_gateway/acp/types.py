"""ACP internal types — session state and configuration.

Maps CrabCode session/model concepts to ACP's expectations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from acp.schema import McpServerStdio, HttpMcpServer, SseMcpServer


@dataclass
class ACPSessionState:
    """Tracks ACP-specific state for a single session.

    Stored in ACPSessionManager; not persisted to disk.
    """

    id: str
    cwd: str
    mcp_servers: list[McpServerStdio | HttpMcpServer | SseMcpServer] = field(default_factory=list)
    created_at: float = 0.0
    model: ModelSelection | None = None
    mode_id: str | None = None
    variant: str | None = None


@dataclass
class ModelSelection:
    """Identifies a provider + model pair."""

    provider_id: str
    model_id: str


@dataclass
class ACPConfig:
    """Top-level configuration passed to the ACP Agent on construction."""

    base_url: str  # e.g. "http://127.0.0.1:4096"
    default_model: ModelSelection | None = None


# ── Tool kind mapping ──────────────────────────────────────────


def to_tool_kind(tool_name: str) -> str:
    """Map CrabCode tool names to ACP ToolKind literals.

    ACP ToolKind: read | edit | delete | move | search | execute |
                  think | fetch | switch_mode | other
    """
    tool = tool_name.lower()
    if tool == "bash":
        return "execute"
    if tool == "web_search":
        return "fetch"
    if tool in ("edit", "patch", "file_edit", "file_write", "write"):
        return "edit"
    if tool in ("grep", "glob"):
        return "search"
    if tool == "read" or tool == "file_read":
        return "read"
    if tool == "delete":
        return "delete"
    if tool == "switch_mode":
        return "switch_mode"
    return "other"


def to_locations(tool_name: str, tool_input: dict[str, Any]) -> list[dict[str, str]]:
    """Extract file paths from tool input for ACP ToolCallLocation."""
    tool = tool_name.lower()
    if tool in ("read", "file_read", "edit", "file_edit", "write", "file_write"):
        path = tool_input.get("filePath") or tool_input.get("file_path") or ""
        return [{"path": path}] if path else []
    if tool in ("glob", "grep"):
        path = tool_input.get("path") or ""
        return [{"path": path}] if path else []
    return []
