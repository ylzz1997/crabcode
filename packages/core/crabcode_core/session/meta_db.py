"""SQLite session metadata store — fast queries for session listing and stats."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from crabcode_core.session.storage import get_config_home


def _db_path() -> Path:
    return get_config_home() / "sessions.db"


def _coerce_epoch(value: Any, fallback: int) -> int:
    """Normalize JSONL ISO timestamps and legacy SQLite values to epoch seconds."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, str) and value:
        try:
            return int(value)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return int(parsed.timestamp())
            except (TypeError, ValueError, OverflowError):
                pass
    return fallback


def _persist_archive_marker(session_id: str, cwd: str) -> None:
    """Write the JSONL tombstone before SQLite archive state can disappear."""
    if not cwd:
        return
    from crabcode_core.session.storage import SessionStorage

    SessionStorage(cwd, session_id).persist_archive_marker()


_SCHEMA = """\
CREATE TABLE IF NOT EXISTS session_meta (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    cwd TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    first_user_message TEXT NOT NULL DEFAULT '',
    tokens_used INTEGER NOT NULL DEFAULT 0,
    git_branch TEXT,
    git_sha TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    is_archived INTEGER NOT NULL DEFAULT 0,
    message_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_session_meta_updated
    ON session_meta(updated_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_session_meta_cwd
    ON session_meta(cwd, updated_at DESC);
CREATE TABLE IF NOT EXISTS session_tombstones (
    id TEXT PRIMARY KEY,
    cwd TEXT NOT NULL DEFAULT '',
    archived_at INTEGER NOT NULL
);
CREATE TRIGGER IF NOT EXISTS prevent_tombstoned_session_insert
    BEFORE INSERT ON session_meta
    WHEN EXISTS (SELECT 1 FROM session_tombstones WHERE id = NEW.id)
    BEGIN
        SELECT RAISE(IGNORE);
    END;
"""


_MIGRATIONS = [
    "ALTER TABLE session_meta ADD COLUMN summary TEXT NOT NULL DEFAULT ''",
    """\
CREATE TABLE IF NOT EXISTS checkpoints (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    message_uuid TEXT NOT NULL,
    message_index INTEGER NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
)""",
    "CREATE INDEX IF NOT EXISTS idx_checkpoints_session ON checkpoints(session_id, created_at DESC)",
    "ALTER TABLE checkpoints ADD COLUMN snapshot_id TEXT",
]


def _get_conn(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    for stmt in _MIGRATIONS:
        try:
            conn.execute(stmt)
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column/table already exists
    now = int(datetime.now(timezone.utc).timestamp())
    legacy_rows = conn.execute(
        "SELECT id, created_at FROM session_meta WHERE typeof(created_at) != 'integer'"
    ).fetchall()
    for session_id, created_at in legacy_rows:
        conn.execute(
            "UPDATE session_meta SET created_at = ? WHERE id = ?",
            (_coerce_epoch(created_at, now), session_id),
        )
    if legacy_rows:
        conn.commit()
    conn.execute(
        "INSERT OR IGNORE INTO session_tombstones (id, cwd, archived_at) "
        "SELECT id, cwd, updated_at FROM session_meta WHERE is_archived = 1"
    )
    conn.commit()
    return conn


class SessionMetaStore:
    """SQLite-backed session metadata for fast listing and querying."""

    def __init__(self, db_path: Path | None = None):
        self._db_path = db_path or _db_path()
        self._conn: sqlite3.Connection | None = None

    def _conn_or_create(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = _get_conn(self._db_path)
        return self._conn

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def upsert(self, meta: dict[str, Any]) -> None:
        """Insert or update a session metadata row."""
        conn = self._conn_or_create()
        now = int(datetime.now(timezone.utc).timestamp())
        meta["created_at"] = _coerce_epoch(meta.get("created_at"), now)
        meta["updated_at"] = now

        # ``summary`` was added after the original upsert statement.  Keep an
        # explicit flag so the conflict update can preserve an existing value
        # atomically when older callers omit the field.
        summary = meta.get("summary")
        summary_provided = summary is not None

        conn.execute(
            """INSERT INTO session_meta
               (id, title, cwd, model, provider, first_user_message,
                tokens_used, git_branch, git_sha,
                created_at, updated_at, is_archived, message_count, summary)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   title = excluded.title,
                   cwd = excluded.cwd,
                   model = excluded.model,
                   provider = excluded.provider,
                   first_user_message = excluded.first_user_message,
                   tokens_used = excluded.tokens_used,
                   git_branch = excluded.git_branch,
                   git_sha = excluded.git_sha,
                   created_at = session_meta.created_at,
                   updated_at = excluded.updated_at,
                   -- Archiving is a terminal metadata transition. A late
                   -- session write must not make the row visible again.
                   is_archived = MAX(session_meta.is_archived, excluded.is_archived),
                   message_count = excluded.message_count,
                   summary = CASE
                       WHEN ? THEN excluded.summary
                       ELSE session_meta.summary
                   END""",
            (
                meta["id"],
                meta.get("title", ""),
                meta.get("cwd", ""),
                meta.get("model", ""),
                meta.get("provider", ""),
                meta.get("first_user_message", ""),
                meta.get("tokens_used", 0),
                meta.get("git_branch"),
                meta.get("git_sha"),
                meta["created_at"],
                meta["updated_at"],
                1 if meta.get("is_archived") else 0,
                meta.get("message_count", 0),
                summary or "",
                1 if summary_provided else 0,
            ),
        )
        conn.commit()

    def update_tokens(self, session_id: str, tokens: int) -> None:
        """Accumulate token usage for a session."""
        conn = self._conn_or_create()
        conn.execute(
            "UPDATE session_meta SET tokens_used = tokens_used + ?, updated_at = ? WHERE id = ?",
            (tokens, int(datetime.now(timezone.utc).timestamp()), session_id),
        )
        conn.commit()

    def update_title(self, session_id: str, title: str) -> None:
        """Update the title for a session."""
        conn = self._conn_or_create()
        conn.execute(
            "UPDATE session_meta SET title = ?, updated_at = ? WHERE id = ?",
            (title, int(datetime.now(timezone.utc).timestamp()), session_id),
        )
        conn.commit()

    def update_model(self, session_id: str, model: str, provider: str) -> bool:
        """Update only model fields, preserving accumulated session stats."""
        conn = self._conn_or_create()
        cur = conn.execute(
            "UPDATE session_meta SET model = ?, provider = ?, updated_at = ? WHERE id = ?",
            (
                model,
                provider,
                int(datetime.now(timezone.utc).timestamp()),
                session_id,
            ),
        )
        conn.commit()
        return cur.rowcount > 0

    def update_summary(self, session_id: str, summary: str) -> None:
        """Update the summary for a session."""
        conn = self._conn_or_create()
        conn.execute(
            "UPDATE session_meta SET summary = ?, updated_at = ? WHERE id = ?",
            (summary, int(datetime.now(timezone.utc).timestamp()), session_id),
        )
        conn.commit()

    def update_message_count(self, session_id: str, count: int) -> None:
        """Update message count and updated_at for a session."""
        conn = self._conn_or_create()
        conn.execute(
            "UPDATE session_meta SET message_count = ?, updated_at = ? WHERE id = ?",
            (count, int(datetime.now(timezone.utc).timestamp()), session_id),
        )
        conn.commit()

    def get(self, session_id: str) -> dict[str, Any] | None:
        """Get metadata for a single session."""
        conn = self._conn_or_create()
        row = conn.execute(
            "SELECT * FROM session_meta WHERE id = ?", (session_id,)
        ).fetchone()
        if not row:
            return None
        cols = [d[0] for d in conn.execute("SELECT * FROM session_meta LIMIT 0").description]
        return dict(zip(cols, row))

    def list_by_cwd(self, cwd: str, limit: int = 50) -> list[dict[str, Any]]:
        """List sessions for a project directory, most recent first."""
        conn = self._conn_or_create()
        rows = conn.execute(
            "SELECT * FROM session_meta WHERE cwd = ? AND is_archived = 0 "
            "ORDER BY updated_at DESC, id DESC LIMIT ?",
            (cwd, limit),
        ).fetchall()
        cols = [d[0] for d in conn.execute("SELECT * FROM session_meta LIMIT 0").description]
        return [dict(zip(cols, r)) for r in rows]

    def has_any_by_cwd(self, cwd: str) -> bool:
        """Return whether the index knows this project, including archived rows."""
        conn = self._conn_or_create()
        row = conn.execute(
            "SELECT 1 FROM session_meta WHERE cwd = ? LIMIT 1",
            (cwd,),
        ).fetchone()
        return row is not None

    def list_states_by_cwd(self, cwd: str) -> dict[str, bool]:
        """Return every indexed session ID mapped to its archived state."""
        conn = self._conn_or_create()
        rows = conn.execute(
            "SELECT id, is_archived FROM session_meta WHERE cwd = ?",
            (cwd,),
        ).fetchall()
        return {str(session_id): bool(is_archived) for session_id, is_archived in rows}

    def list_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """List all recent sessions across all projects."""
        conn = self._conn_or_create()
        rows = conn.execute(
            "SELECT * FROM session_meta WHERE is_archived = 0 "
            "ORDER BY updated_at DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        cols = [d[0] for d in conn.execute("SELECT * FROM session_meta LIMIT 0").description]
        return [dict(zip(cols, r)) for r in rows]

    def search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """Search sessions by title or first_user_message."""
        conn = self._conn_or_create()
        like = f"%{query}%"
        rows = conn.execute(
            "SELECT * FROM session_meta "
            "WHERE (title LIKE ? OR first_user_message LIKE ?) AND is_archived = 0 "
            "ORDER BY updated_at DESC LIMIT ?",
            (like, like, limit),
        ).fetchall()
        cols = [d[0] for d in conn.execute("SELECT * FROM session_meta LIMIT 0").description]
        return [dict(zip(cols, r)) for r in rows]

    def delete(self, session_id: str) -> None:
        """Delete a session metadata row."""
        conn = self._conn_or_create()
        conn.execute("DELETE FROM session_meta WHERE id = ?", (session_id,))
        conn.commit()

    def archive(self, session_id: str) -> None:
        """Mark a session as archived."""
        conn = self._conn_or_create()
        row = conn.execute(
            "SELECT cwd FROM session_meta WHERE id = ?",
            (session_id,),
        ).fetchone()
        if not row:
            return
        _persist_archive_marker(session_id, str(row[0] or ""))
        now = int(datetime.now(timezone.utc).timestamp())
        conn.execute(
            "UPDATE session_meta SET is_archived = 1, updated_at = ? WHERE id = ?",
            (now, session_id),
        )
        conn.execute(
            "INSERT OR IGNORE INTO session_tombstones (id, cwd, archived_at) VALUES (?, ?, ?)",
            (session_id, str(row[0] or ""), now),
        )
        conn.commit()

    def auto_archive(self, days: int = 30) -> int:
        """Archive sessions not updated in the last *days* days. Returns count archived."""
        conn = self._conn_or_create()
        cutoff = int(datetime.now(timezone.utc).timestamp()) - days * 86400
        candidates = conn.execute(
            "SELECT id, cwd FROM session_meta "
            "WHERE is_archived = 0 AND updated_at < ?",
            (cutoff,),
        ).fetchall()
        archived_ids: list[str] = []
        for session_id, cwd in candidates:
            try:
                _persist_archive_marker(str(session_id), str(cwd or ""))
            except Exception:
                # Keep the index row active when the durable tombstone cannot
                # be committed; a later prune can retry safely.
                continue
            archived_ids.append(str(session_id))
        now = int(datetime.now(timezone.utc).timestamp())
        if archived_ids:
            conn.executemany(
                "UPDATE session_meta SET is_archived = 1, updated_at = ? WHERE id = ?",
                ((now, session_id) for session_id in archived_ids),
            )
            cwd_by_id = {str(session_id): str(cwd or "") for session_id, cwd in candidates}
            conn.executemany(
                "INSERT OR IGNORE INTO session_tombstones (id, cwd, archived_at) "
                "VALUES (?, ?, ?)",
                (
                    (session_id, cwd_by_id.get(session_id, ""), now)
                    for session_id in archived_ids
                ),
            )
        conn.commit()
        return len(archived_ids)

    def purge_archived(self, *, delete_rows: bool = True) -> list[dict[str, Any]]:
        """Prepare archived rows for purge and optionally delete their index rows."""
        conn = self._conn_or_create()
        rows = conn.execute(
            "SELECT id, cwd FROM session_meta WHERE is_archived = 1"
        ).fetchall()
        purgeable: list[tuple[str, str]] = []
        for session_id, cwd in rows:
            try:
                # Upgrade old archived rows before removing their SQLite
                # tombstone. A failed transcript unlink then remains harmless.
                _persist_archive_marker(str(session_id), str(cwd or ""))
            except Exception:
                continue
            purgeable.append((str(session_id), str(cwd or "")))
        if purgeable:
            now = int(datetime.now(timezone.utc).timestamp())
            conn.executemany(
                "INSERT OR IGNORE INTO session_tombstones (id, cwd, archived_at) "
                "VALUES (?, ?, ?)",
                ((session_id, cwd, now) for session_id, cwd in purgeable),
            )
        if purgeable and delete_rows:
            conn.executemany(
                "DELETE FROM session_meta WHERE id = ?",
                ((session_id,) for session_id, _cwd in purgeable),
            )
        if purgeable:
            conn.commit()
        return [{"id": session_id, "cwd": cwd} for session_id, cwd in purgeable]

    # --- Checkpoints ---

    def create_checkpoint(
        self,
        session_id: str,
        message_uuid: str,
        message_index: int,
        label: str = "",
        snapshot_id: str | None = None,
    ) -> str:
        """Create a checkpoint at the given message position. Returns checkpoint ID."""
        import uuid
        cp_id = str(uuid.uuid4())
        conn = self._conn_or_create()
        conn.execute(
            "INSERT INTO checkpoints (id, session_id, message_uuid, message_index, label, snapshot_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (cp_id, session_id, message_uuid, message_index, label, snapshot_id,
             int(datetime.now(timezone.utc).timestamp())),
        )
        conn.commit()
        return cp_id

    def list_checkpoints(self, session_id: str) -> list[dict[str, Any]]:
        """List checkpoints for a session, newest first."""
        conn = self._conn_or_create()
        rows = conn.execute(
            "SELECT id, session_id, message_uuid, message_index, label, snapshot_id, created_at "
            "FROM checkpoints WHERE session_id = ? ORDER BY created_at DESC",
            (session_id,),
        ).fetchall()
        cols = ["id", "session_id", "message_uuid", "message_index", "label", "snapshot_id", "created_at"]
        return [dict(zip(cols, r)) for r in rows]

    def delete_checkpoint(self, checkpoint_id: str) -> None:
        conn = self._conn_or_create()
        conn.execute("DELETE FROM checkpoints WHERE id = ?", (checkpoint_id,))
        conn.commit()

    def get_checkpoint(self, checkpoint_id: str) -> dict[str, Any] | None:
        conn = self._conn_or_create()
        row = conn.execute(
            "SELECT id, session_id, message_uuid, message_index, label, snapshot_id, created_at "
            "FROM checkpoints WHERE id = ?",
            (checkpoint_id,),
        ).fetchone()
        if not row:
            return None
        cols = ["id", "session_id", "message_uuid", "message_index", "label", "snapshot_id", "created_at"]
        return dict(zip(cols, row))

    # --- Statistics ---

    def stats_global(self) -> dict[str, Any]:
        """Aggregate statistics across all sessions."""
        conn = self._conn_or_create()
        row = conn.execute(
            "SELECT COUNT(*) as total, "
            "COALESCE(SUM(tokens_used), 0) as total_tokens, "
            "COALESCE(SUM(message_count), 0) as total_messages, "
            "COUNT(DISTINCT cwd) as active_projects "
            "FROM session_meta WHERE is_archived = 0"
        ).fetchone()
        now = int(datetime.now(timezone.utc).timestamp())
        week_ago = now - 7 * 86400
        week_row = conn.execute(
            "SELECT COUNT(*) as week_sessions, "
            "COALESCE(SUM(tokens_used), 0) as week_tokens "
            "FROM session_meta WHERE is_archived = 0 AND created_at > ?",
            (week_ago,),
        ).fetchone()
        return {
            "total_sessions": row[0],
            "total_tokens": row[1],
            "total_messages": row[2],
            "active_projects": row[3],
            "week_sessions": week_row[0] if week_row else 0,
            "week_tokens": week_row[1] if week_row else 0,
        }

    def stats_by_project(self, cwd: str) -> dict[str, Any]:
        """Statistics for a specific project directory."""
        conn = self._conn_or_create()
        row = conn.execute(
            "SELECT COUNT(*) as total, "
            "COALESCE(SUM(tokens_used), 0) as total_tokens, "
            "COALESCE(SUM(message_count), 0) as total_messages "
            "FROM session_meta WHERE cwd = ? AND is_archived = 0",
            (cwd,),
        ).fetchone()
        return {
            "total_sessions": row[0],
            "total_tokens": row[1],
            "total_messages": row[2],
        }

    def stats_by_model(self, limit: int = 10) -> list[dict[str, Any]]:
        """Token usage aggregated by model."""
        conn = self._conn_or_create()
        rows = conn.execute(
            "SELECT model, COUNT(*) as sessions, "
            "COALESCE(SUM(tokens_used), 0) as tokens "
            "FROM session_meta WHERE is_archived = 0 AND model != '' "
            "GROUP BY model ORDER BY tokens DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [{"model": r[0], "sessions": r[1], "tokens": r[2]} for r in rows]
