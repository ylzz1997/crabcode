"""SQLite session metadata store — fast queries for session listing and stats."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from crabcode_core.session.storage import get_config_home


def _db_path() -> Path:
    return get_config_home() / "sessions.db"


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
        meta.setdefault("created_at", now)
        meta["updated_at"] = now

        conn.execute(
            """INSERT OR REPLACE INTO session_meta
               (id, title, cwd, model, provider, first_user_message,
                tokens_used, git_branch, git_sha,
                created_at, updated_at, is_archived, message_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
        conn.execute(
            "UPDATE session_meta SET is_archived = 1, updated_at = ? WHERE id = ?",
            (int(datetime.now(timezone.utc).timestamp()), session_id),
        )
        conn.commit()

    def auto_archive(self, days: int = 30) -> int:
        """Archive sessions not updated in the last *days* days. Returns count archived."""
        conn = self._conn_or_create()
        cutoff = int(datetime.now(timezone.utc).timestamp()) - days * 86400
        cur = conn.execute(
            "UPDATE session_meta SET is_archived = 1 "
            "WHERE is_archived = 0 AND updated_at < ?",
            (cutoff,),
        )
        conn.commit()
        return cur.rowcount

    def purge_archived(self) -> list[dict[str, Any]]:
        """Delete archived rows from SQLite. Returns list of purged sessions (id, cwd)."""
        conn = self._conn_or_create()
        rows = conn.execute(
            "SELECT id, cwd FROM session_meta WHERE is_archived = 1"
        ).fetchall()
        if rows:
            conn.execute("DELETE FROM session_meta WHERE is_archived = 1")
            conn.commit()
        return [{"id": r[0], "cwd": r[1]} for r in rows]

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
