"""SQLite schedule store — persistent storage for scheduled jobs and their runs."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from crabcode_core.schedule.models import JobRun, ScheduleJob


def _db_path() -> Path:
    """Default database path: ~/.crabcode/schedules.db"""
    return Path.home() / ".crabcode" / "schedules.db"


_SCHEMA = """\
CREATE TABLE IF NOT EXISTS schedules (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    prompt TEXT NOT NULL,
    schedule TEXT NOT NULL,
    schedule_type TEXT NOT NULL,
    cwd TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'active',
    last_run TEXT,
    next_run TEXT,
    run_count INTEGER NOT NULL DEFAULT 0,
    max_runs INTEGER,
    created_at TEXT NOT NULL,
    session_id TEXT,
    description TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '[]',
    timeout INTEGER,
    model_profile TEXT,
    extra TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS schedule_runs (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    started_at TEXT,
    finished_at TEXT,
    duration_seconds REAL,
    session_id TEXT,
    exit_code INTEGER,
    error_message TEXT,
    result_summary TEXT NOT NULL DEFAULT '',
    tokens_used INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY (job_id) REFERENCES schedules(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_schedule_runs_job
    ON schedule_runs(job_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_schedules_pending
    ON schedules(next_run)
    WHERE status = 'active' AND enabled = 1;
"""

_MIGRATIONS: list[str] = []


def _get_conn(db_path: Path) -> sqlite3.Connection:
    """Open (or create) the SQLite database and ensure the schema exists."""
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


def _row_to_schedule(row: tuple[Any, ...], cols: list[str]) -> dict[str, Any]:
    """Convert a raw DB row into a dict, deserialising JSON columns."""
    d = dict(zip(cols, row))
    # Deserialize JSON columns
    for key in ("tags", "extra"):
        val = d.get(key)
        if isinstance(val, str):
            try:
                d[key] = json.loads(val)
            except (json.JSONDecodeError, TypeError):
                d[key] = [] if key == "tags" else {}
    # Convert SQLite integer booleans
    if "enabled" in d and isinstance(d["enabled"], int):
        d["enabled"] = bool(d["enabled"])
    return d


def _row_to_run(row: tuple[Any, ...], cols: list[str]) -> dict[str, Any]:
    """Convert a raw schedule_runs row into a dict."""
    d = dict(zip(cols, row))
    return d


class ScheduleStore:
    """SQLite-backed persistence for :class:`ScheduleJob` and :class:`JobRun`."""

    def __init__(self, db_path: Path | None = None):
        self._db_path = db_path or _db_path()
        self._conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def _conn_or_create(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = _get_conn(self._db_path)
        return self._conn

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # Schedule CRUD
    # ------------------------------------------------------------------

    def upsert_schedule(self, job: ScheduleJob) -> None:
        """Insert or update a :class:`ScheduleJob` row."""
        conn = self._conn_or_create()
        conn.execute(
            """INSERT OR REPLACE INTO schedules
               (id, name, prompt, schedule, schedule_type, cwd, enabled, status,
                last_run, next_run, run_count, max_runs, created_at, session_id,
                description, tags, timeout, model_profile, extra)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                job.id,
                job.name,
                job.prompt,
                job.schedule,
                job.schedule_type.value,
                job.cwd,
                1 if job.enabled else 0,
                job.status.value,
                job.last_run,
                job.next_run,
                job.run_count,
                job.max_runs,
                job.created_at,
                job.session_id,
                job.description,
                json.dumps(job.tags, ensure_ascii=False),
                job.timeout,
                job.model_profile,
                json.dumps(job.extra, ensure_ascii=False),
            ),
        )
        conn.commit()

    def get_schedule(self, job_id: str) -> dict[str, Any] | None:
        """Return a single schedule as a dict, or ``None`` if not found."""
        conn = self._conn_or_create()
        row = conn.execute(
            "SELECT * FROM schedules WHERE id = ?", (job_id,)
        ).fetchone()
        if not row:
            return None
        cols = [d[0] for d in conn.execute("SELECT * FROM schedules LIMIT 0").description]
        return _row_to_schedule(row, cols)

    def list_schedules(
        self,
        *,
        status: str | None = None,
        schedule_type: str | None = None,
        enabled: bool | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List schedules with optional filtering, most recently created first."""
        conn = self._conn_or_create()
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if schedule_type is not None:
            clauses.append("schedule_type = ?")
            params.append(schedule_type)
        if enabled is not None:
            clauses.append("enabled = ?")
            params.append(1 if enabled else 0)

        where = ""
        if clauses:
            where = "WHERE " + " AND ".join(clauses)

        rows = conn.execute(
            f"SELECT * FROM schedules {where} ORDER BY created_at DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        cols = [d[0] for d in conn.execute("SELECT * FROM schedules LIMIT 0").description]
        return [_row_to_schedule(r, cols) for r in rows]

    def delete_schedule(self, job_id: str) -> None:
        """Delete a schedule and all its associated runs (cascade)."""
        conn = self._conn_or_create()
        # Delete runs first (defensive — FK ON DELETE CASCADE should handle it)
        conn.execute("DELETE FROM schedule_runs WHERE job_id = ?", (job_id,))
        conn.execute("DELETE FROM schedules WHERE id = ?", (job_id,))
        conn.commit()

    # ------------------------------------------------------------------
    # JobRun operations
    # ------------------------------------------------------------------

    def record_run(self, run: JobRun) -> None:
        """Persist a :class:`JobRun` record (insert only — runs are immutable)."""
        conn = self._conn_or_create()
        conn.execute(
            """INSERT INTO schedule_runs
               (id, job_id, status, started_at, finished_at, duration_seconds,
                session_id, exit_code, error_message, result_summary,
                tokens_used, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run.id,
                run.job_id,
                run.status.value,
                run.started_at,
                run.finished_at,
                run.duration_seconds,
                run.session_id,
                run.exit_code,
                run.error_message,
                run.result_summary,
                run.tokens_used,
                run.created_at,
            ),
        )
        conn.commit()

    def update_run(self, run: JobRun) -> None:
        """Update an existing :class:`JobRun` record (e.g. status transition)."""
        conn = self._conn_or_create()
        conn.execute(
            """UPDATE schedule_runs
               SET status = ?, started_at = ?, finished_at = ?,
                   duration_seconds = ?, session_id = ?, exit_code = ?,
                   error_message = ?, result_summary = ?, tokens_used = ?
               WHERE id = ?""",
            (
                run.status.value,
                run.started_at,
                run.finished_at,
                run.duration_seconds,
                run.session_id,
                run.exit_code,
                run.error_message,
                run.result_summary,
                run.tokens_used,
                run.id,
            ),
        )
        conn.commit()

    def list_runs(
        self,
        job_id: str,
        *,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List runs for a given job, newest first."""
        conn = self._conn_or_create()
        if status is not None:
            rows = conn.execute(
                "SELECT * FROM schedule_runs WHERE job_id = ? AND status = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (job_id, status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM schedule_runs WHERE job_id = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (job_id, limit),
            ).fetchall()
        cols = [d[0] for d in conn.execute("SELECT * FROM schedule_runs LIMIT 0").description]
        return [_row_to_run(r, cols) for r in rows]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        """Return a single run as a dict, or ``None`` if not found."""
        conn = self._conn_or_create()
        row = conn.execute(
            "SELECT * FROM schedule_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if not row:
            return None
        cols = [d[0] for d in conn.execute("SELECT * FROM schedule_runs LIMIT 0").description]
        return _row_to_run(row, cols)

    # ------------------------------------------------------------------
    # Restart recovery
    # ------------------------------------------------------------------

    def get_pending_schedules(self, now_iso: str | None = None) -> list[dict[str, Any]]:
        """Return schedules that are due for execution (for restart recovery).

        A schedule is *pending* when all of the following hold:
        - ``status`` is ``active``
        - ``enabled`` is true
        - ``next_run`` is not ``NULL`` and ≤ *now*
        - ``max_runs`` has not been reached (or is unlimited)

        This is the primary method used by the scheduler engine on startup
        to discover jobs that fired while the process was down.
        """
        conn = self._conn_or_create()
        now = now_iso or datetime.now(timezone.utc).isoformat()

        rows = conn.execute(
            """SELECT * FROM schedules
               WHERE status = 'active'
                 AND enabled = 1
                 AND next_run IS NOT NULL
                 AND next_run <= ?
                 AND (max_runs IS NULL OR run_count < max_runs)
               ORDER BY next_run ASC""",
            (now,),
        ).fetchall()
        cols = [d[0] for d in conn.execute("SELECT * FROM schedules LIMIT 0").description]
        return [_row_to_schedule(r, cols) for r in rows]

    def get_stale_running_runs(self) -> list[dict[str, Any]]:
        """Return runs that are still marked as ``running`` (orphaned after a crash).

        These should be transitioned to ``failed`` on startup.
        """
        conn = self._conn_or_create()
        rows = conn.execute(
            "SELECT * FROM schedule_runs WHERE status = 'running'"
        ).fetchall()
        cols = [d[0] for d in conn.execute("SELECT * FROM schedule_runs LIMIT 0").description]
        return [_row_to_run(r, cols) for r in rows]
