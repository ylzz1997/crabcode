"""Git-based file system snapshots — track and revert code changes.

When the working directory is inside a git repo, snapshots use git internals
(hash-object, write-tree, update-ref) for lightweight, zero-commit snapshots.
When there is no git repo, files are copied into a ``.crabcode/snapshots``
store for tracking.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from crabcode_core.logging_utils import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class Patch:
    """A snapshot patch — the hash identifying it and the files it covers."""
    hash: str
    files: list[str] = field(default_factory=list)


@dataclass
class FileDiff:
    """Diff for a single file between two snapshots."""
    file: str
    patch: str
    additions: int = 0
    deletions: int = 0
    status: str = "modified"  # "added" | "deleted" | "modified"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SNAPSHOT_REFS_PREFIX = "refs/crabcode/"
_CRABCODE_DIR = ".crabcode"
_SNAPSHOTS_DIR = "snapshots"
_PRUNE_DAYS = 7
_MAX_DIFF_CHARS = 50_000


def _git(*args: str, cwd: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a git command and return the result."""
    cmd = ["git", "-c", "core.longpaths=true", "-c", "core.autocrlf=false", *args]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=cwd,
        check=check,
        timeout=30,
    )


def _is_git_repo(cwd: str) -> bool:
    """Check whether *cwd* is inside a git working tree."""
    try:
        r = _git("rev-parse", "--is-inside-work-tree", cwd=cwd, check=False)
        return r.stdout.strip() == "true"
    except Exception:
        return False


def _short_hash(h: str) -> str:
    return h[:12]


# ---------------------------------------------------------------------------
# SnapshotManager
# ---------------------------------------------------------------------------

class SnapshotManager:
    """Manages lightweight git-based snapshots of the working directory.

    Usage::

        mgr = SnapshotManager(cwd="/path/to/project")
        mgr.init()
        snap_id = mgr.track()          # create snapshot
        # ... files are modified ...
        diff_text = mgr.diff(snap_id)  # see what changed
        mgr.restore(snap_id)           # revert to snapshot
    """

    def __init__(self, cwd: str) -> None:
        self.cwd = os.path.abspath(cwd)
        self._is_git = _is_git_repo(self.cwd)
        self._snapshots_dir = Path(self.cwd) / _CRABCODE_DIR / _SNAPSHOTS_DIR

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def init(self) -> None:
        """Ensure snapshot infrastructure exists."""
        if self._is_git:
            # Nothing extra needed — we use git refs
            logger.debug("SnapshotManager: using git repo at %s", self.cwd)
        else:
            self._snapshots_dir.mkdir(parents=True, exist_ok=True)
            logger.debug("SnapshotManager: using file-based snapshots at %s", self._snapshots_dir)

    def cleanup(self) -> int:
        """Prune snapshots older than ``_PRUNE_DAYS``. Returns count removed."""
        if self._is_git:
            return self._cleanup_git_refs()
        return self._cleanup_file_snapshots()

    # ------------------------------------------------------------------
    # Track (create snapshot)
    # ------------------------------------------------------------------

    def track(self) -> str | None:
        """Create a snapshot of the current working directory state.

        Returns the snapshot ID (a hash string), or ``None`` on failure.
        """
        if self._is_git:
            return self._track_git()
        return self._track_file()

    # ------------------------------------------------------------------
    # Diff
    # ------------------------------------------------------------------

    def diff(self, snapshot_id: str) -> str:
        """Return a unified diff between the current state and *snapshot_id*."""
        if self._is_git:
            return self._diff_git(snapshot_id)
        return self._diff_file(snapshot_id)

    def diff_full(self, from_id: str, to_id: str) -> list[FileDiff]:
        """Return per-file diffs between two snapshots."""
        if self._is_git:
            return self._diff_full_git(from_id, to_id)
        return self._diff_full_file(from_id, to_id)

    # ------------------------------------------------------------------
    # Restore / Revert
    # ------------------------------------------------------------------

    def restore(self, snapshot_id: str) -> list[str]:
        """Restore the working directory to the state of *snapshot_id*.

        Returns a list of files that were restored.
        """
        if self._is_git:
            return self._restore_git(snapshot_id)
        return self._restore_file(snapshot_id)

    def revert(self, patches: list[Patch]) -> list[str]:
        """Revert a list of previously applied patches (snapshot IDs).

        Returns a list of files that were reverted.
        """
        all_files: list[str] = []
        for patch in reversed(patches):
            files = self.restore(patch.hash)
            all_files.extend(files)
        return list(dict.fromkeys(all_files))  # deduplicate preserving order

    # ------------------------------------------------------------------
    # Git-based implementation
    # ------------------------------------------------------------------

    def _track_git(self) -> str | None:
        try:
            # Stage all changes
            _git("add", "-A", cwd=self.cwd)
            # Write tree object — this is the snapshot
            r = _git("write-tree", cwd=self.cwd)
            tree_hash = r.stdout.strip()
            if not tree_hash:
                return None
            # Store a ref so the tree isn't garbage-collected
            ref_name = f"{_SNAPSHOT_REFS_PREFIX}{tree_hash}"
            _git("update-ref", ref_name, tree_hash, cwd=self.cwd, check=False)
            logger.info("Git snapshot created: %s", _short_hash(tree_hash))
            return tree_hash
        except Exception:
            logger.warning("Failed to create git snapshot", exc_info=True)
            return None

    def _diff_git(self, snapshot_id: str) -> str:
        try:
            r = _git("diff", snapshot_id, "--", cwd=self.cwd)
            text = r.stdout
            if len(text) > _MAX_DIFF_CHARS:
                text = text[:_MAX_DIFF_CHARS] + "\n... (diff truncated)"
            return text
        except Exception:
            logger.warning("Failed to diff git snapshot %s", _short_hash(snapshot_id), exc_info=True)
            return ""

    def _diff_full_git(self, from_id: str, to_id: str) -> list[FileDiff]:
        try:
            r = _git("diff-tree", "--no-commit-id", "-r", from_id, to_id, cwd=self.cwd)
            diffs: list[FileDiff] = []
            for line in r.stdout.strip().splitlines():
                # Format: :old_mode new_mode old_hash new_hash status\tfilename
                if not line.startswith(":"):
                    continue
                parts = line.split("\t", 1)
                if len(parts) < 2:
                    continue
                meta, filepath = parts
                status_char = meta.rsplit(" ", 1)[-1] if " " in meta else "M"
                status_map = {"A": "added", "D": "deleted", "M": "modified", "R": "modified"}
                status = status_map.get(status_char[0], "modified")
                diffs.append(FileDiff(file=filepath, patch="", status=status))
            return diffs
        except Exception:
            logger.warning("Failed to diff_full git snapshots", exc_info=True)
            return []

    def _restore_git(self, snapshot_id: str) -> list[str]:
        try:
            # Get list of changed files
            r = _git("diff-tree", "--no-commit-id", "-r", "--name-only", snapshot_id, "HEAD", cwd=self.cwd)
            changed_files = [f for f in r.stdout.strip().splitlines() if f]

            # Checkout all files from the snapshot tree
            _git("checkout", snapshot_id, "--", ".", cwd=self.cwd)

            # Unstage so working tree is clean but has the restored content
            _git("reset", "HEAD", "--", ".", cwd=self.cwd, check=False)

            logger.info("Restored git snapshot %s (%d files)", _short_hash(snapshot_id), len(changed_files))
            return changed_files
        except Exception:
            logger.warning("Failed to restore git snapshot %s", _short_hash(snapshot_id), exc_info=True)
            return []

    def _cleanup_git_refs(self) -> int:
        # Git refs don't have timestamps, so age-based pruning isn't straightforward.
        # They'll be collected by git gc eventually. Cap at 100 refs.
        return 0

    # ------------------------------------------------------------------
    # File-based (non-git) implementation
    # ------------------------------------------------------------------

    def _track_file(self) -> str | None:
        try:
            self._snapshots_dir.mkdir(parents=True, exist_ok=True)
            snap_hash = self._compute_directory_hash()
            snap_dir = self._snapshots_dir / snap_hash
            if snap_dir.exists():
                return snap_hash

            snap_dir.mkdir(parents=True, exist_ok=True)
            # Copy all files (excluding .crabcode, .git, __pycache__, node_modules)
            ignore_patterns = shutil.ignore_patterns(
                ".git", ".crabcode", "__pycache__", "node_modules", ".venv", "venv",
            )
            for item in Path(self.cwd).iterdir():
                if item.name.startswith(".") and item.name in (".git", ".crabcode"):
                    continue
                try:
                    if item.is_file():
                        dest = snap_dir / item.name
                        if not dest.exists():
                            shutil.copy2(str(item), str(dest))
                    elif item.is_dir():
                        dest = snap_dir / item.name
                        if not dest.exists():
                            shutil.copytree(str(item), str(dest), ignore=ignore_patterns)
                except Exception:
                    logger.debug("Failed to snapshot %s", item, exc_info=True)

            logger.info("File snapshot created: %s", _short_hash(snap_hash))
            return snap_hash
        except Exception:
            logger.warning("Failed to create file snapshot", exc_info=True)
            return None

    def _compute_directory_hash(self) -> str:
        """Compute a content hash of the working directory for snapshot ID."""
        h = hashlib.sha256()
        for root, dirs, files in os.walk(self.cwd):
            # Skip hidden/special directories
            dirs[:] = [d for d in dirs if d not in (".git", ".crabcode", "__pycache__", "node_modules", ".venv", "venv")]
            for fname in sorted(files):
                fpath = os.path.join(root, fname)
                try:
                    rel = os.path.relpath(fpath, self.cwd)
                    h.update(rel.encode())
                    stat = os.stat(fpath)
                    h.update(str(stat.st_mtime).encode())
                    h.update(str(stat.st_size).encode())
                except Exception:
                    pass
        return h.hexdigest()[:40]

    def _diff_file(self, snapshot_id: str) -> str:
        snap_dir = self._snapshots_dir / snapshot_id
        if not snap_dir.exists():
            return ""
        import difflib
        parts: list[str] = []
        for root, _dirs, files in os.walk(snap_dir):
            for fname in sorted(files):
                snap_file = Path(root) / fname
                rel = os.path.relpath(str(snap_file), str(snap_dir))
                current_file = Path(self.cwd) / rel
                try:
                    old_lines = snap_file.read_text(errors="replace").splitlines(keepends=True)
                except Exception:
                    old_lines = []
                try:
                    new_lines = current_file.read_text(errors="replace").splitlines(keepends=True) if current_file.exists() else []
                except Exception:
                    new_lines = []
                diff = list(difflib.unified_diff(old_lines, new_lines, fromfile=rel, tofile=rel))
                if diff:
                    parts.extend(diff)
        text = "\n".join(parts)
        if len(text) > _MAX_DIFF_CHARS:
            text = text[:_MAX_DIFF_CHARS] + "\n... (diff truncated)"
        return text

    def _diff_full_file(self, from_id: str, to_id: str) -> list[FileDiff]:
        # Simplified: just list files that differ between two snapshot dirs
        from_dir = self._snapshots_dir / from_id
        to_dir = self._snapshots_dir / to_id
        if not from_dir.exists() or not to_dir.exists():
            return []
        diffs: list[FileDiff] = []
        all_files = set()
        for d in (from_dir, to_dir):
            for root, _, files in os.walk(d):
                for f in files:
                    rel = os.path.relpath(os.path.join(root, f), str(d))
                    all_files.add(rel)
        for rel in sorted(all_files):
            from_file = from_dir / rel
            to_file = to_dir / rel
            if from_file.exists() and not to_file.exists():
                diffs.append(FileDiff(file=rel, patch="", status="deleted"))
            elif not from_file.exists() and to_file.exists():
                diffs.append(FileDiff(file=rel, patch="", status="added"))
            else:
                try:
                    if from_file.read_text(errors="replace") != to_file.read_text(errors="replace"):
                        diffs.append(FileDiff(file=rel, patch="", status="modified"))
                except Exception:
                    diffs.append(FileDiff(file=rel, patch="", status="modified"))
        return diffs

    def _restore_file(self, snapshot_id: str) -> list[str]:
        snap_dir = self._snapshots_dir / snapshot_id
        if not snap_dir.exists():
            logger.warning("File snapshot not found: %s", _short_hash(snapshot_id))
            return []
        restored: list[str] = []
        for root, _dirs, files in os.walk(snap_dir):
            for fname in sorted(files):
                snap_file = Path(root) / fname
                rel = os.path.relpath(str(snap_file), str(snap_dir))
                current_file = Path(self.cwd) / rel
                try:
                    current_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(snap_file), str(current_file))
                    restored.append(rel)
                except Exception:
                    logger.debug("Failed to restore %s", rel, exc_info=True)
        # Delete files that were added after the snapshot (exist in cwd but not in snapshot)
        for root, _dirs, files in os.walk(self.cwd):
            root_path = Path(root)
            if _is_under_special_dir(root_path, self.cwd):
                continue
            for fname in files:
                current_file = root_path / fname
                rel = os.path.relpath(str(current_file), self.cwd)
                snap_file = snap_dir / rel
                if not snap_file.exists():
                    try:
                        current_file.unlink()
                        restored.append(rel)
                    except Exception:
                        logger.debug("Failed to remove post-snapshot file %s", rel, exc_info=True)
        logger.info("Restored file snapshot %s (%d files)", _short_hash(snapshot_id), len(restored))
        return restored

    def _cleanup_file_snapshots(self) -> int:
        if not self._snapshots_dir.exists():
            return 0
        count = 0
        cutoff = time.time() - _PRUNE_DAYS * 86400
        for d in self._snapshots_dir.iterdir():
            if d.is_dir() and d.stat().st_mtime < cutoff:
                try:
                    shutil.rmtree(d)
                    count += 1
                except Exception:
                    logger.debug("Failed to prune snapshot %s", d, exc_info=True)
        return count


def _is_under_special_dir(path: Path, cwd: str) -> bool:
    """Check if *path* is under a directory we should skip (e.g. .git, .crabcode)."""
    try:
        rel = os.path.relpath(str(path), cwd)
        parts = Path(rel).parts
        skip = {".git", ".crabcode", "__pycache__", "node_modules", ".venv", "venv"}
        return any(p in skip for p in parts)
    except Exception:
        return True
