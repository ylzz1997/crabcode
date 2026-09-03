"""Inbox persistence — JSONL-based message storage for team inboxes.

Provides O(1) append writes and bulk read/mark_read operations.
This is the low-level storage layer; the TeamMessageBus uses it internally.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from crabcode_core.file_lock import file_lock
from crabcode_core.logging_utils import get_logger
from crabcode_core.path_validation import validate_path_component
from crabcode_core.team.models import TeamMessage

logger = get_logger(__name__)

class InboxStorage:
    """Manages JSONL inbox files for a team's agents.

    Directory structure:
        <root>/<team_name>/<agent_id>.jsonl

    Each line in the JSONL file is a serialized TeamMessage.
    Writes are O(1) (append). mark_read rewrites the full file,
    but only fires once per prompt loop completion, not per message.
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._locks: dict[tuple[str, str], threading.Lock] = {}

    @staticmethod
    def _validate_component(value: str, label: str) -> None:
        validate_path_component(value, label)

    def _lock_for(self, team_name: str, agent_id: str) -> threading.Lock:
        return self._locks.setdefault((team_name, agent_id), threading.Lock())

    @contextmanager
    def _team_file_lock(self, team_name: str, *, shared: bool = False) -> Iterator[None]:
        """Coordinate every operation for a team, including directory removal.

        The lock lives beside the team directory rather than inside it.  A
        lock file inside the directory would be deleted by ``delete_team``;
        a writer that resumed afterward could then create a new lock inode and
        recreate the supposedly deleted inbox.
        """
        self._root.mkdir(parents=True, exist_ok=True)
        lock_path = self._root / f".{team_name}.team.lock"
        with file_lock(lock_path, exclusive=not shared):
            yield

    def inbox_path(self, team_name: str, agent_id: str) -> Path:
        self._validate_component(team_name, "team name")
        self._validate_component(agent_id, "agent id")
        return self._root / team_name / f"{agent_id}.jsonl"

    def write(self, team_name: str, agent_id: str, message: TeamMessage) -> None:
        """Append a single message to an agent's inbox (O(1))."""
        path = self.inbox_path(team_name, agent_id)
        with self._lock_for(team_name, agent_id):
            with self._team_file_lock(team_name):
                path.parent.mkdir(parents=True, exist_ok=True)
                line = message.model_dump_json() + "\n"
                lock_path = path.with_name(f".{path.name}.lock")
                with file_lock(lock_path):
                    with open(path, "a", encoding="utf-8") as f:
                        f.write(line)
                        f.flush()
                        os.fsync(f.fileno())

    async def async_write(self, team_name: str, agent_id: str, message: TeamMessage) -> None:
        """Async version of write."""
        loop = asyncio.get_event_loop()
        operation = loop.run_in_executor(None, self.write, team_name, agent_id, message)
        try:
            await asyncio.shield(operation)
        except asyncio.CancelledError:
            # A thread-pool write cannot be cancelled safely.  Drain it before
            # propagating cancellation so a caller cannot delete or replace the
            # inbox while the append is still in flight.
            await asyncio.shield(operation)
            raise

    def read_all(self, team_name: str, agent_id: str) -> list[TeamMessage]:
        """Read all messages from an agent's inbox."""
        path = self.inbox_path(team_name, agent_id)
        with self._lock_for(team_name, agent_id):
            with self._team_file_lock(team_name, shared=True):
                try:
                    return self._read_all_locked(path)
                except FileNotFoundError:
                    # Team cleanup may remove the file before a shared reader
                    # acquires the per-file lock.
                    return []

    @staticmethod
    def _read_all_locked(path: Path) -> list[TeamMessage]:
        """Read a file while its caller holds the logical inbox lock."""
        messages: list[TeamMessage] = []
        lock_path = path.with_name(f".{path.name}.lock")
        with file_lock(lock_path, exclusive=False):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        messages.append(TeamMessage.model_validate_json(line))
                    except Exception:
                        logger.debug("Skipping invalid inbox line", exc_info=True)
        return messages

    def mark_read(self, team_name: str, agent_id: str, message_ids: set[str] | None = None) -> int:
        """Mark messages as read and rewrite the inbox file.

        If message_ids is None, marks all as read.
        Returns the count of messages marked read.
        """
        path = self.inbox_path(team_name, agent_id)
        with self._lock_for(team_name, agent_id):
            with self._team_file_lock(team_name):
                try:
                    messages = self._read_all_locked(path)
                except FileNotFoundError:
                    return 0
                count = 0
                for msg in messages:
                    if msg.read:
                        continue
                    if message_ids is not None and msg.id not in message_ids:
                        continue
                    msg.read = True
                    count += 1

                if count > 0:
                    self._rewrite(path, messages)
                return count

    def delete_team(self, team_name: str) -> None:
        """Delete all inbox files for a team."""
        self._validate_component(team_name, "team name")
        import shutil
        with self._team_file_lock(team_name):
            team_dir = self._root / team_name
            if team_dir.exists():
                shutil.rmtree(team_dir, ignore_errors=True)

    def _rewrite(self, path: Path, messages: list[TeamMessage]) -> None:
        """Rewrite the full inbox file (used by mark_read)."""
        lines = [msg.model_dump_json() + "\n" for msg in messages]
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_name(f".{path.name}.lock")
        with file_lock(lock_path):
            current = ""
            if path.exists():
                try:
                    current = path.read_text(encoding="utf-8")
                except OSError:
                    current = ""
            desired = "".join(lines)
            merged = self._merge_jsonl(current, desired)
            tmp_path: str | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=path.parent,
                    prefix=f".{path.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as f:
                    tmp_path = f.name
                    f.write(merged)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, path)
                tmp_path = None
            finally:
                if tmp_path is not None:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass

    @staticmethod
    def _merge_jsonl(current: str, desired: str) -> str:
        desired_by_id: dict[str, str] = {}
        for raw in desired.splitlines():
            if not raw.strip():
                continue
            try:
                message_id = str(json.loads(raw).get("id", ""))
            except Exception:
                message_id = ""
            if message_id:
                desired_by_id[message_id] = raw

        merged: list[str] = []
        for raw in current.splitlines():
            if not raw.strip():
                continue
            try:
                message_id = str(json.loads(raw).get("id", ""))
            except Exception:
                message_id = ""
            if message_id and message_id in desired_by_id:
                merged.append(
                    InboxStorage._merge_json_line(
                        raw,
                        desired_by_id.pop(message_id),
                    )
                )
            else:
                merged.append(raw)
        merged.extend(desired_by_id.values())
        return "".join(f"{line}\n" for line in merged)

    @staticmethod
    def _merge_json_line(current: str, desired: str) -> str:
        """Merge a line without allowing a read acknowledgement to regress."""
        try:
            current_obj = json.loads(current)
            desired_obj = json.loads(desired)
            if current_obj.get("read"):
                desired_obj["read"] = True
            return json.dumps(desired_obj, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            return desired
