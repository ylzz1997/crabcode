"""MCP configuration — multi-scope server config loading and merging."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from crabcode_core.types.config import McpServerConfig


def load_mcp_configs(cwd: str) -> dict[str, McpServerConfig]:
    """Load and merge MCP server configurations from multiple scopes.

    Scopes (later overrides earlier):
      1. User (~/.crabcode/mcp_servers.json)
      2. Project (<project>/.crabcode/mcp_servers.json)
      3. Local (<project>/.crabcode/mcp_servers.local.json)
    """
    merged: dict[str, dict[str, Any]] = {}
    home = Path.home()

    paths = [
        home / ".crabcode" / "mcp_servers.json",
        home / ".claude" / "mcp_servers.json",
        Path(cwd).resolve() / ".crabcode" / "mcp_servers.json",
        Path(cwd).resolve() / ".claude" / "mcp_servers.json",
        Path(cwd).resolve() / ".crabcode" / "mcp_servers.local.json",
    ]

    for path in paths:
        if not path.exists():
            continue
        try:
            raw = json.loads(path.read_text(errors="replace"))
            if isinstance(raw, dict):
                servers = raw.get("mcpServers", raw)
                if isinstance(servers, dict):
                    for name, config in servers.items():
                        if isinstance(config, dict):
                            merged[name] = {**merged.get(name, {}), **config}
        except (json.JSONDecodeError, OSError):
            continue

    result: dict[str, McpServerConfig] = {}
    for name, raw in merged.items():
        try:
            result[name] = McpServerConfig.model_validate(raw)
        except Exception:
            pass

    return result
