"""Session storage — JSONL-based conversation persistence with SQLite metadata."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from crabcode_core.file_lock import file_lock
from crabcode_core.filesystem import replace_with_retry
from crabcode_core.logging_utils import get_logger
from crabcode_core.path_validation import validate_path_component
from crabcode_core.subprocess_utils import subprocess_group_options
from crabcode_core.types.message import Message
from crabcode_core.utf8_sanitize import safe_utf8_json_tree

logger = get_logger(__name__)


class SessionArchivedError(RuntimeError):
    """Raised when a writer targets a durably archived session."""


@contextmanager
def _transcript_file_lock(path: Path, *, exclusive: bool):
    """Serialize transcript access across threads and cooperating processes."""
    lock_path = path.with_name(f".{path.name}.lock")
    with file_lock(lock_path, exclusive=exclusive):
        yield


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


def _windows_extended_path(path: Path) -> Path:
    """Return an extended-length path for legacy Windows session locations."""
    if os.name != "nt":
        return path
    raw = os.path.abspath(str(path))
    if raw.startswith("\\\\?\\"):
        return Path(raw)
    if raw.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + raw[2:])
    return Path("\\\\?\\" + raw)


def _project_path_hash(cwd: str) -> str:
    """Return a stable collision-resistant key for an absolute project path."""
    identity = os.path.normcase(os.path.abspath(cwd))
    return hashlib.sha256(os.fsencode(identity)).hexdigest()[:20]


def _legacy_project_dir(cwd: str) -> Path:
    """Return the pre-hash project directory used by older CrabCode builds."""
    path = get_projects_dir() / _sanitize_path(os.path.abspath(cwd))
    return _windows_extended_path(path)


def _hashed_project_dir(cwd: str, prefix_length: int) -> Path:
    identity = os.path.normcase(os.path.abspath(cwd))
    prefix = _sanitize_path(identity)[:prefix_length]
    return get_projects_dir() / f"{prefix}--{_project_path_hash(identity)}"


def _case_sensitive_hashed_project_dir(cwd: str, prefix_length: int) -> Path:
    """Return the pre-Windows-normalization hashed directory."""
    absolute = os.path.abspath(cwd)
    prefix = _sanitize_path(absolute)[:prefix_length]
    digest = hashlib.sha256(os.fsencode(absolute)).hexdigest()[:20]
    return get_projects_dir() / f"{prefix}--{digest}"


def _validate_component(value: str, label: str) -> str:
    """Validate an identifier before interpolating it into a file name.

    Session and agent IDs normally come from UUIDs, but resume/import APIs can
    receive values from an untrusted client.  Sanitizing here would make two
    distinct IDs collide, so reject path separators and other filesystem
    control components at the storage boundary instead.
    """
    return validate_path_component(value, label)


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
    """Get the collision-resistant project-specific session directory."""
    return _hashed_project_dir(cwd, 64)


def _previous_hashed_project_dir(cwd: str) -> Path:
    """Return the longer hashed directory used by CrabCode 0.1.4 and earlier."""
    return _windows_extended_path(_case_sensitive_hashed_project_dir(cwd, 175))


def _case_sensitive_project_dir(cwd: str) -> Path:
    """Return the previous 64-character, case-sensitive project directory."""
    return _windows_extended_path(_case_sensitive_hashed_project_dir(cwd, 64))


def _project_dirs_for_read(cwd: str) -> list[Path]:
    """Return the current project directory plus its legacy compatibility path."""
    directories = [
        get_project_dir(cwd),
        _case_sensitive_project_dir(cwd),
        _previous_hashed_project_dir(cwd),
        _legacy_project_dir(cwd),
    ]
    return list(dict.fromkeys(directories))


def _transcript_declared_cwd(path: Path) -> str | None:
    """Read a transcript's durable cwd without trusting its directory name."""
    declared: str | None = None
    try:
        with _transcript_file_lock(path, exclusive=False):
            with open(path, encoding="utf-8") as transcript:
                for raw_line in transcript:
                    try:
                        entry = json.loads(raw_line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(entry, dict) or entry.get("type") != "session_meta":
                        continue
                    value = entry.get("cwd")
                    if isinstance(value, str) and value:
                        declared = os.path.abspath(value)
    except (OSError, ValueError):
        return None
    return declared


def _session_project_dir(cwd: str, session_id: str) -> Path:
    """Resolve an existing legacy transcript, while placing new sessions safely."""
    validated_id = _validate_component(session_id, "session id")
    directories = _project_dirs_for_read(cwd)
    current = directories[0]
    legacy = _legacy_project_dir(cwd)
    for directory in directories:
        transcript = directory / f"{validated_id}.jsonl"
        if not transcript.exists():
            continue
        if directory != legacy:
            return directory
        declared_cwd = _transcript_declared_cwd(transcript)
        if declared_cwd is None or os.path.normcase(declared_cwd) == os.path.normcase(
            os.path.abspath(cwd)
        ):
            return directory
    return current


def get_session_tombstone_path(_cwd: str, session_id: str) -> Path:
    """Return the durable archive fence, stored outside purgeable artifacts."""
    validated_id = _validate_component(session_id, "session id")
    shard = hashlib.sha256(validated_id.encode("utf-8")).hexdigest()[:2]
    return (
        get_config_home()
        / "session-tombstones"
        / shard
        / f"{validated_id}.json"
    )


def _session_lifecycle_lock_path(_cwd: str, session_id: str) -> Path:
    validated_id = _validate_component(session_id, "session id")
    shard = hashlib.sha256(validated_id.encode("utf-8")).hexdigest()[:2]
    return (
        get_config_home()
        / "session-locks"
        / shard
        / validated_id
    )


@contextmanager
def _session_lifecycle_lock(cwd: str, session_id: str, *, exclusive: bool = True):
    """Serialize archive, purge, and writes independently of directory migration."""
    with _transcript_file_lock(
        _session_lifecycle_lock_path(cwd, session_id),
        exclusive=exclusive,
    ):
        yield


def get_transcript_path(cwd: str, session_id: str) -> Path:
    """Get the path for a session transcript file."""
    validated_id = _validate_component(session_id, "session id")
    return _session_project_dir(cwd, validated_id) / f"{validated_id}.jsonl"


def get_agent_meta_path(cwd: str, session_id: str) -> Path:
    """Get the path for a session's managed-agent metadata."""
    validated_id = _validate_component(session_id, "session id")
    return _session_project_dir(cwd, validated_id) / f"{validated_id}.agents.json"


def get_agent_transcript_dir(cwd: str, session_id: str) -> Path:
    """Get the directory for managed-agent transcripts for a session."""
    validated_id = _validate_component(session_id, "session id")
    return _session_project_dir(cwd, validated_id) / f"{validated_id}.agents"


def get_agent_transcript_path(cwd: str, session_id: str, agent_id: str) -> Path:
    """Get the transcript path for a managed agent."""
    return get_agent_transcript_dir(cwd, session_id) / (
        f"{_validate_component(agent_id, 'agent id')}.jsonl"
    )


def get_task_output_path(cwd: str, session_id: str, task_id: str) -> Path:
    """Get the output path for a session-scoped background task."""
    return (
        _session_project_dir(cwd, session_id)
        / f"{_validate_component(session_id, 'session id')}.tasks"
        / f"{_validate_component(task_id, 'task id')}.log"
    )


def generate_session_id() -> str:
    return str(uuid.uuid4())


def _get_git_info(cwd: str) -> dict[str, str | None]:
    """Get git branch and SHA for the cwd."""
    info: dict[str, str | None] = {"git_branch": None, "git_sha": None}
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=cwd, timeout=5,
            **subprocess_group_options(),
        )
        if result.stdout.strip() != "true":
            return info
        branch = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=cwd, timeout=5,
            **subprocess_group_options(),
        ).stdout.strip()
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=cwd, timeout=5,
            **subprocess_group_options(),
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
        self._archive_state_checked = False

    def _ensure_dir(self) -> None:
        if not self._initialized:
            self._transcript_path.parent.mkdir(parents=True, exist_ok=True)
            self._initialized = True

    def _transcript_has_archive_marker_locked(self) -> bool:
        """Return the latest inline archive lifecycle state."""
        if not self._transcript_path.exists():
            return False
        archived = False
        try:
            with _transcript_file_lock(self._transcript_path, exclusive=False):
                with open(self._transcript_path, encoding="utf-8") as transcript:
                    for raw_line in transcript:
                        try:
                            entry = json.loads(raw_line)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(entry, dict):
                            continue
                        if entry.get("type") == "session_archive":
                            archived = True
                        elif entry.get("type") == "session_restore":
                            archived = False
        except FileNotFoundError:
            return False
        return archived

    def _write_tombstone_locked(self) -> None:
        """Commit the external archive marker while the session lock is held."""
        marker = get_session_tombstone_path(self.cwd, self.session_id)
        if marker.exists():
            return
        marker.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write_text(
            marker,
            json.dumps(
                {
                    "session_id": self.session_id,
                    "cwd": self.cwd,
                    "archived_at": datetime.now(timezone.utc).isoformat(),
                },
                ensure_ascii=False,
            )
            + "\n",
        )

    def _assert_writable_locked(self) -> None:
        """Reject writes after archive, including legacy inline-only archives."""
        if get_session_tombstone_path(self.cwd, self.session_id).exists():
            raise SessionArchivedError(f"Session {self.session_id} is archived")
        if not self._archive_state_checked:
            self._archive_state_checked = True
            if self._transcript_has_archive_marker_locked():
                self._write_tombstone_locked()
                raise SessionArchivedError(f"Session {self.session_id} is archived")

    def _append_transcript_line_locked(self, entry: Any) -> None:
        """Append one JSONL record while the session lifecycle lock is held."""
        self._ensure_dir()
        line = _dump_jsonl_line(entry)
        with _transcript_file_lock(self._transcript_path, exclusive=True):
            with open(self._transcript_path, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())

    def _append_transcript_line(self, entry: Any) -> None:
        """Append one durable JSONL record under the cross-process locks."""
        with _session_lifecycle_lock(self.cwd, self.session_id):
            self._assert_writable_locked()
            self._append_transcript_line_locked(entry)

    def _append_transcript_line_if_absent_locked(
        self,
        entry: Any,
        matcher: Callable[[dict[str, Any]], bool],
    ) -> bool:
        """Check and append one record while the session lock is held."""
        self._ensure_dir()
        line = _dump_jsonl_line(entry)
        with _transcript_file_lock(self._transcript_path, exclusive=True):
            try:
                with open(self._transcript_path, encoding="utf-8") as transcript:
                    for raw_line in transcript:
                        try:
                            existing = json.loads(raw_line)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(existing, dict) and matcher(existing):
                            return False
            except FileNotFoundError:
                pass

            with open(self._transcript_path, "a", encoding="utf-8") as transcript:
                transcript.write(line)
                transcript.flush()
                os.fsync(transcript.fileno())
        return True

    def _append_transcript_line_if_absent(
        self,
        entry: Any,
        matcher: Callable[[dict[str, Any]], bool],
    ) -> bool:
        """Append a record only when no matching record exists.

        The check and append must share the same exclusive lock.  Checking
        ``_written_uuids`` alone is insufficient because another
        ``SessionStorage`` instance (or process) can append between that
        in-memory check and our write.
        """
        with _session_lifecycle_lock(self.cwd, self.session_id):
            self._assert_writable_locked()
            return self._append_transcript_line_if_absent_locked(entry, matcher)

    def _read_transcript_text(self) -> str:
        """Read the transcript without racing an append or marker write."""
        try:
            with _session_lifecycle_lock(self.cwd, self.session_id, exclusive=False):
                if not self._transcript_path.exists():
                    return ""
                with _transcript_file_lock(self._transcript_path, exclusive=False):
                    return self._transcript_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            # A concurrent archive can remove the file between the existence
            # check and lock acquisition.
            return ""
        except OSError:
            logger.warning("Failed to read transcript: %s", self._transcript_path, exc_info=True)
            return ""

    @staticmethod
    def _atomic_write_text(path: Path, content: str) -> None:
        temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            replace_with_retry(temp_path, path)
        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass

    def write_agent_snapshots(self, snapshots: list[dict[str, Any]]) -> None:
        """Persist managed-agent metadata for this session."""
        path = get_agent_meta_path(self.cwd, self.session_id)
        try:
            # Agent state can be persisted by several detached run tasks and
            # by a concurrent gateway resume.  Serialize the replace with the
            # same sidecar lock used for the main transcript so readers never
            # observe a half-written metadata file and writers cannot race on
            # the temporary path.
            with _session_lifecycle_lock(self.cwd, self.session_id):
                self._assert_writable_locked()
                self._ensure_dir()
                with _transcript_file_lock(path, exclusive=True):
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
        path = get_agent_transcript_path(self.cwd, self.session_id, agent_id)
        lines = [
            _dump_jsonl_line(_message_to_entry(message))
            for message in messages
        ]
        # A cancellation/finalization callback may race a normal transcript
        # checkpoint for the same agent.  Keep the full-file replace under a
        # path lock, matching the main session transcript's cross-process
        # discipline.
        with _session_lifecycle_lock(self.cwd, self.session_id):
            self._assert_writable_locked()
            self._ensure_dir()
            path.parent.mkdir(parents=True, exist_ok=True)
            with _transcript_file_lock(path, exclusive=True):
                self._atomic_write_text(path, "".join(lines))

    def load_agent_messages(self, agent_id: str) -> list[dict[str, Any]]:
        """Load a managed agent transcript."""
        path = get_agent_transcript_path(self.cwd, self.session_id, agent_id)
        if not path.exists():
            return []
        messages: list[dict[str, Any]] = []
        try:
            with _transcript_file_lock(path, exclusive=False):
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
            with _transcript_file_lock(path, exclusive=False):
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
        git_info = _get_git_info(self.cwd)
        now = datetime.now(timezone.utc)
        with _session_lifecycle_lock(self.cwd, self.session_id):
            self._assert_writable_locked()
            current = self._read_latest_meta_locked()
            if current:
                self._meta = current
                self._meta_written = True
                if not first_user_message:
                    return
                fields = {
                    "first_user_message": first_user_message[:500],
                    "title": first_user_message[:200],
                    "updated_at": now.isoformat(),
                }
                self._meta.update(fields)
                self._append_transcript_line_locked({"type": "session_meta", **fields})
            else:
                self._meta = self._new_meta(
                    model=model,
                    provider=provider,
                    first_user_message=first_user_message,
                    git_info=git_info,
                    now=now,
                )
                self._append_transcript_line_locked({"type": "session_meta", **self._meta})
                self._meta_written = True
            self._upsert_meta_locked()

    def _new_meta(
        self,
        *,
        model: str = "",
        provider: str = "",
        first_user_message: str = "",
        git_info: dict[str, str | None] | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        git_info = git_info or _get_git_info(self.cwd)
        return {
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

    def _read_latest_meta_locked(self) -> dict[str, Any]:
        """Read merged metadata while the lifecycle lock prevents concurrent writes."""
        if not self._transcript_path.exists():
            return {}
        meta: dict[str, Any] = {}
        with _transcript_file_lock(self._transcript_path, exclusive=False):
            try:
                with open(self._transcript_path, encoding="utf-8") as transcript:
                    for raw_line in transcript:
                        try:
                            entry = json.loads(raw_line)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(entry, dict):
                            continue
                        if entry.get("type") == "session_meta":
                            meta.update({key: value for key, value in entry.items() if key != "type"})
                        elif entry.get("type") == "session_archive":
                            meta["is_archived"] = True
                        elif entry.get("type") == "session_restore":
                            meta["is_archived"] = False
            except FileNotFoundError:
                return {}
        return meta

    def _upsert_meta_locked(self) -> None:
        """Mirror the current JSONL metadata into SQLite before releasing the lock."""
        store = None
        try:
            from crabcode_core.session.meta_db import SessionMetaStore

            store = SessionMetaStore()
            store.upsert(dict(self._meta))
        except Exception:
            logger.warning("Failed to persist session metadata to SQLite", exc_info=True)
        finally:
            if store is not None:
                try:
                    store.close()
                except Exception:
                    logger.debug("Failed to close session metadata store", exc_info=True)

    def _commit_metadata_update(
        self,
        update: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> None:
        """Atomically merge one metadata change into JSONL and its SQLite index."""
        with _session_lifecycle_lock(self.cwd, self.session_id):
            self._assert_writable_locked()
            current = self._read_latest_meta_locked()
            initialized = not current
            if initialized:
                current = self._new_meta()
            fields = update(current)
            current.update(fields)
            self._meta = current
            self._meta_written = True
            entry = self._meta if initialized else fields
            self._append_transcript_line_locked({"type": "session_meta", **entry})
            self._upsert_meta_locked()

    def append_message(self, message: Message) -> None:
        """Append a message to the session transcript (skips duplicates by uuid)."""
        if not self._written_uuids and self._transcript_path.exists():
            self.load_messages(full_history=True)
        if message.uuid in self._written_uuids:
            return

        entry = _message_to_entry(message)
        message_uuid = message.uuid

        def matches(existing: dict[str, Any]) -> bool:
            if existing.get("uuid") == message_uuid:
                return True
            # A projection boundary embeds its active messages and can be the
            # only durable occurrence of a UUID in a newly-created storage
            # instance.
            if existing.get("type") not in {"compact_boundary", "projection_boundary"}:
                return False
            snapshot = existing.get("messages")
            return isinstance(snapshot, list) and any(
                isinstance(item, dict) and item.get("uuid") == message_uuid
                for item in snapshot
            )

        if not self._append_transcript_line_if_absent(entry, matches):
            self._written_uuids.add(message_uuid)
            return
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
            self._append_transcript_line_if_absent(
                entry,
                lambda existing: (
                    existing.get("type") == "callback_delivery"
                    and existing.get("agent_id") == agent_id
                    and existing.get("callback_epoch") == callback_epoch
                    and existing.get("callback_message_id") == callback_message_id
                ),
            )
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
        transcript = self._read_transcript_text()
        if transcript:
            try:
                for line in transcript.splitlines():
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
        self._append_transcript_line(entry)
        for message in messages:
            self._written_uuids.add(message.uuid)
        self.compact_count += 1
        return checkpoint_id

    def append_projection(
        self,
        messages: list[Message],
        *,
        trigger: str,
        messages_before: int,
    ) -> str | None:
        """Durably replace active context without claiming a model summary.

        This is used when oversized historical tool output can be pruned enough
        to avoid a summarization request. The rewritten messages retain their
        UUIDs, so ordinary append de-duplication cannot persist the new content.
        """
        if not messages:
            return None
        projection_id = str(uuid.uuid4())
        self._append_transcript_line(
            {
                "type": "projection_boundary",
                "projection_id": projection_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "trigger": trigger,
                "messages_before": messages_before,
                "messages_after": len(messages),
                "messages": [_message_to_entry(message) for message in messages],
            }
        )
        for message in messages:
            self._written_uuids.add(message.uuid)
        return projection_id

    def append_clear_boundary(self, *, messages_before: int) -> str:
        """Durably replace the active conversation projection with an empty one.

        Ordinary transcript messages remain available to audit/export readers,
        while future session resumes start from an empty active context.
        """
        clear_id = str(uuid.uuid4())
        self._append_transcript_line(
            {
                "type": "clear_boundary",
                "clear_id": clear_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "messages_before": max(0, int(messages_before)),
            }
        )
        self._written_uuids.clear()
        return clear_id

    def persist_archive_marker(self) -> None:
        """Persist terminal external and inline archive tombstones."""
        entry = {
            "type": "session_archive",
            "session_id": self.session_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        with _session_lifecycle_lock(self.cwd, self.session_id):
            self._write_tombstone_locked()
            if self._transcript_path.exists():
                self._append_transcript_line_if_absent_locked(
                    entry,
                    lambda existing: existing.get("type") == "session_archive",
                )
            self._archive_state_checked = True
            self._meta["is_archived"] = True

    def restore_from_archive(self) -> None:
        """Remove archive fences so an explicitly recovered session is writable."""
        with _session_lifecycle_lock(self.cwd, self.session_id):
            marker = get_session_tombstone_path(self.cwd, self.session_id)
            marker.unlink(missing_ok=True)
            if self._transcript_path.exists():
                # Keep the audit record, but make the durable session active
                # again for a user-requested recovery.
                self._append_transcript_line_if_absent_locked(
                    {
                        "type": "session_restore",
                        "session_id": self.session_id,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    },
                    lambda existing: existing.get("type") == "session_restore",
                )
            try:
                from crabcode_core.session.meta_db import SessionMetaStore
                store = SessionMetaStore()
                try:
                    store.restore(self.session_id)
                finally:
                    store.close()
            except Exception:
                logger.warning("Failed to clear restored session metadata", exc_info=True)
            self._archive_state_checked = False
            self._meta["is_archived"] = False

    def load_messages(
        self,
        *,
        full_history: bool = False,
        _transcript_text: str | None = None,
    ) -> list[dict[str, Any]]:
        """Load the active context, or the durable audit history when requested.

        A completed ``compact_boundary`` atomically replaces the active projection
        with its embedded checkpoint snapshot. Earlier ordinary messages remain in
        the transcript for export and audit, but are not replayed to the model.
        """
        # These counters describe this transcript read. Reset them even when
        # the file is missing or has been truncated so a reused storage object
        # cannot expose values from an earlier load.
        self.last_context_used_tokens = 0
        self.last_context_window_tokens = 0
        self.compact_count = 0
        self._written_uuids = set()
        self._meta = {}
        self._meta_written = False
        transcript = (
            self._read_transcript_text()
            if _transcript_text is None
            else _transcript_text
        )
        if not transcript:
            if get_session_tombstone_path(self.cwd, self.session_id).exists():
                self._meta = {
                    "id": self.session_id,
                    "cwd": self.cwd,
                    "is_archived": True,
                }
                self._meta_written = True
            return []

        all_messages: list[dict[str, Any]] = []
        active_messages: list[dict[str, Any]] = []
        seen: set[str] = set()
        active_seen: set[str] = set()
        boundary_seen = False
        rollback_seen = False
        archived = False
        meta: dict[str, Any] = {}
        checkpoints: dict[str, dict[str, Any]] = {}
        checkpoint_projections: dict[str, list[dict[str, Any]]] = {}
        try:
            for line in transcript.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if not isinstance(entry, dict):
                    continue

                if entry.get("type") == "session_archive":
                    archived = True
                    continue
                if entry.get("type") == "session_restore":
                    archived = False
                    continue

                # Capture the session_meta line but don't add it as a message.
                if entry.get("type") == "session_meta":
                    # Metadata updates may intentionally contain only the
                    # fields that changed (for example model/provider).
                    # Merge them over the previous record so counters and
                    # titles are not lost when rebuilding from JSONL.
                    meta.update({k: v for k, v in entry.items() if k != "type"})
                    continue

                # Capture context_usage lines (written at turn_complete) but don't add as message
                if entry.get("type") == "context_usage":
                    # A truncated or hand-edited transcript must not prevent
                    # later messages from loading.  Keep each counter's last
                    # valid value when its counterpart is malformed.
                    try:
                        self.last_context_used_tokens = int(entry.get("used_tokens", 0))
                    except (TypeError, ValueError, OverflowError):
                        pass
                    try:
                        self.last_context_window_tokens = int(entry.get("window_tokens", 0))
                    except (TypeError, ValueError, OverflowError):
                        pass
                    continue

                if entry.get("type") in {"compact_boundary", "projection_boundary"}:
                    is_compaction = entry.get("type") == "compact_boundary"
                    snapshot = entry.get("messages")
                    if not isinstance(snapshot, list):
                        continue
                    restored = [
                        item for item in snapshot
                        if isinstance(item, dict) and _is_message_entry(item)
                    ]
                    if not restored or (
                        is_compaction and not restored[0].get("is_compact_summary")
                    ):
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
                    if is_compaction:
                        self.compact_count += 1
                    continue

                if entry.get("type") == "clear_boundary":
                    active_messages = []
                    active_seen = set()
                    boundary_seen = True
                    self.last_context_used_tokens = 0
                    self.last_context_window_tokens = 0
                    self.compact_count = 0
                    continue

                if entry.get("type") == "checkpoint":
                    checkpoint_id = str(entry.get("checkpoint_id") or "")
                    if checkpoint_id:
                        checkpoints[checkpoint_id] = entry
                        snapshot = entry.get("messages")
                        if isinstance(snapshot, list):
                            restored = [
                                item
                                for item in snapshot
                                if isinstance(item, dict) and _is_message_entry(item)
                            ]
                            target_uuid = str(entry.get("message_uuid") or "")
                            if restored and (
                                not target_uuid
                                or any(item.get("uuid") == target_uuid for item in restored)
                            ):
                                checkpoint_projections[checkpoint_id] = restored
                                continue
                        if checkpoint_id not in checkpoint_projections:
                            # Older markers do not embed their projection. Keep
                            # the active state observed at marker time as the
                            # compatibility and malformed-snapshot fallback.
                            checkpoint_projections[checkpoint_id] = list(active_messages)
                    continue

                if entry.get("type") == "rollback":
                    # Rollback markers are durable state transitions, not
                    # messages.  Apply the UUID target when available; this is
                    # stable even if unrelated metadata records were appended
                    # after the checkpoint.  Older markers only carried an
                    # index, so retain the checkpoint/index fallback.
                    checkpoint_id = str(entry.get("checkpoint_id") or "")
                    rollback_seen = True
                    checkpoint = checkpoints.get(checkpoint_id, {})
                    checkpoint_projection = checkpoint_projections.get(checkpoint_id)
                    if checkpoint_projection is not None:
                        active_messages = list(checkpoint_projection)
                        active_seen = {
                            str(item.get("uuid"))
                            for item in active_messages
                            if item.get("uuid")
                        }
                        continue
                    target_uuid = str(
                        entry.get("message_uuid")
                        or checkpoint.get("message_uuid")
                        or ""
                    )
                    target_position: int | None = None
                    if target_uuid:
                        target_position = next(
                            (
                                index
                                for index, item in enumerate(active_messages)
                                if item.get("uuid") == target_uuid
                            ),
                            None,
                        )
                    if target_uuid:
                        # A UUID is stable across metadata records and compact
                        # boundaries. If it is no longer in the active
                        # projection, the checkpoint cannot be replayed safely;
                        # never guess using a stale pre-compaction index.
                        if target_position is not None:
                            active_messages = active_messages[: target_position + 1]
                        else:
                            continue
                    else:
                        try:
                            rollback_index = int(
                                entry.get(
                                    "rollback_to_index",
                                    checkpoint.get("message_index", -1),
                                )
                            )
                        except (TypeError, ValueError, OverflowError):
                            rollback_index = None
                        if rollback_index is not None:
                            active_messages = active_messages[: rollback_index + 1]
                    active_seen = {
                        str(item.get("uuid"))
                        for item in active_messages
                        if item.get("uuid")
                    }
                    continue

                # Future metadata records must never be reconstructed as empty
                # user messages.
                if not _is_message_entry(entry):
                    continue

                msg_uuid = entry.get("uuid", "")
                if msg_uuid and msg_uuid in seen:
                    # A normal record written after a boundary can duplicate a
                    # message embedded in that boundary; keep only one active copy.
                    if msg_uuid not in active_seen:
                        active_seen.add(msg_uuid)
                        active_messages.append(entry)
                    continue
                if msg_uuid:
                    seen.add(msg_uuid)
                all_messages.append(entry)
                if not msg_uuid or msg_uuid not in active_seen:
                    if msg_uuid:
                        active_seen.add(msg_uuid)
                    active_messages.append(entry)
        except Exception:
            logger.warning("Failed to load transcript: %s", self._transcript_path, exc_info=True)

        self._written_uuids = seen
        if archived or get_session_tombstone_path(self.cwd, self.session_id).exists():
            meta["is_archived"] = True
        if meta:
            self._meta = meta
            self._meta_written = True
        if full_history:
            return all_messages
        return active_messages if boundary_seen or rollback_seen else all_messages

    @classmethod
    def fork_from(
        cls,
        source_cwd: str,
        source_session_id: str,
        message_uuid: str,
        *,
        title: str | None = None,
    ) -> "SessionStorage":
        """Atomically clone durable conversation state up to an assistant reply."""
        source = cls(os.path.abspath(source_cwd), source_session_id)
        _validate_component(message_uuid, "message uuid")
        with _session_lifecycle_lock(source.cwd, source.session_id, exclusive=True):
            if not source._transcript_path.exists():
                raise ValueError(f"Session {source.session_id} not found")
            with _transcript_file_lock(source._transcript_path, exclusive=False):
                transcript = source._transcript_path.read_text(encoding="utf-8")
            messages = source.load_messages(_transcript_text=transcript)
            if source.meta.get("is_archived"):
                raise ValueError(f"Session {source.session_id} is archived")
            target_index = next(
                (
                    index for index, item in enumerate(messages)
                    if item.get("uuid") == message_uuid
                    and item.get("type") == "assistant"
                ),
                -1,
            )
            if target_index < 0:
                raise ValueError("Assistant message not found in active session history")
            cloned_messages = messages[: target_index + 1]
            source_meta = dict(source.meta)

        new_id = generate_session_id()
        destination = cls(source.cwd, new_id)
        source_title = str(
            source_meta.get("title")
            or source_meta.get("first_user_message")
            or ""
        )[:200]
        fork_title = str(title or "").strip()[:200] or (
            f"{source_title} · 分叉" if source_title else "分叉会话"
        )
        first_user = str(source_meta.get("first_user_message") or "")[:500]
        now = datetime.now(timezone.utc)
        metadata = destination._new_meta(
            model=str(source_meta.get("model") or ""),
            provider=str(source_meta.get("provider") or ""),
            first_user_message=first_user,
            now=now,
        )
        metadata.update(
            {
                "title": fork_title,
                "updated_at": now.isoformat(),
                "forked_from_session_id": source.session_id,
                "forked_from_message_uuid": message_uuid,
                "forked_from_title": source_title,
                "message_count": len(cloned_messages),
            }
        )
        for key in ("summary", "goal", "git_branch", "git_sha"):
            if source_meta.get(key) is not None:
                metadata[key] = source_meta[key]
        lines = [_dump_jsonl_line({"type": "session_meta", **metadata})]
        lines.extend(_dump_jsonl_line(item) for item in cloned_messages)
        with _session_lifecycle_lock(destination.cwd, destination.session_id):
            destination._ensure_dir()
            destination._atomic_write_text(destination._transcript_path, "".join(lines))
            destination._meta = metadata
            destination._meta_written = True
            destination._upsert_meta_locked()
        return destination

    def update_title(self, title: str) -> None:
        """Update the session title in JSONL, SQLite, and in-memory metadata."""
        try:
            self._commit_metadata_update(
                lambda _meta: {
                    "title": title,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        except Exception:
            logger.warning("Failed to persist session title for %s", self.session_id, exc_info=True)

    def update_model(self, *, model: str = "", provider: str = "") -> None:
        """Persist the active model/provider for an existing session.

        Model selection can change after the first turn. The update is merged
        with the latest on-disk metadata so another storage instance cannot
        overwrite counters or titles with a stale in-memory snapshot.
        """
        try:
            self._commit_metadata_update(
                lambda _meta: {
                    "model": model,
                    "provider": provider,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        except Exception:
            logger.warning(
                "Failed to persist model metadata: %s",
                self._transcript_path,
                exc_info=True,
            )

    def update_summary(self, summary: str) -> None:
        """Update the session summary in JSONL, SQLite, and in-memory metadata."""
        try:
            self._commit_metadata_update(
                lambda _meta: {
                    "summary": summary,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        except Exception:
            logger.warning("Failed to persist session summary for %s", self.session_id, exc_info=True)

    def update_goal(self, goal: dict[str, Any] | None) -> None:
        """Persist the session goal in the append-only JSONL metadata."""
        try:
            self._commit_metadata_update(
                lambda _meta: {
                    "goal": goal,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        except Exception:
            logger.warning("Failed to persist session goal for %s", self.session_id, exc_info=True)

    def record_tokens(self, tokens: int) -> None:
        """Accumulate token usage in JSONL and SQLite."""
        def accumulate(meta: dict[str, Any]) -> dict[str, Any]:
            try:
                previous_tokens = int(meta.get("tokens_used", 0) or 0)
            except (TypeError, ValueError, OverflowError):
                previous_tokens = 0
            return {
                "tokens_used": previous_tokens + int(tokens),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }

        try:
            self._commit_metadata_update(accumulate)
        except Exception:
            logger.warning("Failed to persist token usage for %s", self.session_id, exc_info=True)

    def record_context_usage(self, used_tokens: int, window_tokens: int) -> None:
        """Persist context window usage to JSONL so it survives session restore."""
        if not used_tokens and not window_tokens:
            return
        entry = {"type": "context_usage", "used_tokens": used_tokens, "window_tokens": window_tokens}
        self._append_transcript_line(entry)
        self.last_context_used_tokens = used_tokens
        self.last_context_window_tokens = window_tokens

    def record_message_count(self, count: int) -> None:
        """Update message count in JSONL and SQLite."""
        try:
            self._commit_metadata_update(
                lambda _meta: {
                    "message_count": int(count),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        except Exception:
            logger.warning("Failed to persist message count for %s", self.session_id, exc_info=True)

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
        # Initialize before entering the try block: importing or constructing
        # the metadata store can fail, and the finally block must still be
        # able to close a partially-created store without masking the failure.
        store = None
        cp_id: str | None = None
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
            # Write a marker line to JSONL for auditability
            entry = {
                "type": "checkpoint",
                "checkpoint_id": cp_id,
                "message_uuid": msg_uuid,
                "message_index": msg_index,
                "label": label,
                "snapshot_id": snapshot_id,
                # Keep the projection captured by the caller rather than
                # reconstructing it from whatever records happen to precede
                # this marker.  A compaction can commit between the SQLite
                # row and this JSONL append; without the embedded projection
                # that race changes what rollback restores.
                "messages": [_message_to_entry(message) for message in messages],
            }
            self._append_transcript_line(entry)
            return cp_id
        except Exception:
            # The SQLite row is created before the JSONL audit marker.  If the
            # marker cannot be committed, compensate while the same store is
            # still open so callers never receive None for a checkpoint that
            # remains active in list/rollback APIs.
            if cp_id is not None and store is not None:
                try:
                    store.delete_checkpoint(cp_id)
                except Exception:
                    logger.warning(
                        "Failed to remove partial checkpoint %s",
                        cp_id,
                        exc_info=True,
                    )
            logger.warning("Failed to create checkpoint for session %s", self.session_id, exc_info=True)
            return None
        finally:
            if store is not None:
                try:
                    store.close()
                except Exception:
                    logger.debug("Failed to close session metadata store", exc_info=True)

    def list_checkpoints(self) -> list[dict[str, Any]]:
        """List checkpoints for this session."""
        store = None
        try:
            from crabcode_core.session.meta_db import SessionMetaStore
            store = SessionMetaStore()
            cps = store.list_checkpoints(self.session_id)
            return cps
        except Exception:
            logger.debug("Failed to list checkpoints for session %s", self.session_id, exc_info=True)
            return []
        finally:
            if store is not None:
                try:
                    store.close()
                except Exception:
                    logger.debug("Failed to close session metadata store", exc_info=True)

    def rollback_to_checkpoint(self, checkpoint_id: str) -> int | None:
        """Get the message index to rollback to. Returns index or None if not found."""
        store = None
        try:
            from crabcode_core.session.meta_db import SessionMetaStore
            store = SessionMetaStore()
            cp = store.get_checkpoint(checkpoint_id)
            if cp and cp["session_id"] == self.session_id:
                target_uuid = str(cp.get("message_uuid") or "")
                # Write rollback marker to JSONL
                entry = {
                    "type": "rollback",
                    "checkpoint_id": checkpoint_id,
                    "rollback_to_index": cp["message_index"],
                    # New markers carry the stable target so a resumed session
                    # can replay the rollback even after other records or a
                    # compaction boundary changed the active list length.
                    "message_uuid": cp.get("message_uuid"),
                }
                self._append_transcript_line(entry)
                if target_uuid:
                    # Replaying the marker also handles checkpoints that
                    # predate a compact boundary (the parser retains the
                    # checkpoint-time projection). If the projection cannot
                    # be reconstructed, report failure instead of handing the
                    # caller a stale integer index.
                    active = self.load_messages()
                    if not any(item.get("uuid") == target_uuid for item in active):
                        return None
                return cp["message_index"]
        except Exception:
            logger.warning("Failed to rollback checkpoint %s", checkpoint_id, exc_info=True)
        finally:
            if store is not None:
                try:
                    store.close()
                except Exception:
                    logger.debug("Failed to close session metadata store", exc_info=True)
        return None

    @property
    def meta(self) -> dict[str, Any]:
        """Return the session metadata (from JSONL or empty dict)."""
        return self._meta

    @staticmethod
    def list_sessions(cwd: str) -> list[dict[str, Any]]:
        """List all sessions for a given working directory.

        SQLite supplies indexed rows, while JSONL scanning reconciles active
        sessions whose index write was missed. Durable archive state always
        wins over either source.
        """
        abs_cwd = os.path.abspath(cwd)
        sessions_by_id: dict[str, dict[str, Any]] = {}
        indexed_states: dict[str, bool] = {}
        store = None
        try:
            from crabcode_core.session.meta_db import SessionMetaStore

            store = SessionMetaStore()
            rows = store.list_by_cwd(abs_cwd, limit=100)
            list_states = getattr(store, "list_states_by_cwd", None)
            if callable(list_states):
                indexed_states = dict(list_states(abs_cwd))
            else:
                indexed_states = {str(row["id"]): bool(row.get("is_archived")) for row in rows}
            for row in rows:
                session_id = str(row["id"])
                if get_session_tombstone_path(abs_cwd, session_id).exists():
                    continue
                timestamp = row.get("updated_at", 0)
                try:
                    sort_timestamp = float(timestamp or 0)
                except (TypeError, ValueError, OverflowError):
                    sort_timestamp = 0.0
                sessions_by_id[session_id] = {
                    "session_id": session_id,
                    "title": row.get("title", ""),
                    "model": row.get("model", ""),
                    "provider": row.get("provider", ""),
                    "tokens_used": row.get("tokens_used", 0),
                    "git_branch": row.get("git_branch"),
                    "git_sha": row.get("git_sha"),
                    "message_count": row.get("message_count", 0),
                    "modified": (
                        datetime.fromtimestamp(sort_timestamp, tz=timezone.utc).isoformat()
                        if sort_timestamp
                        else ""
                    ),
                    "summary": row.get("summary", ""),
                    "forked_from_session_id": row.get("forked_from_session_id"),
                    "forked_from_message_uuid": row.get("forked_from_message_uuid"),
                    "forked_from_title": row.get("forked_from_title"),
                    "preview": (
                        row.get("summary", "")[:100]
                        or row.get("first_user_message", "")[:100]
                    ),
                    "_sort_timestamp": sort_timestamp,
                }
        except Exception:
            logger.debug("SQLite session listing failed for cwd %s", abs_cwd, exc_info=True)
        finally:
            if store is not None:
                try:
                    store.close()
                except Exception:
                    logger.debug("Failed to close session metadata store", exc_info=True)

        transcript_paths: list[Path] = []
        for project_dir in _project_dirs_for_read(abs_cwd):
            if project_dir.exists():
                transcript_paths.extend(project_dir.glob("*.jsonl"))

        def transcript_mtime(path: Path) -> float:
            try:
                return path.stat().st_mtime
            except OSError:
                return 0.0

        for path in sorted(transcript_paths, key=transcript_mtime, reverse=True):
            session_id = path.stem
            if session_id in sessions_by_id or indexed_states.get(session_id) is True:
                continue
            if get_session_tombstone_path(abs_cwd, session_id).exists():
                continue
            try:
                stat = path.stat()
                first_user_msg = ""
                meta_info: dict[str, Any] = {}
                archived = False
                with _transcript_file_lock(path, exclusive=False):
                    with open(path, encoding="utf-8") as f:
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
                            if entry.get("type") == "session_meta":
                                meta_info.update({k: v for k, v in entry.items() if k != "type"})
                                continue
                            if entry.get("type") == "session_archive":
                                archived = True
                                continue
                            if entry.get("type") == "session_restore":
                                archived = False
                                continue

                            # First non-meta user message is the preview
                            if entry.get("type") == "user" and not first_user_msg:
                                first_user_msg = _extract_preview(entry)
                            # Continue scanning: incremental title/model/count
                            # updates are appended as later session_meta records,
                            # and the final record is authoritative when SQLite is
                            # unavailable.

                declared_cwd = meta_info.get("cwd")
                if (
                    isinstance(declared_cwd, str)
                    and declared_cwd
                    and os.path.normcase(os.path.abspath(declared_cwd))
                    != os.path.normcase(abs_cwd)
                ):
                    continue
                if archived or meta_info.get("is_archived"):
                    continue
                sessions_by_id[session_id] = {
                    "session_id": session_id,
                    "title": meta_info.get("title", ""),
                    "model": meta_info.get("model", ""),
                    "provider": meta_info.get("provider", ""),
                    "tokens_used": meta_info.get("tokens_used", 0),
                    "git_branch": meta_info.get("git_branch"),
                    "git_sha": meta_info.get("git_sha"),
                    "message_count": meta_info.get("message_count", 0),
                    "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                    "summary": meta_info.get("summary", ""),
                    "forked_from_session_id": meta_info.get("forked_from_session_id"),
                    "forked_from_message_uuid": meta_info.get("forked_from_message_uuid"),
                    "forked_from_title": meta_info.get("forked_from_title"),
                    "preview": meta_info.get("first_user_message", "")[:100] or first_user_msg,
                    "_sort_timestamp": stat.st_mtime,
                }
            except Exception:
                logger.warning("Failed to inspect session transcript: %s", path, exc_info=True)
                sessions_by_id.setdefault(session_id, {
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
                    "_sort_timestamp": 0.0,
                })

        sessions = sorted(
            sessions_by_id.values(),
            key=lambda item: (float(item.get("_sort_timestamp", 0.0)), item["session_id"]),
            reverse=True,
        )
        for session in sessions:
            session.pop("_sort_timestamp", None)
        return sessions

    @classmethod
    def from_session_id(cls, session_id: str) -> "SessionStorage | None":
        """Resolve a session by ID using SQLite metadata (cross-project).

        Returns a SessionStorage pointed at the original cwd, or None if not found.
        """
        store = None
        try:
            from crabcode_core.session.meta_db import SessionMetaStore
            store = SessionMetaStore()
            row = store.get(session_id)
            if row and row.get("cwd"):
                return cls(cwd=row["cwd"], session_id=session_id)
        except Exception:
            logger.debug("from_session_id lookup failed for %s", session_id, exc_info=True)
        finally:
            if store is not None:
                try:
                    store.close()
                except Exception:
                    logger.debug("Failed to close session metadata store", exc_info=True)
        return None

    @staticmethod
    def search_sessions(query: str, limit: int = 20) -> list[dict[str, Any]]:
        """Search sessions across all projects by title or first message."""
        store = None
        try:
            from crabcode_core.session.meta_db import SessionMetaStore
            store = SessionMetaStore()
            rows = store.search(query, limit=limit)
            return rows
        except Exception:
            logger.warning("Session search failed for query %r", query, exc_info=True)
            return []
        finally:
            if store is not None:
                try:
                    store.close()
                except Exception:
                    logger.debug("Failed to close session metadata store", exc_info=True)


def _remove_session_artifact(path: Path, *, directory: bool) -> None:
    """Remove one exact session artifact without following directory symlinks."""
    try:
        path.lstat()
    except FileNotFoundError:
        return
    if path.is_symlink() or not directory:
        path.unlink()
        return
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def purge_session_artifacts(cwd: str, session_id: str) -> None:
    """Delete explicit session data artifacts while retaining its archive fence."""
    absolute_cwd = os.path.abspath(cwd)
    validated_id = _validate_component(session_id, "session id")
    storage = SessionStorage(absolute_cwd, validated_id)
    failures: list[OSError] = []

    with _session_lifecycle_lock(absolute_cwd, validated_id):
        storage._write_tombstone_locked()
        legacy_dir = _legacy_project_dir(absolute_cwd)
        for project_dir in _project_dirs_for_read(absolute_cwd):
            if project_dir == legacy_dir:
                legacy_transcript = project_dir / f"{validated_id}.jsonl"
                declared_cwd = _transcript_declared_cwd(legacy_transcript)
                if (
                    declared_cwd is not None
                    and os.path.normcase(declared_cwd) != os.path.normcase(absolute_cwd)
                ):
                    continue
            artifacts = (
                (project_dir / f"{validated_id}.jsonl", False),
                (project_dir / f"{validated_id}.agents.json", False),
                (project_dir / f"{validated_id}.agents", True),
                (project_dir / f"{validated_id}.tasks", True),
            )
            for path, is_directory in artifacts:
                try:
                    _remove_session_artifact(path, directory=is_directory)
                except OSError as exc:
                    failures.append(exc)

    if failures:
        raise failures[0]


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
