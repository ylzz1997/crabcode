"""Inbox persistence — JSONL-based message storage for team inboxes.

Provides O(1) append writes and bulk read/mark_read operations.
This is the low-level storage layer; the TeamMessageBus uses it internally.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from crabcode_core.logging_utils import get_logger
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

    def inbox_path(self, team_name: str, agent_id: str) -> Path:
        return self._root / team_name / f"{agent_id}.jsonl"

    def write(self, team_name: str, agent_id: str, message: TeamMessage) -> None:
        """Append a single message to an agent's inbox (O(1))."""
        path = self.inbox_path(team_name, agent_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = message.model_dump_json() + "\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)

    async def async_write(self, team_name: str, agent_id: str, message: TeamMessage) -> None:
        """Async version of write."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.write, team_name, agent_id, message)

    def read_all(self, team_name: str, agent_id: str) -> list[TeamMessage]:
        """Read all messages from an agent's inbox."""
        path = self.inbox_path(team_name, agent_id)
        if not path.exists():
            return []
        messages: list[TeamMessage] = []
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
        if not path.exists():
            return 0

        messages = self.read_all(team_name, agent_id)
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
        import shutil
        team_dir = self._root / team_name
        if team_dir.exists():
            shutil.rmtree(team_dir, ignore_errors=True)

    def _rewrite(self, path: Path, messages: list[TeamMessage]) -> None:
        """Rewrite the full inbox file (used by mark_read)."""
        lines = [msg.model_dump_json() + "\n" for msg in messages]
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(lines)
