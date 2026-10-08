"""Shared locations for CrabCode's user-level configuration and state."""

from __future__ import annotations

import os
from pathlib import Path


def get_config_home() -> Path:
    """Return CRABCODE_HOME, or ~/.crabcode when unset or blank.

    Require an absolute directory so processes launched in different projects
    share the same home. A leading ``~`` for the current user is supported.
    This function does not create the directory or migrate existing data.
    """
    override = os.environ.get("CRABCODE_HOME", "").strip()
    if not override:
        return Path.home() / ".crabcode"
    path = Path(override)
    if path.parts and path.parts[0] == "~":
        path = Path.home().joinpath(*path.parts[1:])
    if not path.is_absolute():
        raise ValueError("CRABCODE_HOME must be an absolute directory path (or start with ~/)")
    return path
