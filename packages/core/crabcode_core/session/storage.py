"""Session storage — JSONL-based conversation persistence."""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from crabcode_core.types.message import Message


def get_config_home() -> Path:
    """Get the CrabCode config directory (~/.crabcode/)."""
    return Path.home() / ".crabcode"


def get_projects_dir() -> Path:
    """Get the projects directory for session storage."""
    return get_config_home() / "projects"


def _sanitize_path(path: str) -> str:
    """Sanitize a filesystem path for use as a directory name."""
    sanitized = re.sub(r'[^\w\-.]', '_', path)
    if len(sanitized) > 200:
        sanitized = sanitized[:200]
    return sanitized


def get_project_dir(cwd: str) -> Path:
    """Get the project-specific session directory."""
    return get_projects_dir() / _sanitize_path(os.path.abspath(cwd))


def get_transcript_path(cwd: str, session_id: str) -> Path:
    """Get the path for a session transcript file."""
    return get_project_dir(cwd) / f"{session_id}.jsonl"


def generate_session_id() -> str:
    return str(uuid.uuid4())


class SessionStorage:
    """Manages session persistence using JSONL files."""

    def __init__(self, cwd: str, session_id: str | None = None):
        self.cwd = cwd
        self.session_id = session_id or generate_session_id()
        self._transcript_path = get_transcript_path(cwd, self.session_id)
        self._initialized = False
        self._written_uuids: set[str] = set()

    def _ensure_dir(self) -> None:
        if not self._initialized:
            self._transcript_path.parent.mkdir(parents=True, exist_ok=True)
            self._initialized = True

    def append_message(self, message: Message) -> None:
        """Append a message to the session transcript (skips duplicates by uuid)."""
        if message.uuid in self._written_uuids:
            return
        self._written_uuids.add(message.uuid)

        self._ensure_dir()
        entry = {
            "type": message.role.value,
            "uuid": message.uuid,
            "parent_uuid": message.parent_uuid,
            "timestamp": message.timestamp,
            "content": message.content if isinstance(message.content, str) else [
                block.model_dump() for block in message.content
            ],
        }
        with open(self._transcript_path, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def load_messages(self) -> list[dict[str, Any]]:
        """Load all messages from the session transcript (deduped by uuid)."""
        if not self._transcript_path.exists():
            return []

        messages: list[dict[str, Any]] = []
        seen: set[str] = set()
        try:
            with open(self._transcript_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    msg_uuid = entry.get("uuid", "")
                    if msg_uuid and msg_uuid in seen:
                        continue
                    if msg_uuid:
                        seen.add(msg_uuid)
                    messages.append(entry)
        except Exception:
            pass

        self._written_uuids = seen
        return messages

    @staticmethod
    def list_sessions(cwd: str) -> list[dict[str, Any]]:
        """List all sessions for a given working directory."""
        project_dir = get_project_dir(cwd)
        if not project_dir.exists():
            return []

        sessions: list[dict[str, Any]] = []
        for path in sorted(project_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
            session_id = path.stem
            try:
                stat = path.stat()
                with open(path) as f:
                    first_line = f.readline().strip()
                    first_msg = json.loads(first_line) if first_line else {}

                sessions.append({
                    "session_id": session_id,
                    "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                    "size": stat.st_size,
                    "preview": _extract_preview(first_msg),
                })
            except Exception:
                sessions.append({
                    "session_id": session_id,
                    "modified": "",
                    "size": 0,
                    "preview": "",
                })

        return sessions


def _extract_preview(msg: dict[str, Any]) -> str:
    """Extract a short preview from a message entry."""
    content = msg.get("content", "")
    if isinstance(content, str):
        return content[:100]
    if isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict) and "text" in first:
            return first["text"][:100]
    return ""
