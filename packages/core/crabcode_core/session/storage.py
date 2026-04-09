"""Session storage — JSONL-based conversation persistence with SQLite metadata."""

from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from crabcode_core.types.message import Message
from crabcode_core.utf8_sanitize import safe_utf8_json_tree


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


def _dump_jsonl_line(obj: Any) -> str:
    return json.dumps(safe_utf8_json_tree(obj), ensure_ascii=False) + "\n"


def get_project_dir(cwd: str) -> Path:
    """Get the project-specific session directory."""
    return get_projects_dir() / _sanitize_path(os.path.abspath(cwd))


def get_transcript_path(cwd: str, session_id: str) -> Path:
    """Get the path for a session transcript file."""
    return get_project_dir(cwd) / f"{session_id}.jsonl"


def generate_session_id() -> str:
    return str(uuid.uuid4())


def _get_git_info(cwd: str) -> dict[str, str | None]:
    """Get git branch and SHA for the cwd."""
    info: dict[str, str | None] = {"git_branch": None, "git_sha": None}
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, cwd=cwd, timeout=5,
        )
        if result.stdout.strip() != "true":
            return info
        branch = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True, text=True, cwd=cwd, timeout=5,
        ).stdout.strip()
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, cwd=cwd, timeout=5,
        ).stdout.strip()
        if branch:
            info["git_branch"] = branch
        if sha:
            info["git_sha"] = sha
    except Exception:
        pass
    return info


class SessionStorage:
    """Manages session persistence using JSONL files + SQLite metadata."""

    def __init__(self, cwd: str, session_id: str | None = None):
        self.cwd = os.path.abspath(cwd)
        self.session_id = session_id or generate_session_id()
        self._transcript_path = get_transcript_path(self.cwd, self.session_id)
        self._initialized = False
        self._written_uuids: set[str] = set()
        self._meta_written = False
        self._meta: dict[str, Any] = {}

    def _ensure_dir(self) -> None:
        if not self._initialized:
            self._transcript_path.parent.mkdir(parents=True, exist_ok=True)
            self._initialized = True

    def write_meta(
        self,
        *,
        model: str = "",
        provider: str = "",
        first_user_message: str = "",
    ) -> None:
        """Write the session_meta line to the JSONL file and upsert into SQLite.

        Called when the session starts and again when the first user message
        is known (to set first_user_message / title).
        """
        if self._meta_written and not first_user_message:
            return

        git_info = _get_git_info(self.cwd)
        now = datetime.now(timezone.utc)

        # If already written, just update the fields that changed
        if self._meta_written and self._meta:
            self._meta["first_user_message"] = first_user_message[:500]
            self._meta["title"] = first_user_message[:200]
            self._meta["updated_at"] = now.isoformat()

            # Update SQLite only
            try:
                from crabcode_core.session.meta_db import SessionMetaStore
                store = SessionMetaStore()
                store.upsert({
                    "id": self.session_id,
                    "title": first_user_message[:200],
                    "cwd": self.cwd,
                    "model": self._meta.get("model", model),
                    "provider": self._meta.get("provider", provider),
                    "first_user_message": first_user_message[:500],
                    "tokens_used": self._meta.get("tokens_used", 0),
                    "git_branch": self._meta.get("git_branch", git_info["git_branch"]),
                    "git_sha": self._meta.get("git_sha", git_info["git_sha"]),
                    "created_at": self._meta.get("created_at", now.isoformat()),
                    "updated_at": now.isoformat(),
                    "message_count": self._meta.get("message_count", 0),
                })
                store.close()
            except Exception:
                pass
            return

        git_info = _get_git_info(self.cwd)
        now = datetime.now(timezone.utc)

        self._meta = {
            "id": self.session_id,
            "title": first_user_message[:200] if first_user_message else "",
            "cwd": self.cwd,
            "model": model,
            "provider": provider,
            "first_user_message": first_user_message[:500] if first_user_message else "",
            "tokens_used": 0,
            "git_branch": git_info["git_branch"],
            "git_sha": git_info["git_sha"],
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "message_count": 0,
            "is_archived": False,
        }

        # Write session_meta line to JSONL
        self._ensure_dir()
        meta_entry = {"type": "session_meta", **self._meta}
        with open(self._transcript_path, "a", encoding="utf-8") as f:
            f.write(_dump_jsonl_line(meta_entry))

        # Upsert into SQLite
        try:
            from crabcode_core.session.meta_db import SessionMetaStore
            store = SessionMetaStore()
            sqlite_meta = {
                "id": self.session_id,
                "title": self._meta["title"],
                "cwd": self.cwd,
                "model": model,
                "provider": provider,
                "first_user_message": self._meta["first_user_message"],
                "tokens_used": 0,
                "git_branch": git_info["git_branch"],
                "git_sha": git_info["git_sha"],
                "created_at": int(now.timestamp()),
                "updated_at": int(now.timestamp()),
                "message_count": 0,
            }
            store.upsert(sqlite_meta)
            store.close()
        except Exception:
            pass

        self._meta_written = True

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
        with open(self._transcript_path, "a", encoding="utf-8") as f:
            f.write(_dump_jsonl_line(entry))

    def load_messages(self) -> list[dict[str, Any]]:
        """Load all messages from the session transcript (deduped by uuid).

        Skips session_meta lines (they are metadata, not conversation messages).
        """
        if not self._transcript_path.exists():
            return []

        messages: list[dict[str, Any]] = []
        seen: set[str] = set()
        meta: dict[str, Any] = {}
        try:
            with open(self._transcript_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    # Capture the session_meta line but don't add it as a message
                    if entry.get("type") == "session_meta":
                        meta = {k: v for k, v in entry.items() if k != "type"}
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
        if meta:
            self._meta = meta
            self._meta_written = True
        return messages

    def record_tokens(self, tokens: int) -> None:
        """Accumulate token usage in SQLite."""
        try:
            from crabcode_core.session.meta_db import SessionMetaStore
            store = SessionMetaStore()
            store.update_tokens(self.session_id, tokens)
            store.close()
        except Exception:
            pass

    def record_message_count(self, count: int) -> None:
        """Update message count in SQLite."""
        try:
            from crabcode_core.session.meta_db import SessionMetaStore
            store = SessionMetaStore()
            store.update_message_count(self.session_id, count)
            store.close()
        except Exception:
            pass

    @property
    def meta(self) -> dict[str, Any]:
        """Return the session metadata (from JSONL or empty dict)."""
        return self._meta

    @staticmethod
    def list_sessions(cwd: str) -> list[dict[str, Any]]:
        """List all sessions for a given working directory.

        Tries SQLite first (fast); falls back to scanning JSONL files.
        """
        abs_cwd = os.path.abspath(cwd)

        # Try SQLite first
        try:
            from crabcode_core.session.meta_db import SessionMetaStore
            store = SessionMetaStore()
            rows = store.list_by_cwd(abs_cwd, limit=100)
            store.close()
            if rows:
                results = []
                for r in rows:
                    ts = r.get("updated_at", 0)
                    results.append({
                        "session_id": r["id"],
                        "title": r.get("title", ""),
                        "model": r.get("model", ""),
                        "provider": r.get("provider", ""),
                        "tokens_used": r.get("tokens_used", 0),
                        "git_branch": r.get("git_branch"),
                        "git_sha": r.get("git_sha"),
                        "message_count": r.get("message_count", 0),
                        "modified": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else "",
                        "preview": r.get("first_user_message", "")[:100],
                    })
                return results
        except Exception:
            pass

        # Fallback: scan JSONL files
        project_dir = get_project_dir(cwd)
        if not project_dir.exists():
            return []

        sessions: list[dict[str, Any]] = []
        for path in sorted(project_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
            session_id = path.stem
            try:
                stat = path.stat()
                first_user_msg = ""
                meta_info: dict[str, Any] = {}
                with open(path) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        if entry.get("type") == "session_meta":
                            meta_info = {k: v for k, v in entry.items() if k != "type"}
                            continue

                        # First non-meta user message is the preview
                        if entry.get("type") == "user" and not first_user_msg:
                            first_user_msg = _extract_preview(entry)
                        break  # Only read enough to get meta + first message

                sessions.append({
                    "session_id": session_id,
                    "title": meta_info.get("title", ""),
                    "model": meta_info.get("model", ""),
                    "provider": meta_info.get("provider", ""),
                    "tokens_used": meta_info.get("tokens_used", 0),
                    "git_branch": meta_info.get("git_branch"),
                    "git_sha": meta_info.get("git_sha"),
                    "message_count": meta_info.get("message_count", 0),
                    "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                    "preview": meta_info.get("first_user_message", "")[:100] or first_user_msg,
                })
            except Exception:
                sessions.append({
                    "session_id": session_id,
                    "title": "",
                    "model": "",
                    "provider": "",
                    "tokens_used": 0,
                    "git_branch": None,
                    "git_sha": None,
                    "message_count": 0,
                    "modified": "",
                    "preview": "",
                })

        return sessions

    @staticmethod
    def search_sessions(query: str, limit: int = 20) -> list[dict[str, Any]]:
        """Search sessions across all projects by title or first message."""
        try:
            from crabcode_core.session.meta_db import SessionMetaStore
            store = SessionMetaStore()
            rows = store.search(query, limit=limit)
            store.close()
            return rows
        except Exception:
            return []


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
