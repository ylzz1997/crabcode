"""Shared debugger runtime state for multiple tool instances."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from crabcode_debugger.sessions import DebugSessionManager


@dataclass
class _ManagerEntry:
    manager: DebugSessionManager
    users: int = 1


_MANAGERS: dict[tuple[str, str], _ManagerEntry] = {}


def _manager_key(cwd: str, owner_id: str) -> tuple[str, str]:
    return (str(Path(cwd).resolve()), owner_id or "legacy")


def get_debug_session_manager(
    *,
    cwd: str,
    owner_id: str = "",
    env: dict[str, str] | None = None,
    config: dict | None = None,
) -> DebugSessionManager:
    key = _manager_key(cwd, owner_id)
    entry = _MANAGERS.get(key)
    if entry is None:
        manager = DebugSessionManager(cwd=cwd, env=env, config=config)
        _MANAGERS[key] = _ManagerEntry(manager=manager)
    else:
        manager = entry.manager
        entry.users += 1
        manager.update_config(config)
    return manager


async def release_debug_session_manager(
    *,
    cwd: str,
    owner_id: str = "",
    manager: DebugSessionManager,
) -> None:
    """Release one tool's reference and close the manager after the last user."""
    key = _manager_key(cwd, owner_id)
    entry = _MANAGERS.get(key)
    if entry is None or entry.manager is not manager:
        return
    entry.users -= 1
    if entry.users > 0:
        return
    _MANAGERS.pop(key, None)
    await manager.close()
