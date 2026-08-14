"""Shared synchronization for the gateway's in-process session registry."""

from __future__ import annotations

import asyncio
from typing import Any

_LOAD_LOCK_ATTR = "session_load_lock"


def get_session_lock(app_state: Any) -> asyncio.Lock:
    """Return the app-wide registry lock, creating it for bare test apps.

    FastAPI applications built by :class:`GatewayServer` initialize this lock
    eagerly.  The lazy fallback keeps route helpers usable with lightweight
    ASGI apps in tests and integrations.
    """
    lock = getattr(app_state, "session_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        app_state.session_lock = lock
    return lock


def get_session_load_lock(app_state: Any) -> asyncio.Lock:
    """Return the lock serializing disk-backed session loads.

    Session initialization can await provider/LSP setup and must not hold the
    short registry lock while doing so.  A separate lock still prevents two
    concurrent resume calls from constructing duplicate CoreSession objects.
    """
    lock = getattr(app_state, _LOAD_LOCK_ATTR, None)
    if lock is None:
        lock = asyncio.Lock()
        setattr(app_state, _LOAD_LOCK_ATTR, lock)
    return lock
