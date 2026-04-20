"""Core data models for the Schedule subsystem.

Defines ScheduleJob, JobRun and related types used by the scheduler engine
and persistence layer.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ScheduleType(str, Enum):
    """How a job is triggered."""

    CRON = "cron"
    INTERVAL = "interval"
    ONCE = "once"


class JobStatus(str, Enum):
    """Lifecycle status of a ScheduleJob."""

    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    DISABLED = "disabled"
    ERROR = "error"


class RunStatus(str, Enum):
    """Status of an individual JobRun execution."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"
    SKIPPED = "skipped"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ScheduleJob(BaseModel):
    """A scheduled job definition.

    A job encapsulates a prompt that will be executed on a time-based
    schedule (cron expression, fixed interval, or a single one-shot).
    """

    id: str = Field(default_factory=_new_id, description="Unique job identifier")
    name: str = Field(description="Human-readable job name")
    prompt: str = Field(description="The prompt to send to the agent when the job fires")
    schedule: str = Field(
        description="Schedule definition: cron expression, interval string (e.g. '30m', '2h'), or ISO timestamp for once"
    )
    schedule_type: ScheduleType = Field(description="Type of schedule: cron | interval | once")
    cwd: str | None = Field(default=None, description="Working directory for the job execution")
    enabled: bool = Field(default=True, description="Whether the job is enabled")
    status: JobStatus = Field(default=JobStatus.ACTIVE, description="Current lifecycle status")
    last_run: str | None = Field(default=None, description="ISO timestamp of the last run")
    next_run: str | None = Field(default=None, description="ISO timestamp of the next scheduled run")
    run_count: int = Field(default=0, description="Total number of completed runs")
    max_runs: int | None = Field(
        default=None, description="Maximum allowed runs (None = unlimited)"
    )
    created_at: str = Field(default_factory=_now_iso, description="ISO timestamp of creation")
    session_id: str | None = Field(
        default=None,
        description="Associated session ID (reused if set, otherwise a new session is created per run)",
    )

    # Optional metadata
    description: str = Field(default="", description="Optional longer description of the job")
    tags: list[str] = Field(default_factory=list, description="Arbitrary tags for filtering")
    timeout: int | None = Field(
        default=None, description="Per-job timeout in seconds (overrides ScheduleSettings.default_timeout)"
    )
    model_profile: str | None = Field(
        default=None, description="Model profile to use for this job's agent"
    )
    extra: dict[str, Any] = Field(
        default_factory=dict, description="Extension key-value pairs for future use"
    )

    def mark_running(self) -> None:
        """Transition to active status when a run starts."""
        if self.status in (JobStatus.ACTIVE, JobStatus.ERROR):
            self.status = JobStatus.ACTIVE

    def mark_completed(self) -> None:
        """Mark the job as fully completed (e.g. max_runs reached or one-shot done)."""
        self.status = JobStatus.COMPLETED

    def increment_run(self, timestamp: str | None = None) -> None:
        """Record a successful run: bump run_count and set last_run."""
        self.run_count += 1
        self.last_run = timestamp or _now_iso()
        # Auto-complete when max_runs is reached
        if self.max_runs is not None and self.run_count >= self.max_runs:
            self.status = JobStatus.COMPLETED


class JobRun(BaseModel):
    """A single execution record of a ScheduleJob.

    Each time a job fires, a JobRun is created to track that invocation's
    lifecycle from pending → running → success/failed/timeout.
    """

    id: str = Field(default_factory=_new_id, description="Unique run identifier")
    job_id: str = Field(description="Foreign key to the parent ScheduleJob.id")
    status: RunStatus = Field(default=RunStatus.PENDING, description="Current execution status")
    started_at: str | None = Field(default=None, description="ISO timestamp when the run started")
    finished_at: str | None = Field(default=None, description="ISO timestamp when the run finished")
    duration_seconds: float | None = Field(
        default=None, description="Wall-clock duration in seconds"
    )
    session_id: str | None = Field(
        default=None, description="Session used for this specific run"
    )
    exit_code: int | None = Field(default=None, description="Process exit code if applicable")
    error_message: str | None = Field(default=None, description="Error details on failure")
    result_summary: str = Field(
        default="", description="Short summary of the run outcome"
    )
    tokens_used: int = Field(default=0, description="Token usage for this run")
    created_at: str = Field(default_factory=_now_iso, description="ISO timestamp of creation")

    def start(self, session_id: str | None = None) -> None:
        """Transition from PENDING → RUNNING."""
        if self.status != RunStatus.PENDING:
            return
        self.status = RunStatus.RUNNING
        self.started_at = _now_iso()
        if session_id:
            self.session_id = session_id

    def succeed(self, summary: str = "", tokens: int = 0) -> None:
        """Transition from RUNNING → SUCCESS."""
        if self.status != RunStatus.RUNNING:
            return
        self.status = RunStatus.SUCCESS
        self.finished_at = _now_iso()
        self._compute_duration()
        self.result_summary = summary
        self.tokens_used = tokens

    def fail(self, error: str = "", exit_code: int | None = None) -> None:
        """Transition from RUNNING → FAILED."""
        if self.status != RunStatus.RUNNING:
            return
        self.status = RunStatus.FAILED
        self.finished_at = _now_iso()
        self._compute_duration()
        self.error_message = error
        self.exit_code = exit_code

    def timeout(self) -> None:
        """Transition from RUNNING → TIMEOUT."""
        if self.status != RunStatus.RUNNING:
            return
        self.status = RunStatus.TIMEOUT
        self.finished_at = _now_iso()
        self._compute_duration()
        self.error_message = "Job execution timed out"

    def skip(self, reason: str = "") -> None:
        """Transition from PENDING → SKIPPED (e.g. concurrency limit)."""
        if self.status != RunStatus.PENDING:
            return
        self.status = RunStatus.SKIPPED
        self.finished_at = _now_iso()
        self.result_summary = reason or "Skipped"

    def _compute_duration(self) -> None:
        """Calculate duration_seconds from started_at and finished_at."""
        if self.started_at and self.finished_at:
            try:
                start = datetime.fromisoformat(self.started_at)
                end = datetime.fromisoformat(self.finished_at)
                self.duration_seconds = (end - start).total_seconds()
            except (ValueError, TypeError):
                pass
