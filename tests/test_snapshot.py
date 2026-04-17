"""Tests for the snapshot + revert feature."""

from __future__ import annotations

import os
import subprocess

import pytest

from crabcode_core.snapshot.snapshot import SnapshotManager
from crabcode_core.snapshot.tracker import (
    create_full_snapshot,
    get_session_snapshots,
    pre_bash_snapshot,
    track_snapshot_for_file,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def git_project(tmp_path):
    """Create a temporary git project with one file."""
    cwd = str(tmp_path)
    subprocess.run(["git", "init", cwd], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=cwd, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=cwd, check=True, capture_output=True)
    test_file = tmp_path / "hello.py"
    test_file.write_text("print('hello')\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=cwd, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=cwd, check=True, capture_output=True)
    return cwd


@pytest.fixture
def plain_project(tmp_path):
    """Create a temporary non-git project directory."""
    cwd = str(tmp_path)
    test_file = tmp_path / "hello.py"
    test_file.write_text("print('hello')\n", encoding="utf-8")
    return cwd


# ---------------------------------------------------------------------------
# SnapshotManager — git mode
# ---------------------------------------------------------------------------

class TestSnapshotManagerGit:
    def test_track_returns_snapshot_id(self, git_project):
        mgr = SnapshotManager(git_project)
        mgr.init()
        snap_id = mgr.track()
        assert snap_id is not None
        assert len(snap_id) >= 7  # git hash

    def test_diff_shows_changes(self, git_project):
        mgr = SnapshotManager(git_project)
        mgr.init()
        snap_id = mgr.track()
        assert snap_id is not None
        # Modify a file
        with open(os.path.join(git_project, "hello.py"), "w") as f:
            f.write("print('world')\n")
        diff = mgr.diff(snap_id)
        assert "world" in diff
        assert "hello" in diff

    def test_restore_reverts_files(self, git_project):
        mgr = SnapshotManager(git_project)
        mgr.init()
        snap_id = mgr.track()
        # Modify a file
        with open(os.path.join(git_project, "hello.py"), "w") as f:
            f.write("print('world')\n")
        # Restore
        files = mgr.restore(snap_id)
        assert len(files) > 0
        # File should be back to original
        with open(os.path.join(git_project, "hello.py")) as f:
            assert "hello" in f.read()

    def test_multiple_snapshots(self, git_project):
        mgr = SnapshotManager(git_project)
        mgr.init()
        snap1 = mgr.track()
        with open(os.path.join(git_project, "hello.py"), "w") as f:
            f.write("v2\n")
        snap2 = mgr.track()
        assert snap1 != snap2
        # Restore to snap1
        mgr.restore(snap1)
        with open(os.path.join(git_project, "hello.py")) as f:
            assert "hello" in f.read() or "print" in f.read()

    def test_new_file_tracking(self, git_project):
        mgr = SnapshotManager(git_project)
        mgr.init()
        snap_id = mgr.track()
        # Add a new file
        with open(os.path.join(git_project, "new_file.py"), "w") as f:
            f.write("# new\n")
        diff = mgr.diff(snap_id)
        assert "new_file" in diff or "new_file.py" in diff

    def test_file_deletion_tracking(self, git_project):
        mgr = SnapshotManager(git_project)
        mgr.init()
        snap_id = mgr.track()
        os.remove(os.path.join(git_project, "hello.py"))
        diff = mgr.diff(snap_id)
        # Diff should mention the deleted file
        assert "hello" in diff


# ---------------------------------------------------------------------------
# SnapshotManager — file (non-git) mode
# ---------------------------------------------------------------------------

class TestSnapshotManagerFile:
    def test_track_returns_snapshot_id(self, plain_project):
        mgr = SnapshotManager(plain_project)
        mgr.init()
        snap_id = mgr.track()
        assert snap_id is not None

    def test_restore_reverts_files(self, plain_project):
        mgr = SnapshotManager(plain_project)
        mgr.init()
        snap_id = mgr.track()
        # Modify file
        with open(os.path.join(plain_project, "hello.py"), "w") as f:
            f.write("print('world')\n")
        # Restore
        files = mgr.restore(snap_id)
        assert len(files) > 0
        with open(os.path.join(plain_project, "hello.py")) as f:
            assert "hello" in f.read()

    def test_diff_shows_changes(self, plain_project):
        mgr = SnapshotManager(plain_project)
        mgr.init()
        snap_id = mgr.track()
        with open(os.path.join(plain_project, "hello.py"), "w") as f:
            f.write("print('world')\n")
        diff = mgr.diff(snap_id)
        assert "world" in diff or "hello" in diff


# ---------------------------------------------------------------------------
# Tracker — per-file and session-level tracking
# ---------------------------------------------------------------------------

class TestTracker:
    def test_track_snapshot_for_file(self, tmp_path):
        cwd = str(tmp_path)
        session_id = "test-session-1"
        test_file = str(tmp_path / "foo.py")
        with open(test_file, "w") as f:
            f.write("old content")

        track_snapshot_for_file(
            cwd=cwd,
            session_id=session_id,
            file_path=test_file,
            old_content="old content",
            action="modify",
        )
        snapshots = get_session_snapshots(cwd, session_id)
        assert len(snapshots) >= 1
        assert snapshots[0].action == "modify"
        assert test_file in snapshots[0].files

    def test_pre_bash_snapshot(self, git_project):
        snap_id = pre_bash_snapshot(git_project, "test-session-bash")
        # May return None if git operations fail, but should not raise
        # In a proper git project, it should return a snapshot ID
        if snap_id:
            snapshots = get_session_snapshots(git_project, "test-session-bash")
            assert any(s.action == "bash" for s in snapshots)

    def test_create_full_snapshot(self, git_project):
        snap_id = create_full_snapshot(git_project, "test-session-full", label="test")
        assert snap_id is not None
        snapshots = get_session_snapshots(git_project, "test-session-full")
        assert any(s.action == "checkpoint" for s in snapshots)

    def test_get_session_snapshots_empty(self, tmp_path):
        snapshots = get_session_snapshots(str(tmp_path), "nonexistent-session")
        assert snapshots == []


# ---------------------------------------------------------------------------
# Integration — CoreSession revert
# ---------------------------------------------------------------------------

class TestCoreSessionRevert:
    def test_revert_restores_files_and_conversation(self, git_project):
        from crabcode_core.events import CoreSession
        from crabcode_core.types.config import CrabCodeSettings
        from crabcode_core.types.message import create_user_message

        session = CoreSession(cwd=git_project, settings=CrabCodeSettings())
        # We need to manually set up storage since we're not calling initialize()
        from crabcode_core.session.storage import SessionStorage
        session._session_storage = SessionStorage(git_project, "test-revert-session")
        session.session_id = "test-revert-session"
        session._session_storage.write_meta(model="test", provider="test")

        # Add some messages
        session.messages.append(create_user_message(content="hello"))
        session.messages.append(create_user_message(content="world"))

        # Create checkpoint (this also creates a file snapshot)
        cp_id = session.checkpoint(label="before-edit")
        assert cp_id is not None

        # Modify a file
        with open(os.path.join(git_project, "hello.py"), "w") as f:
            f.write("print('modified')\n")

        # Add more messages
        session.messages.append(create_user_message(content="after edit"))

        # Revert
        result = session.revert(cp_id)
        assert result["success"] is True
        assert result["messages_rolled_back"] >= 1

    def test_revert_without_snapshot_warns(self, tmp_path):
        """Old checkpoints without file snapshots should still work for conversation."""
        from crabcode_core.events import CoreSession
        from crabcode_core.types.config import CrabCodeSettings
        from crabcode_core.types.message import create_user_message

        cwd = str(tmp_path)
        session = CoreSession(cwd=cwd, settings=CrabCodeSettings())
        from crabcode_core.session.storage import SessionStorage
        session._session_storage = SessionStorage(cwd, "test-no-snap-session")
        session.session_id = "test-no-snap-session"
        session._session_storage.write_meta(model="test", provider="test")

        session.messages.append(create_user_message(content="msg1"))
        session.messages.append(create_user_message(content="msg2"))

        # Create checkpoint with no snapshot (pass None explicitly)
        cp_id = session._session_storage.create_checkpoint(
            session.messages, label="no-snap", snapshot_id=None
        )
        assert cp_id is not None

        session.messages.append(create_user_message(content="msg3"))
        result = session.revert(cp_id)
        assert result["success"] is True
        assert result["warning"] is not None
        assert "No file snapshot" in result["warning"]


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_empty_directory(self, tmp_path):
        cwd = str(tmp_path)
        mgr = SnapshotManager(cwd)
        mgr.init()
        snap_id = mgr.track()
        # Should still return a snapshot ID (or None gracefully)
        # In a non-git empty dir, file-based mode should work
        if snap_id:
            diff = mgr.diff(snap_id)
            assert isinstance(diff, str)

    def test_restore_nonexistent_snapshot(self, git_project):
        mgr = SnapshotManager(git_project)
        mgr.init()
        files = mgr.restore("nonexistent0000000")
        assert files == []

    def test_diff_nonexistent_snapshot(self, git_project):
        mgr = SnapshotManager(git_project)
        mgr.init()
        diff = mgr.diff("nonexistent0000000")
        assert diff == ""

    def test_cleanup_does_not_crash(self, git_project):
        mgr = SnapshotManager(git_project)
        mgr.init()
        mgr.track()
        # cleanup should not raise
        mgr.cleanup()
