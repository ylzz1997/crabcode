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

from crabcode_core.logging_utils import get_logger
from crabcode_core.types.message import Message
from crabcode_core.utf8_sanitize import safe_utf8_json_tree

logger = get_logger(__name__)


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


def _message_to_entry(message: Message) -> dict[str, Any]:
    """Serialize all message state needed to reconstruct active context."""
    entry: dict[str, Any] = {
        "type": message.role.value,
        "uuid": message.uuid,
        "parent_uuid": message.parent_uuid,
        "timestamp": message.timestamp,
        "is_compact_summary": message.is_compact_summary,
        "content": message.content if isinstance(message.content, str) else [
            block.model_dump() for block in message.content
        ],
    }
    if message.origin is not None:
        entry["origin"] = message.origin
    for field in (
        "tool_use_result",
        "source_tool_assistant_uuid",
        "reply_to_uuid",
        "api_error",
        "usage",
        "request_id",
    ):
        value = getattr(message, field, None)
        if value is not None:
            entry[field] = value
    return entry


def _is_message_entry(entry: dict[str, Any]) -> bool:
    return entry.get("type") in {"user", "assistant", "system"}


def get_project_dir(cwd: str) -> Path:
    """Get the project-specific session directory."""
    return get_projects_dir() / _sanitize_path(os.path.abspath(cwd))


def get_transcript_path(cwd: str, session_id: str) -> Path:
    """Get the path for a session transcript file."""
    return get_project_dir(cwd) / f"{session_id}.jsonl"


def get_agent_meta_path(cwd: str, session_id: str) -> Path:
    """Get the path for a session's managed-agent metadata."""
    return get_project_dir(cwd) / f"{session_id}.agents.json"


def get_agent_transcript_dir(cwd: str, session_id: str) -> Path:
    """Get the directory for managed-agent transcripts for a session."""
    return get_project_dir(cwd) / f"{session_id}.agents"


def get_agent_transcript_path(cwd: str, session_id: str, agent_id: str) -> Path:
    """Get the transcript path for a managed agent."""
    return get_agent_transcript_dir(cwd, session_id) / f"{agent_id}.jsonl"


def get_task_output_path(cwd: str, session_id: str, task_id: str) -> Path:
    """Get the output path for a session-scoped background task."""
    return get_project_dir(cwd) / f"{session_id}.tasks" / f"{task_id}.log"


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
        logger.debug("Failed to read git info for %s", cwd, exc_info=True)
    return info


class SessionStorage:
    """Manages session persistence using JSONL files + SQLite metadata."""

    def __init__(self, cwd: str, session_id: str | None = None):
        self.cwd = os.path.abspath(cwd)
        self.session_id = session_id or generate_session_id()
        self._transcript_path = get_transcript_path(self.cwd, self.session_id)
        self._initialized = False
        self._written_uuids: set[str] = set()
        self._callback_deliveries: dict[tuple[str, int, str], dict[str, Any]] = {}
        self._callback_deliveries_loaded = False
        self._meta_written = False
        self._meta: dict[str, Any] = {}
        self.last_context_used_tokens: int = 0
        self.last_context_window_tokens: int = 0
        self.compact_count: int = 0

    def _ensure_dir(self) -> None:
        if not self._initialized:
            self._transcript_path.parent.mkdir(parents=True, exist_ok=True)
            self._initialized = True

    @staticmethod
    def _atomic_write_text(path: Path, content: str) -> None:
        temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, path)
        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass

    def write_agent_snapshots(self, snapshots: list[dict[str, Any]]) -> None:
        """Persist managed-agent metadata for this session."""
        self._ensure_dir()
        path = get_agent_meta_path(self.cwd, self.session_id)
        try:
            self._atomic_write_text(
                path,
                json.dumps(snapshots, ensure_ascii=False, indent=2) + "\n",
            )
        except Exception:
            logger.warning("Failed to write agent snapshots: %s", path, exc_info=True)

    def append_agent_messages(
        self,
        agent_id: str,
        messages: list[Message],
    ) -> None:
        """Persist a managed agent's transcript."""
        if not messages:
            return
        self._ensure_dir()
        path = get_agent_transcript_path(self.cwd, self.session_id, agent_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            _dump_jsonl_line(_message_to_entry(message))
            for message in messages
        ]
        self._atomic_write_text(path, "".join(lines))

    def load_agent_messages(self, agent_id: str) -> list[dict[str, Any]]:
        """Load a managed agent transcript."""
        path = get_agent_transcript_path(self.cwd, self.session_id, agent_id)
        if not path.exists():
            return []
        messages: list[dict[str, Any]] = []
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(entry, dict):
                        messages.append(entry)
        except Exception:
            logger.warning("Failed to load agent messages: %s", path, exc_info=True)
            return []
        return messages

    def load_agent_snapshots(self) -> list[dict[str, Any]]:
        """Load managed-agent metadata for this session."""
        path = get_agent_meta_path(self.cwd, self.session_id)
        if not path.exists():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("Failed to load agent snapshots: %s", path, exc_info=True)
            return []
        if isinstance(raw, list):
            return [item for item in raw if isinstance(item, dict)]
        return []

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
                logger.warning("Failed to update session metadata in SQLite", exc_info=True)
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
            logger.warning("Failed to persist session metadata to SQLite", exc_info=True)

        self._meta_written = True

    def append_message(self, message: Message) -> None:
        """Append a message to the session transcript (skips duplicates by uuid)."""
        if not self._written_uuids and self._transcript_path.exists():
            self.load_messages(full_history=True)
        if message.uuid in self._written_uuids:
            return

        self._ensure_dir()
        entry = _message_to_entry(message)
        with open(self._transcript_path, "a", encoding="utf-8") as f:
            f.write(_dump_jsonl_line(entry))
        self._written_uuids.add(message.uuid)

    def record_callback_delivery(
        self,
        *,
        agent_id: str,
        callback_epoch: int,
        callback_message_id: str,
        assistant_uuid: str,
    ) -> bool:
        """Persist an idempotent callback receipt outside the compacted context."""
        key = (agent_id, callback_epoch, callback_message_id)
        if not self._callback_deliveries_loaded:
            self.load_callback_deliveries()
        if key in self._callback_deliveries:
            return True

        entry = {
            "type": "callback_delivery",
            "session_id": self.session_id,
            "agent_id": agent_id,
            "callback_epoch": callback_epoch,
            "callback_message_id": callback_message_id,
            "assistant_uuid": assistant_uuid,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        try:
            self._ensure_dir()
            with open(self._transcript_path, "a", encoding="utf-8") as f:
                f.write(_dump_jsonl_line(entry))
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            logger.warning(
                "Failed to record callback delivery for agent %s epoch %d",
                agent_id,
                callback_epoch,
                exc_info=True,
            )
            return False
        self._callback_deliveries[key] = entry
        return True

    def has_callback_delivery(
        self,
        *,
        agent_id: str,
        callback_epoch: int,
        callback_message_id: str,
    ) -> bool:
        if not self._callback_deliveries_loaded:
            self.load_callback_deliveries()
        return (
            agent_id,
            callback_epoch,
            callback_message_id,
        ) in self._callback_deliveries

    def load_callback_deliveries(self) -> list[dict[str, Any]]:
        """Load durable callback receipts without affecting active message projection."""
        deliveries: dict[tuple[str, int, str], dict[str, Any]] = {}
        if self._transcript_path.exists():
            try:
                with open(self._transcript_path, encoding="utf-8") as f:
                    for line in f:
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(entry, dict) or entry.get("type") != "callback_delivery":
                            continue
                        agent_id = str(entry.get("agent_id") or "")
                        message_id = str(entry.get("callback_message_id") or "")
                        try:
                            epoch = int(entry.get("callback_epoch", 0))
                        except (TypeError, ValueError):
                            continue
                        if agent_id and message_id:
                            deliveries.setdefault((agent_id, epoch, message_id), entry)
            except Exception:
                logger.warning(
                    "Failed to load callback deliveries: %s",
                    self._transcript_path,
                    exc_info=True,
                )
        self._callback_deliveries = deliveries
        self._callback_deliveries_loaded = True
        return list(deliveries.values())

    def append_compaction(
        self,
        messages: list[Message],
        *,
        trigger: str,
        messages_before: int,
        estimated_tokens_before: int = 0,
        estimated_tokens_after: int = 0,
    ) -> str | None:
        """Durably commit an active-context checkpoint in one JSONL record."""
        if not messages or not messages[0].is_compact_summary:
            return None
        checkpoint_id = str(uuid.uuid4())
        snapshot = [_message_to_entry(message) for message in messages]
        entry = {
            "type": "compact_boundary",
            "checkpoint_id": checkpoint_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "trigger": trigger,
            "messages_before": messages_before,
            "messages_after": len(messages),
            "estimated_tokens_before": max(0, estimated_tokens_before),
            "estimated_tokens_after": max(0, estimated_tokens_after),
            "tail_start_uuid": messages[1].uuid if len(messages) > 1 else None,
            "summary_uuid": messages[0].uuid,
            "messages": snapshot,
        }
        self._ensure_dir()
        with open(self._transcript_path, "a", encoding="utf-8") as f:
            f.write(_dump_jsonl_line(entry))
            f.flush()
            os.fsync(f.fileno())
        for message in messages:
            self._written_uuids.add(message.uuid)
        self.compact_count += 1
        return checkpoint_id

    def load_messages(self, *, full_history: bool = False) -> list[dict[str, Any]]:
        """Load the active context, or the durable audit history when requested.

        A completed ``compact_boundary`` atomically replaces the active projection
        with its embedded checkpoint snapshot. Earlier ordinary messages remain in
        the transcript for export and audit, but are not replayed to the model.
        """
        if not self._transcript_path.exists():
            return []

        self.compact_count = 0
        all_messages: list[dict[str, Any]] = []
        active_messages: list[dict[str, Any]] = []
        seen: set[str] = set()
        active_seen: set[str] = set()
        boundary_seen = False
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

                    if not isinstance(entry, dict):
                        continue

                    # Capture the session_meta line but don't add it as a message.
                    if entry.get("type") == "session_meta":
                        meta = {k: v for k, v in entry.items() if k != "type"}
                        continue

                    # Capture context_usage lines (written at turn_complete) but don't add as message
                    if entry.get("type") == "context_usage":
                        self.last_context_used_tokens = int(entry.get("used_tokens", 0))
                        self.last_context_window_tokens = int(entry.get("window_tokens", 0))
                        continue

                    if entry.get("type") == "compact_boundary":
                        snapshot = entry.get("messages")
                        if not isinstance(snapshot, list):
                            continue
                        restored = [
                            item for item in snapshot
                            if isinstance(item, dict) and _is_message_entry(item)
                        ]
                        if not restored or not restored[0].get("is_compact_summary"):
                            continue
                        active_messages = []
                        active_seen = set()
                        for item in restored:
                            item_uuid = str(item.get("uuid") or "")
                            was_seen = bool(item_uuid and item_uuid in seen)
                            if item_uuid and item_uuid in active_seen:
                                continue
                            if item_uuid:
                                active_seen.add(item_uuid)
                                seen.add(item_uuid)
                            active_messages.append(item)
                            # A boundary can contain messages produced earlier in
                            # the current agentic turn. They may not have ordinary
                            # JSONL records yet, so retain their first appearance in
                            # the audit/export view as well as in active context.
                            if full_history and (not item_uuid or not was_seen):
                                all_messages.append(item)
                        boundary_seen = True
                        self.compact_count += 1
                        continue

                    # Checkpoint, rollback, and future metadata records must never be
                    # reconstructed as empty user messages.
                    if not _is_message_entry(entry):
                        continue

                    msg_uuid = entry.get("uuid", "")
                    if msg_uuid and msg_uuid in seen:
                        # A normal record written after a boundary can duplicate a
                        # message embedded in that boundary; keep only one active copy.
                        if boundary_seen and msg_uuid not in active_seen:
                            active_seen.add(msg_uuid)
                            active_messages.append(entry)
                        continue
                    if msg_uuid:
                        seen.add(msg_uuid)
                    all_messages.append(entry)
                    if boundary_seen:
                        if not msg_uuid or msg_uuid not in active_seen:
                            if msg_uuid:
                                active_seen.add(msg_uuid)
                            active_messages.append(entry)
        except Exception:
            logger.warning("Failed to load transcript: %s", self._transcript_path, exc_info=True)

        self._written_uuids = seen
        if meta:
            self._meta = meta
            self._meta_written = True
        if full_history:
            return all_messages
        return active_messages if boundary_seen else all_messages

    def update_title(self, title: str) -> None:
        """Update the session title in SQLite and in-memory meta."""
        self._meta["title"] = title
        try:
            from crabcode_core.session.meta_db import SessionMetaStore
            store = SessionMetaStore()
            store.update_title(self.session_id, title)
            store.close()
        except Exception:
            logger.debug("Failed to update session title for %s", self.session_id, exc_info=True)

    def update_summary(self, summary: str) -> None:
        """Update the session summary in SQLite and in-memory meta."""
        self._meta["summary"] = summary
        try:
            from crabcode_core.session.meta_db import SessionMetaStore
            store = SessionMetaStore()
            store.update_summary(self.session_id, summary)
            store.close()
        except Exception:
            logger.debug("Failed to update session summary for %s", self.session_id, exc_info=True)

    def record_tokens(self, tokens: int) -> None:
        """Accumulate token usage in SQLite."""
        try:
            from crabcode_core.session.meta_db import SessionMetaStore
            store = SessionMetaStore()
            store.update_tokens(self.session_id, tokens)
            store.close()
        except Exception:
            logger.debug("Failed to record token usage for session %s", self.session_id, exc_info=True)

    def record_context_usage(self, used_tokens: int, window_tokens: int) -> None:
        """Persist context window usage to JSONL so it survives session restore."""
        if not used_tokens and not window_tokens:
            return
        self.last_context_used_tokens = used_tokens
        self.last_context_window_tokens = window_tokens
        self._ensure_dir()
        entry = {"type": "context_usage", "used_tokens": used_tokens, "window_tokens": window_tokens}
        with open(self._transcript_path, "a", encoding="utf-8") as f:
            f.write(_dump_jsonl_line(entry))

    def record_message_count(self, count: int) -> None:
        """Update message count in SQLite."""
        try:
            from crabcode_core.session.meta_db import SessionMetaStore
            store = SessionMetaStore()
            store.update_message_count(self.session_id, count)
            store.close()
        except Exception:
            logger.debug("Failed to record message count for session %s", self.session_id, exc_info=True)

    def create_checkpoint(self, messages: list, label: str = "", snapshot_id: str | None = None) -> str | None:
        """Create a checkpoint at the current message position.

        If *snapshot_id* is provided, it is stored alongside the checkpoint
        so that ``/revert`` can also restore file-system state.
        """
        if not messages:
            return None
        last_msg = messages[-1]
        msg_uuid = getattr(last_msg, "uuid", "") or ""
        msg_index = len(messages) - 1
        try:
            from crabcode_core.session.meta_db import SessionMetaStore
            store = SessionMetaStore()
            cp_id = store.create_checkpoint(
                session_id=self.session_id,
                message_uuid=msg_uuid,
                message_index=msg_index,
                label=label,
                snapshot_id=snapshot_id,
            )
            store.close()
            # Write a marker line to JSONL for auditability
            self._ensure_dir()
            entry = {
                "type": "checkpoint",
                "checkpoint_id": cp_id,
                "message_uuid": msg_uuid,
                "message_index": msg_index,
                "label": label,
                "snapshot_id": snapshot_id,
            }
            with open(self._transcript_path, "a", encoding="utf-8") as f:
                f.write(_dump_jsonl_line(entry))
            return cp_id
        except Exception:
            logger.warning("Failed to create checkpoint for session %s", self.session_id, exc_info=True)
            return None

    def list_checkpoints(self) -> list[dict[str, Any]]:
        """List checkpoints for this session."""
        try:
            from crabcode_core.session.meta_db import SessionMetaStore
            store = SessionMetaStore()
            cps = store.list_checkpoints(self.session_id)
            store.close()
            return cps
        except Exception:
            logger.debug("Failed to list checkpoints for session %s", self.session_id, exc_info=True)
            return []

    def rollback_to_checkpoint(self, checkpoint_id: str) -> int | None:
        """Get the message index to rollback to. Returns index or None if not found."""
        try:
            from crabcode_core.session.meta_db import SessionMetaStore
            store = SessionMetaStore()
            cp = store.get_checkpoint(checkpoint_id)
            store.close()
            if cp and cp["session_id"] == self.session_id:
                # Write rollback marker to JSONL
                self._ensure_dir()
                entry = {
                    "type": "rollback",
                    "checkpoint_id": checkpoint_id,
                    "rollback_to_index": cp["message_index"],
                }
                with open(self._transcript_path, "a", encoding="utf-8") as f:
                    f.write(_dump_jsonl_line(entry))
                return cp["message_index"]
        except Exception:
            logger.warning("Failed to rollback checkpoint %s", checkpoint_id, exc_info=True)
        return None

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
                        "summary": r.get("summary", ""),
                        "preview": r.get("summary", "")[:100] or r.get("first_user_message", "")[:100],
                    })
                return results
        except Exception:
            logger.debug("SQLite session listing failed for cwd %s", abs_cwd, exc_info=True)

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
                logger.warning("Failed to inspect session transcript: %s", path, exc_info=True)
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

    @classmethod
    def from_session_id(cls, session_id: str) -> "SessionStorage | None":
        """Resolve a session by ID using SQLite metadata (cross-project).

        Returns a SessionStorage pointed at the original cwd, or None if not found.
        """
        try:
            from crabcode_core.session.meta_db import SessionMetaStore
            store = SessionMetaStore()
            row = store.get(session_id)
            store.close()
            if row and row.get("cwd"):
                return cls(cwd=row["cwd"], session_id=session_id)
        except Exception:
            logger.debug("from_session_id lookup failed for %s", session_id, exc_info=True)
        return None

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
            logger.warning("Session search failed for query %r", query, exc_info=True)
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
