"""Lightweight snapshot tracker — bridges tools and SnapshotManager.

Provides module-level convenience functions so that tools (Edit, Write, Bash)
can record file changes without managing a SnapshotManager instance themselves.
"""

from __future__ import annotations

import json
import os
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from crabcode_core.logging_utils import get_logger
from crabcode_core.snapshot.snapshot import SnapshotManager

logger = get_logger(__name__)

_CRABCODE_DIR = ".crabcode"
_SNAPSHOTS_DIR = "snapshots"

# A Bash call can be issued from a workspace container such as the user's
# home directory.  Taking a file snapshot there would recursively copy the
# whole machine-owned workspace and can exhaust disk space before the command
# even starts.  These roots are navigation/workspace containers, not projects.
def _is_broad_workspace(cwd: str) -> bool:
    """Return whether *cwd* is too broad for an automatic file snapshot."""
    try:
        path = Path(cwd).expanduser().resolve()
        home = Path.home().resolve()
        if path == home or path == Path(path.anchor):
            return True
        return False
    except (OSError, RuntimeError, ValueError):
        # Snapshotting is best effort.  An unresolvable path should not block
        # the command that requested it.
        return True


def _workspace_exceeds_limit(cwd: str, max_size_mb: int) -> bool:
    """Return whether a workspace exceeds the snapshot size ceiling.

    The walk is bounded: it stops at the first byte over the limit and skips
    generated/dependency trees that SnapshotManager would not copy anyway.
    Permission and stat failures fail closed so a damaged workspace cannot
    trigger an unbounded snapshot.
    """
    if max_size_mb <= 0:
        return True
    limit = max_size_mb * 1024 * 1024
    try:
        root = Path(cwd).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return True
    if not root.is_dir():
        return True
    skipped = {".git", ".crabcode", "__pycache__", "node_modules", ".venv", "venv"}
    total = 0
    walk_failed = False

    def _on_walk_error(_error: OSError) -> None:
        nonlocal walk_failed
        walk_failed = True

    try:
        for current, dirs, files in os.walk(
            root,
            topdown=True,
            onerror=_on_walk_error,
            followlinks=False,
        ):
            if walk_failed:
                return True
            dirs[:] = [name for name in dirs if name not in skipped]
            for name in files:
                path = Path(current) / name
                try:
                    if path.is_symlink():
                        continue
                    file_stat = path.stat()
                except OSError:
                    return True
                if not stat.S_ISREG(file_stat.st_mode):
                    continue
                total += file_stat.st_size
                if total > limit:
                    return True
        if walk_failed:
            return True
    except OSError:
        return True
    return False


def _snapshot_is_allowed(cwd: str, enabled: bool, max_size_mb: int) -> bool:
    if not enabled or _is_broad_workspace(cwd):
        return False
    return not _workspace_exceeds_limit(cwd, max_size_mb)


@dataclass
class SnapshotInfo:
    """A recorded snapshot entry for a session."""
    snapshot_id: str
    session_id: str
    timestamp: float
    tool_name: str
    files: list[str] = field(default_factory=list)
    action: str = ""  # "modify" | "create" | "delete" | "bash"


def _session_log_path(cwd: str, session_id: str) -> Path:
    return Path(cwd) / _CRABCODE_DIR / _SNAPSHOTS_DIR / f"{session_id}.jsonl"


def _ensure_dir(cwd: str) -> Path:
    d = Path(cwd) / _CRABCODE_DIR / _SNAPSHOTS_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _append_log(cwd: str, session_id: str, entry: dict[str, Any]) -> None:
    """Append a snapshot entry to the session's JSONL log."""
    _ensure_dir(cwd)
    log_path = _session_log_path(cwd, session_id)
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        logger.debug("Failed to append snapshot log: %s", log_path, exc_info=True)


# ---------------------------------------------------------------------------
# Public API — called from tools
# ---------------------------------------------------------------------------

def track_snapshot_for_file(
    cwd: str,
    session_id: str,
    file_path: str,
    old_content: str | None = None,
    action: str = "modify",
) -> None:
    """Record that a file is about to be changed.

    If *old_content* is provided, it is saved to the snapshot store so
    the file can be restored later even without git.
    """
    # Create a quick snapshot ID from the file path + timestamp
    snap_id = _make_file_snapshot_id(file_path, action)

    # If old_content is given, persist it for non-git restore
    if old_content is not None:
        _save_file_backup(cwd, session_id, snap_id, file_path, old_content)

    _append_log(cwd, session_id, {
        "snapshot_id": snap_id,
        "session_id": session_id,
        "timestamp": time.time(),
        "tool_name": "file_edit",
        "files": [file_path],
        "action": action,
    })


def pre_bash_snapshot(
    cwd: str,
    session_id: str,
    *,
    enabled: bool = True,
    max_size_mb: int = 1024,
) -> str | None:
    """Create a full working-directory snapshot before a bash command.

    Returns the snapshot ID, or ``None`` on failure.
    """
    if not _snapshot_is_allowed(cwd, enabled, max_size_mb):
        logger.info("Skipping Bash snapshot for workspace or size policy: %s", cwd)
        return None

    try:
        mgr = SnapshotManager(cwd)
        mgr.init()
        snap_id = mgr.track()
        if snap_id:
            _append_log(cwd, session_id, {
                "snapshot_id": snap_id,
                "session_id": session_id,
                "timestamp": time.time(),
                "tool_name": "Bash",
                "files": [],
                "action": "bash",
            })
        return snap_id
    except Exception:
        logger.debug("pre_bash_snapshot failed", exc_info=True)
        return None


def get_session_snapshots(cwd: str, session_id: str) -> list[SnapshotInfo]:
    """Return all snapshot entries recorded for a session."""
    log_path = _session_log_path(cwd, session_id)
    if not log_path.exists():
        return []
    entries: list[SnapshotInfo] = []
    try:
        with open(log_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                entries.append(SnapshotInfo(
                    snapshot_id=d.get("snapshot_id", ""),
                    session_id=d.get("session_id", ""),
                    timestamp=d.get("timestamp", 0),
                    tool_name=d.get("tool_name", ""),
                    files=d.get("files", []),
                    action=d.get("action", ""),
                ))
    except Exception:
        logger.debug("Failed to read session snapshots: %s", log_path, exc_info=True)
    return entries


def restore_snapshot(cwd: str, snapshot_id: str) -> list[str]:
    """Restore the working directory to a given snapshot.

    Returns the list of files restored.
    """
    # First try git-based restore
    try:
        mgr = SnapshotManager(cwd)
        mgr.init()
        files = mgr.restore(snapshot_id)
        if files:
            return files
    except Exception:
        logger.debug("Git restore failed for %s, trying file backup", snapshot_id, exc_info=True)

    # Fallback: restore from file backups stored by track_snapshot_for_file
    return _restore_file_backups(cwd, snapshot_id)


def create_full_snapshot(
    cwd: str,
    session_id: str,
    label: str = "",
    *,
    enabled: bool = True,
    max_size_mb: int = 1024,
) -> str | None:
    """Create a full working-directory snapshot and return its ID.

    This is used by /checkpoint to create a file-system-level snapshot
    alongside the conversation checkpoint.
    """
    if not _snapshot_is_allowed(cwd, enabled, max_size_mb):
        logger.info("Skipping full snapshot for workspace or size policy: %s", cwd)
        return None

    try:
        mgr = SnapshotManager(cwd)
        mgr.init()
        snap_id = mgr.track()
        if snap_id:
            _append_log(cwd, session_id, {
                "snapshot_id": snap_id,
                "session_id": session_id,
                "timestamp": time.time(),
                "tool_name": "checkpoint",
                "files": [],
                "action": "checkpoint",
                "label": label,
            })
        return snap_id
    except Exception:
        logger.warning("Failed to create full snapshot for checkpoint", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# File-backup helpers (for non-git or per-file granularity)
# ---------------------------------------------------------------------------

def _file_backup_dir(cwd: str, session_id: str) -> Path:
    return Path(cwd) / _CRABCODE_DIR / _SNAPSHOTS_DIR / "files" / session_id


def _make_file_snapshot_id(file_path: str, action: str) -> str:
    import hashlib
    h = hashlib.sha256(f"{file_path}:{action}:{time.time()}".encode()).hexdigest()
    return h[:16]


def _save_file_backup(
    cwd: str,
    session_id: str,
    snap_id: str,
    file_path: str,
    content: str,
) -> None:
    d = _file_backup_dir(cwd, session_id) / snap_id
    d.mkdir(parents=True, exist_ok=True)
    # Store with the file's basename; original path is in the log
    backup_file = d / "content"
    try:
        backup_file.write_text(content, encoding="utf-8")
        (d / "path").write_text(file_path, encoding="utf-8")
    except Exception:
        logger.debug("Failed to save file backup: %s/%s", snap_id, file_path, exc_info=True)


def _restore_file_backups(cwd: str, snapshot_id: str) -> list[str]:
    """Restore files from file backups for a given snapshot ID.

    This is a best-effort fallback — it scans all sessions' backup dirs
    for the snapshot_id.
    """
    files_root = Path(cwd) / _CRABCODE_DIR / _SNAPSHOTS_DIR / "files"
    if not files_root.exists():
        return []
    restored: list[str] = []
    for session_dir in files_root.iterdir():
        if not session_dir.is_dir():
            continue
        snap_dir = session_dir / snapshot_id
        if not snap_dir.exists():
            continue
        content_file = snap_dir / "content"
        path_file = snap_dir / "path"
        if content_file.exists() and path_file.exists():
            try:
                file_path = path_file.read_text(encoding="utf-8").strip()
                content = content_file.read_text(encoding="utf-8")
                target = Path(file_path)
                if not target.is_absolute():
                    target = Path(cwd) / file_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                restored.append(str(target))
            except Exception:
                logger.debug("Failed to restore backup %s", snap_dir, exc_info=True)
    return restored
