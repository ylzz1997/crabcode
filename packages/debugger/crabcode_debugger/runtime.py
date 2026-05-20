"""Shared debugger runtime state for multiple tool instances."""

from __future__ import annotations

from crabcode_debugger.sessions import DebugSessionManager

_MANAGERS: dict[str, DebugSessionManager] = {}


def get_debug_session_manager(
    *,
    cwd: str,
    env: dict[str, str] | None = None,
    config: dict | None = None,
) -> DebugSessionManager:
    key = cwd
    manager = _MANAGERS.get(key)
    if manager is None:
        manager = DebugSessionManager(cwd=cwd, env=env, config=config)
        _MANAGERS[key] = manager
    else:
        manager.update_config(config)
    return manager
