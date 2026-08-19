"""Runtime scheduler for persistent CrabCode jobs.

The manager is intentionally small and dependency-free.  SQLite provides the
cross-process lease, while one asyncio task owns the local polling loop and
the bounded set of executions.  A caller can inject an executor in tests or
for an embedding application; the default executor runs a detached
``CoreSession``.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from crabcode_core.logging_utils import get_logger
from crabcode_core.schedule.models import JobRun, JobStatus, RunStatus, ScheduleJob, ScheduleType
from crabcode_core.schedule.store import ScheduleStore
from crabcode_core.schedule.timing import compute_next_run, parse_iso_timestamp
from crabcode_core.types.config import ScheduleSettings
from crabcode_core.types.event import ScheduleRunEvent

logger = get_logger(__name__)


@dataclass(slots=True)
class ScheduleExecutionResult:
    """Normalized result returned by a scheduled-job executor."""

    summary: str = ""
    tokens_used: int = 0
    session_id: str | None = None


ScheduleExecutor = Callable[[ScheduleJob], Awaitable[ScheduleExecutionResult | str | dict[str, Any] | None]]
EventSink = Callable[[ScheduleRunEvent], Awaitable[None] | None]


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_job(value: ScheduleJob | dict[str, Any]) -> ScheduleJob:
    return value if isinstance(value, ScheduleJob) else ScheduleJob.model_validate(value)


class ScheduleManager:
    """Manage scheduled jobs and execute them in the background."""

    def __init__(
        self,
        *,
        settings: ScheduleSettings | None = None,
        cwd: str = ".",
        session_id: str = "",
        store: ScheduleStore | None = None,
        event_sink: EventSink | None = None,
        executor: ScheduleExecutor | None = None,
    ) -> None:
        self.settings = settings or ScheduleSettings()
        self.cwd = os.path.abspath(cwd)
        self.session_id = session_id
        self._store = store or ScheduleStore(
            Path(":memory:") if not self.settings.persist else None
        )
        self._owns_store = store is None
        self._event_sink = event_sink
        self._executor = executor or self._default_executor
        self._owner = f"{os.getpid()}:{uuid.uuid4()}"
        self._wake = asyncio.Event()
        self._lifecycle_lock = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None
        self._runner_task: asyncio.Task[None] | None = None
        self._active_tasks: dict[str, asyncio.Task[None]] = {}
        self._event_tasks: set[asyncio.Future[Any]] = set()
        self._closing = False

    @property
    def store(self) -> ScheduleStore:
        return self._store

    @property
    def running(self) -> bool:
        return self._runner_task is not None and not self._runner_task.done()

    def update_context(
        self,
        *,
        cwd: str | None = None,
        session_id: str | None = None,
        settings: ScheduleSettings | None = None,
    ) -> None:
        """Refresh defaults used by jobs created after a session switch."""
        if cwd is not None:
            self.cwd = os.path.abspath(cwd)
        if session_id is not None:
            self.session_id = session_id
        if settings is not None:
            self.settings = settings
        self._wake.set()

    async def reconfigure(
        self,
        *,
        settings: ScheduleSettings,
        cwd: str | None = None,
        session_id: str | None = None,
    ) -> None:
        """Apply project settings and start or suspend the runtime."""
        if self._closing:
            raise RuntimeError("ScheduleManager is closed")
        self.update_context(cwd=cwd, session_id=session_id, settings=settings)
        if settings.enabled:
            await self.start()
        else:
            await self._stop_runtime()

    async def start(self) -> None:
        """Start polling and recover interrupted executions."""
        if not self.settings.enabled:
            return
        async with self._lifecycle_lock:
            if self._closing:
                raise RuntimeError("ScheduleManager is closed")
            if self.running:
                return
            for stale in self._store.get_stale_running_runs():
                run = JobRun.model_validate(stale)
                run.fail("Scheduler stopped before this run completed")
                self._store.update_run(run)
            self._runner_task = asyncio.create_task(
                self._run_loop(),
                name="crabcode-schedule-loop",
            )
            self._wake.set()

    async def close(self) -> None:
        """Stop polling, cancel executions, and release SQLite leases."""
        async with self._lifecycle_lock:
            if self._close_task is None:
                if self._closing:
                    return
                self._closing = True
                self._close_task = asyncio.create_task(self._close_impl())
            task = self._close_task
        await asyncio.shield(task)

    async def _close_impl(self) -> None:
        await self._stop_runtime()
        if self._owns_store:
            self._store.close()

    async def _stop_runtime(self) -> None:
        """Stop local tasks without permanently closing the manager/store."""
        async with self._lifecycle_lock:
            runner = self._runner_task
            self._runner_task = None
            active = list(self._active_tasks.values())
            event_tasks = list(self._event_tasks)
            for task in [runner, *active, *event_tasks]:
                if task is not None and not task.done():
                    task.cancel()
        pending = [task for task in [runner, *active, *event_tasks] if task is not None]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        try:
            self._store.release_all_claims(self._owner)
        except Exception:
            logger.warning("Failed to release schedule leases", exc_info=True)

    # ------------------------------------------------------------------
    # Synchronous CRUD used by tools and HTTP adapters
    # ------------------------------------------------------------------

    def create_job(
        self,
        *,
        name: str,
        prompt: str,
        schedule: str,
        schedule_type: ScheduleType | str,
        cwd: str | None = None,
        enabled: bool = True,
        max_runs: int | None = None,
        next_run: str | None = None,
        description: str = "",
        tags: list[str] | None = None,
        timeout: int | None = None,
        model_profile: str | None = None,
        session_id: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> ScheduleJob:
        if not self.settings.enabled:
            raise RuntimeError("Scheduling is disabled by configuration")
        if not str(name).strip():
            raise ValueError("name is required")
        if not str(prompt).strip():
            raise ValueError("prompt is required")
        kind = ScheduleType(schedule_type)
        if max_runs is not None and max_runs <= 0:
            raise ValueError("max_runs must be greater than zero")
        configured_max = self.settings.max_runs_per_job
        if configured_max is not None:
            max_runs = (
                configured_max
                if max_runs is None
                else min(max_runs, configured_max)
            )
        if timeout is not None and timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        if timeout is None:
            timeout = self.settings.default_timeout

        # Recompute rather than trusting a caller-provided timestamp.  The
        # latter is retained as an escape hatch for importing existing jobs,
        # but malformed values should never enter the persistent queue.
        computed = compute_next_run(schedule, kind)
        if next_run is not None:
            parse_iso_timestamp(next_run)
            computed = next_run

        job = ScheduleJob(
            name=str(name).strip(),
            prompt=str(prompt),
            schedule=str(schedule).strip(),
            schedule_type=kind,
            cwd=os.path.abspath(cwd or self.cwd),
            enabled=bool(enabled),
            status=JobStatus.ACTIVE if enabled else JobStatus.PAUSED,
            max_runs=max_runs,
            next_run=computed,
            description=description,
            tags=list(tags or []),
            timeout=timeout,
            model_profile=model_profile,
            session_id=session_id,
            extra=dict(extra or {}),
        )
        self._store.upsert_schedule(job)
        self._wake.set()
        return job

    def list_jobs(
        self,
        *,
        status: str | None = None,
        schedule_type: str | None = None,
        enabled: bool | None = None,
        limit: int = 100,
    ) -> list[ScheduleJob]:
        return [
            _as_job(row)
            for row in self._store.list_schedules(
                status=status,
                schedule_type=schedule_type,
                enabled=enabled,
                limit=max(1, min(int(limit), 1000)),
            )
        ]

    def get_job(self, job_id: str) -> ScheduleJob | None:
        row = self._store.resolve_schedule(job_id)
        return _as_job(row) if row else None

    def list_runs(
        self,
        job_id: str,
        *,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        job = self.get_job(job_id)
        if job is None:
            return []
        return self._store.list_runs(
            job.id,
            status=status,
            limit=max(1, min(int(limit), 1000)),
        )

    def cancel_job(self, job_id: str) -> bool:
        job = self.get_job(job_id)
        if job is None:
            return False
        task = self._active_tasks.get(job.id)
        if task is not None and not task.done():
            task.cancel()
        self._store.delete_schedule(job.id)
        self._wake.set()
        return True

    def pause_job(self, job_id: str) -> ScheduleJob | None:
        job = self.get_job(job_id)
        if job is None:
            return None
        if job.status in {JobStatus.COMPLETED, JobStatus.DISABLED, JobStatus.ERROR}:
            return job
        task = self._active_tasks.get(job.id)
        if task is not None and not task.done():
            task.cancel()
        job.enabled = False
        job.status = JobStatus.PAUSED
        self._store.upsert_schedule(job)
        self._wake.set()
        return job

    def resume_job(self, job_id: str) -> ScheduleJob | None:
        job = self.get_job(job_id)
        if job is None:
            return None
        if job.status == JobStatus.COMPLETED:
            return job
        job.enabled = True
        job.status = JobStatus.ACTIVE
        job.next_run = compute_next_run(job.schedule, job.schedule_type)
        self._store.upsert_schedule(job)
        self._wake.set()
        return job

    async def trigger_job(self, job_id: str) -> bool:
        """Run one job immediately without changing its recurring schedule."""
        job = self.get_job(job_id)
        if job is None or not job.enabled or job.status != JobStatus.ACTIVE:
            return False
        if not self._store.claim_schedule(
            job.id,
            self._owner,
            lease_seconds=max(60, (job.timeout or self.settings.default_timeout) + 60),
        ):
            return False
        self._recover_job_runs(job.id)
        task = asyncio.create_task(self._execute_claimed_job(job, manual=True))
        self._active_tasks[job.id] = task
        task.add_done_callback(lambda done: self._forget_task(job.id, done))
        return True

    # ------------------------------------------------------------------
    # Polling and execution
    # ------------------------------------------------------------------

    async def _run_loop(self) -> None:
        try:
            while not self._closing:
                try:
                    capacity = max(
                        0,
                        max(1, int(self.settings.max_concurrent_jobs))
                        - len(self._active_tasks),
                    )
                    if capacity:
                        claimed = self._store.claim_due_schedules(
                            self._owner,
                            limit=capacity,
                            lease_seconds=max(
                                60,
                                int(self.settings.default_timeout) + 60,
                            ),
                        )
                        for row in claimed:
                            job = _as_job(row)
                            self._recover_job_runs(job.id)
                            task = asyncio.create_task(self._execute_claimed_job(job))
                            self._active_tasks[job.id] = task
                            task.add_done_callback(
                                lambda done, jid=job.id: self._forget_task(jid, done)
                            )
                        if claimed:
                            continue

                    timeout = 30.0
                    next_wakeup = self._store.get_next_wakeup()
                    if next_wakeup is not None:
                        try:
                            delay = (
                                parse_iso_timestamp(next_wakeup)
                                - datetime.now(timezone.utc)
                            ).total_seconds()
                            timeout = max(0.01, min(timeout, delay))
                        except ValueError:
                            pass
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=timeout)
                    except asyncio.TimeoutError:
                        pass
                    self._wake.clear()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Schedule polling iteration failed")
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=5.0)
                    except asyncio.TimeoutError:
                        pass
                    self._wake.clear()
        except asyncio.CancelledError:
            raise

    def _forget_task(self, job_id: str, task: asyncio.Task[None]) -> None:
        if self._active_tasks.get(job_id) is task:
            self._active_tasks.pop(job_id, None)
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.warning("Scheduled job %s failed outside execution fence", job_id, exc_info=True)
        self._wake.set()

    def _recover_job_runs(self, job_id: str) -> None:
        """Fail orphaned runs after this manager acquires their job lease."""
        for stale in self._store.list_runs(
            job_id,
            status=RunStatus.RUNNING.value,
            limit=1000,
        ):
            run = JobRun.model_validate(stale)
            run.fail("Scheduler lease expired before this run completed")
            self._store.update_run(run)

    async def _execute_claimed_job(self, job: ScheduleJob, *, manual: bool = False) -> None:
        run = JobRun(job_id=job.id)
        self._store.record_run(run)
        run.start()
        self._store.update_run(run)
        timeout = max(1, int(job.timeout or self.settings.default_timeout))
        try:
            self._store.renew_claim(job.id, self._owner, timeout + 60)
            raw_result = await asyncio.wait_for(self._executor(job), timeout=timeout)
            result = self._normalize_result(raw_result)
            run.succeed(result.summary, result.tokens_used)
            if result.session_id:
                run.session_id = result.session_id
            self._finish_job(job, run, success=True, manual=manual)
        except asyncio.TimeoutError:
            run.timeout()
            self._finish_job(job, run, success=False, manual=manual)
        except asyncio.CancelledError:
            # Cancellation during manager shutdown leaves the occurrence due;
            # restart recovery will mark this run failed and try it again.
            if run.status == RunStatus.RUNNING:
                run.fail("Scheduled job cancelled")
                self._store.update_run(run)
            self._store.release_claim(job.id, self._owner)
            raise
        except Exception as exc:
            run.fail(str(exc))
            self._finish_job(job, run, success=False, manual=manual)

    @staticmethod
    def _normalize_result(value: Any) -> ScheduleExecutionResult:
        if isinstance(value, ScheduleExecutionResult):
            return value
        if isinstance(value, str):
            return ScheduleExecutionResult(summary=value)
        if isinstance(value, dict):
            return ScheduleExecutionResult(
                summary=str(value.get("summary") or value.get("result_summary") or ""),
                tokens_used=int(value.get("tokens_used") or 0),
                session_id=value.get("session_id"),
            )
        return ScheduleExecutionResult()

    def _finish_job(
        self,
        original: ScheduleJob,
        run: JobRun,
        *,
        success: bool,
        manual: bool,
    ) -> None:
        self._store.update_run(run)
        current_row = self._store.get_schedule(original.id)
        if current_row is None:
            return
        current = _as_job(current_row)
        current.run_count += 1
        current.last_run = run.finished_at or _iso_now()
        next_run: str | None = current.next_run
        if current.schedule_type == ScheduleType.ONCE:
            current.next_run = None
            current.enabled = False
            current.status = JobStatus.COMPLETED if success else JobStatus.ERROR
            self._store.upsert_schedule(current)
        elif manual:
            # A manual trigger consumes a run count but leaves the scheduled
            # occurrence untouched unless max_runs has now been reached.
            if current.max_runs is not None and current.run_count >= current.max_runs:
                current.status = JobStatus.COMPLETED
                current.enabled = False
                current.next_run = None
            self._store.upsert_schedule(current)
        elif current.max_runs is not None and current.run_count >= current.max_runs:
            current.next_run = None
            current.enabled = False
            current.status = JobStatus.COMPLETED
            self._store.upsert_schedule(current)
        else:
            try:
                previous = parse_iso_timestamp(next_run) if next_run else None
                current.next_run = compute_next_run(
                    current.schedule,
                    current.schedule_type,
                    previous_run=previous,
                )
                current.status = JobStatus.ACTIVE
                current.enabled = True
                self._store.upsert_schedule(current)
            except Exception as exc:
                current.status = JobStatus.ERROR
                current.enabled = False
                current.next_run = None
                current.extra["schedule_error"] = str(exc)
                self._store.upsert_schedule(current)

        self._store.release_claim(original.id, self._owner)
        event = ScheduleRunEvent(
            job_id=original.id,
            run_id=run.id,
            status=run.status.value,
            duration_seconds=run.duration_seconds or 0.0,
            error_message=run.error_message or "",
            result_summary=run.result_summary or "",
            next_run=(self._store.get_schedule(original.id) or {}).get("next_run"),
        )
        if self._event_sink is not None:
            try:
                outcome = self._event_sink(event)
                if inspect.isawaitable(outcome):
                    task = asyncio.ensure_future(outcome)
                    self._event_tasks.add(task)
                    task.add_done_callback(self._finish_event_task)
            except Exception:
                logger.warning("Failed to publish schedule event", exc_info=True)

    def _finish_event_task(self, task: asyncio.Future[Any]) -> None:
        self._event_tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            logger.warning("Failed to publish schedule event", exc_info=True)

    async def _default_executor(self, job: ScheduleJob) -> ScheduleExecutionResult:
        """Execute a job in an isolated, non-interactive CoreSession."""
        from crabcode_core.events import CoreSession
        from crabcode_core.types.config import CrabCodeSettings
        from crabcode_core.permissions.manager import PermissionMode
        from crabcode_core.types.event import ErrorEvent, StreamTextEvent, TurnCompleteEvent

        explicit = CrabCodeSettings()
        explicit.schedule.enabled = False
        if job.model_profile:
            explicit.default_model = job.model_profile
        settings = CrabCodeSettings()
        settings._crabcode_explicit_settings = explicit
        child = CoreSession(cwd=job.cwd or self.cwd, settings=settings)
        output: list[str] = []
        tokens = 0
        errors: list[str] = []
        try:
            await child.initialize()
            if child._permission_manager is not None and child._permission_manager.mode in {
                PermissionMode.DEFAULT,
                PermissionMode.AI_REVIEW,
            }:
                # A scheduler has no human response queue.  Preserve explicit
                # allow/deny rules but turn an unanswered prompt into a deny.
                child._permission_manager.mode = PermissionMode.DONT_ASK
            if job.session_id:
                await child.resume(job.session_id)
            async for event in child.send_message(job.prompt):
                if isinstance(event, StreamTextEvent):
                    output.append(event.text)
                elif isinstance(event, ErrorEvent):
                    errors.append(event.message)
                elif isinstance(event, TurnCompleteEvent):
                    usage = event.usage or {}
                    tokens = int(
                        usage.get("total_tokens")
                        or (
                            usage.get("input_tokens", 0)
                            + usage.get("output_tokens", 0)
                        )
                        or tokens
                    )
            if errors:
                raise RuntimeError("; ".join(errors[-3:]))
            return ScheduleExecutionResult(
                summary="".join(output).strip()[-4000:],
                tokens_used=tokens,
                session_id=child.session_id or None,
            )
        finally:
            await child.close()


__all__ = ["ScheduleExecutionResult", "ScheduleManager"]
