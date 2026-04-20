"""Schedule tools — create, list, cancel, and inspect scheduled jobs.

These tools allow the agent to manage time-based scheduled tasks
(cron, interval, one-shot) through the ScheduleManager.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from crabcode_core.schedule.models import JobStatus, ScheduleType
from crabcode_core.types.tool import Tool, ToolContext, ToolResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_SCHEDULE_TYPES = {"cron", "interval", "once"}


def _format_job_brief(job: dict[str, Any]) -> str:
    """Format a single job dict into a one-line summary for list output."""
    job_id = job.get("id", "?")
    short_id = job_id[:8] if len(job_id) > 8 else job_id
    name = job.get("name", "(unnamed)")
    stype = job.get("schedule_type", "?")
    schedule = job.get("schedule", "?")
    status = job.get("status", "?")
    run_count = job.get("run_count", 0)
    max_runs = job.get("max_runs")
    next_run = job.get("next_run")
    runs_str = f"{run_count}/{max_runs}" if max_runs else str(run_count)
    next_str = next_run[:19] if next_run else "—"
    return f"  [{short_id}] {name} | {stype}={schedule} | status={status} | runs={runs_str} | next={next_str}"


def _format_job_detail(job: dict[str, Any]) -> str:
    """Format a full job detail for status output."""
    lines = [
        f"Job ID:        {job.get('id', '?')}",
        f"Name:          {job.get('name', '(unnamed)')}",
        f"Prompt:        {job.get('prompt', '')[:200]}",
        f"Schedule:      {job.get('schedule_type', '?')} — {job.get('schedule', '?')}",
        f"CWD:           {job.get('cwd') or '(default)'}",
        f"Status:        {job.get('status', '?')}",
        f"Enabled:       {job.get('enabled', True)}",
        f"Run count:     {job.get('run_count', 0)}",
        f"Max runs:      {job.get('max_runs') or 'unlimited'}",
        f"Last run:      {job.get('last_run') or '—'}",
        f"Next run:      {job.get('next_run') or '—'}",
        f"Created at:    {job.get('created_at', '?')}",
        f"Timeout:       {job.get('timeout') or '(default)'}",
        f"Model profile: {job.get('model_profile') or '(default)'}",
    ]
    if job.get("tags"):
        lines.append(f"Tags:          {', '.join(job['tags'])}")
    if job.get("description"):
        lines.append(f"Description:   {job['description']}")
    return "\n".join(lines)


def _format_run_brief(run: dict[str, Any]) -> str:
    """Format a single run dict into a one-line summary."""
    run_id = run.get("id", "?")
    short_id = run_id[:8] if len(run_id) > 8 else run_id
    status = run.get("status", "?")
    started = run.get("started_at")
    finished = run.get("finished_at")
    duration = run.get("duration_seconds")
    started_str = started[:19] if started else "—"
    dur_str = f"{duration:.1f}s" if duration is not None else "—"
    error = run.get("error_message", "")
    err_str = f" | error: {error[:80]}" if error else ""
    summary = run.get("result_summary", "")
    sum_str = f" | {summary[:80]}" if summary else ""
    return f"  [{short_id}] {status} | started={started_str} | duration={dur_str}{sum_str}{err_str}"


def _compute_next_run(schedule: str, schedule_type: str) -> str | None:
    """Best-effort computation of the next run timestamp.

    For 'once' type, the schedule itself is the timestamp.
    For 'interval', we add the interval to now.
    For 'cron', we return None (requires a real cron parser).
    """
    now = datetime.now(timezone.utc)

    if schedule_type == "once":
        # Validate that the schedule looks like a timestamp
        try:
            datetime.fromisoformat(schedule)
            return schedule
        except (ValueError, TypeError):
            return None

    if schedule_type == "interval":
        try:
            seconds = _parse_interval(schedule)
            from datetime import timedelta

            next_dt = now + timedelta(seconds=seconds)
            return next_dt.isoformat()
        except (ValueError, TypeError):
            return None

    # cron — needs a proper parser, return None
    return None


def _parse_interval(schedule: str) -> int:
    """Parse an interval string (e.g. '30m', '2h', '1d') into seconds.

    Supports: s (seconds), m (minutes), h (hours), d (days).
    Also accepts plain integer strings (interpreted as seconds).
    """
    schedule = schedule.strip()

    # Plain integer
    try:
        return int(schedule)
    except ValueError:
        pass

    if not schedule:
        raise ValueError("Empty interval")

    unit = schedule[-1].lower()
    value_str = schedule[:-1]

    try:
        value = int(value_str)
    except ValueError:
        raise ValueError(f"Invalid interval value: {value_str}")

    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if unit not in multipliers:
        raise ValueError(f"Unknown interval unit: {unit}")

    return value * multipliers[unit]


# ---------------------------------------------------------------------------
# ScheduleCreateTool
# ---------------------------------------------------------------------------


class ScheduleCreateTool(Tool):
    name = "ScheduleCreate"
    description = "Create a new scheduled task."
    is_read_only = False
    is_concurrency_safe = True
    input_schema = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "minLength": 1,
                "description": "Human-readable name for the scheduled task.",
            },
            "prompt": {
                "type": "string",
                "minLength": 1,
                "description": "The prompt to execute when the task fires.",
            },
            "schedule": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Schedule definition. For cron: a cron expression (e.g. '0 */6 * * *'). "
                    "For interval: seconds as integer or shorthand like '30m', '2h', '1d'. "
                    "For once: an ISO 8601 timestamp (e.g. '2025-03-01T09:00:00Z')."
                ),
            },
            "schedule_type": {
                "type": "string",
                "enum": ["cron", "interval", "once"],
                "description": (
                    "Type of schedule: "
                    "'cron' = recurring via cron expression, "
                    "'interval' = recurring at fixed seconds, "
                    "'once' = one-shot at a specific timestamp."
                ),
            },
            "cwd": {
                "type": "string",
                "description": "Working directory for execution (default: current session cwd).",
            },
            "max_runs": {
                "type": "integer",
                "description": "Maximum number of executions (default: unlimited).",
            },
            "description": {
                "type": "string",
                "description": "Optional longer description of the job.",
            },
            "timeout": {
                "type": "integer",
                "description": "Per-job timeout in seconds.",
            },
            "model_profile": {
                "type": "string",
                "description": "Model profile to use for this job's agent.",
            },
        },
        "required": ["name", "prompt", "schedule", "schedule_type"],
    }

    async def get_prompt(self, **kwargs: Any) -> str:
        return (
            "Create a new scheduled task that will be executed automatically "
            "based on a time-based schedule.\n\n"
            "Schedule types:\n"
            "- 'cron': Recurring schedule using a cron expression "
            "(e.g. '0 */6 * * *' for every 6 hours).\n"
            "- 'interval': Recurring at a fixed interval. "
            "Provide seconds or shorthand like '30m', '2h', '1d'.\n"
            "- 'once': One-shot execution at a specific ISO 8601 timestamp "
            "(e.g. '2025-03-01T09:00:00Z').\n\n"
            "Parameters:\n"
            "- name (required): A human-readable name for the task.\n"
            "- prompt (required): The prompt to send to the agent when the task fires.\n"
            "- schedule (required): The schedule definition (format depends on schedule_type).\n"
            "- schedule_type (required): One of 'cron', 'interval', or 'once'.\n"
            "- cwd (optional): Working directory (defaults to current session cwd).\n"
            "- max_runs (optional): Maximum number of times the task should run.\n\n"
            "Use this tool when you need to set up recurring or delayed tasks, "
            "such as periodic checks, scheduled builds, or reminders."
        )

    async def validate_input(self, tool_input: dict[str, Any]) -> str | None:
        schedule_type = tool_input.get("schedule_type")
        if schedule_type not in _VALID_SCHEDULE_TYPES:
            return f"schedule_type must be one of: {', '.join(sorted(_VALID_SCHEDULE_TYPES))}"
        if not tool_input.get("name"):
            return "name is required"
        if not tool_input.get("prompt"):
            return "prompt is required"
        if not tool_input.get("schedule"):
            return "schedule is required"

        # Validate interval format
        if schedule_type == "interval":
            try:
                _parse_interval(tool_input["schedule"])
            except (ValueError, TypeError) as e:
                return f"Invalid interval format: {e}"

        # Validate once timestamp
        if schedule_type == "once":
            try:
                ts = datetime.fromisoformat(tool_input["schedule"])
                if ts.tzinfo is None:
                    return "once schedule must include timezone info (e.g. '2025-03-01T09:00:00Z')"
            except (ValueError, TypeError) as e:
                return f"Invalid ISO timestamp for once schedule: {e}"

        return None

    async def call(
        self,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        manager = context.schedule_manager
        if not manager:
            return ToolResult(
                result_for_model="Error: schedule manager unavailable",
                is_error=True,
            )

        schedule_type = tool_input["schedule_type"]
        schedule = tool_input["schedule"]
        name = tool_input["name"]
        prompt = tool_input["prompt"]
        cwd = tool_input.get("cwd") or context.cwd
        max_runs = tool_input.get("max_runs")

        # Compute next_run
        next_run = _compute_next_run(schedule, schedule_type)

        try:
            job = manager.create_job(
                name=name,
                prompt=prompt,
                schedule=schedule,
                schedule_type=ScheduleType(schedule_type),
                cwd=cwd,
                max_runs=max_runs,
                next_run=next_run,
                description=tool_input.get("description", ""),
                timeout=tool_input.get("timeout"),
                model_profile=tool_input.get("model_profile"),
            )
        except Exception as exc:
            return ToolResult(
                result_for_model=f"Error creating scheduled task: {exc}",
                is_error=True,
            )

        job_dict = job if isinstance(job, dict) else job.model_dump()
        detail = _format_job_detail(job_dict)
        return ToolResult(
            data=job_dict,
            result_for_model=f"Scheduled task created:\n{detail}",
        )


# ---------------------------------------------------------------------------
# ScheduleListTool
# ---------------------------------------------------------------------------


class ScheduleListTool(Tool):
    name = "ScheduleList"
    description = "List all scheduled tasks with a status summary."
    is_read_only = True
    is_concurrency_safe = True
    input_schema = {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "description": "Filter by job status (active, paused, completed, disabled, error).",
            },
            "schedule_type": {
                "type": "string",
                "enum": ["cron", "interval", "once"],
                "description": "Filter by schedule type.",
            },
        },
    }

    async def get_prompt(self, **kwargs: Any) -> str:
        return (
            "List all scheduled tasks and their status summaries.\n\n"
            "Returns a compact overview of each job including its name, "
            "schedule type, status, run count, and next execution time.\n\n"
            "You can optionally filter by status or schedule type.\n\n"
            "Use this to review what tasks are currently scheduled, "
            "check if any jobs have failed, or see upcoming executions."
        )

    async def call(
        self,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        manager = context.schedule_manager
        if not manager:
            return ToolResult(
                result_for_model="Error: schedule manager unavailable",
                is_error=True,
            )

        status_filter = tool_input.get("status")
        type_filter = tool_input.get("schedule_type")

        try:
            jobs = manager.list_jobs(
                status=status_filter,
                schedule_type=type_filter,
            )
        except Exception as exc:
            return ToolResult(
                result_for_model=f"Error listing scheduled tasks: {exc}",
                is_error=True,
            )

        if not jobs:
            return ToolResult(
                data={"count": 0, "jobs": []},
                result_for_model="No scheduled tasks found.",
            )

        # Normalize to dicts
        job_dicts = [
            j if isinstance(j, dict) else j.model_dump()
            for j in jobs
        ]

        # Summary statistics
        status_counts: dict[str, int] = {}
        for j in job_dicts:
            s = j.get("status", "unknown")
            status_counts[s] = status_counts.get(s, 0) + 1

        summary_parts = [f"{count} {s}" for s, count in sorted(status_counts.items())]
        summary = ", ".join(summary_parts)

        lines = [f"Scheduled tasks ({summary}):"]
        for j in job_dicts:
            lines.append(_format_job_brief(j))

        return ToolResult(
            data={"count": len(job_dicts), "jobs": job_dicts},
            result_for_model="\n".join(lines),
        )


# ---------------------------------------------------------------------------
# ScheduleCancelTool
# ---------------------------------------------------------------------------


class ScheduleCancelTool(Tool):
    name = "ScheduleCancel"
    description = "Cancel and delete a scheduled task."
    is_read_only = False
    is_concurrency_safe = True
    input_schema = {
        "type": "object",
        "properties": {
            "job_id": {
                "type": "string",
                "minLength": 1,
                "description": "The ID of the scheduled task to cancel/delete.",
            },
        },
        "required": ["job_id"],
    }

    async def get_prompt(self, **kwargs: Any) -> str:
        return (
            "Cancel (delete) a scheduled task by its ID.\n\n"
            "This permanently removes the task and all its execution history. "
            "The task will not fire again after cancellation.\n\n"
            "Use ScheduleList to find the job ID, then use this tool to cancel it."
        )

    async def call(
        self,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        manager = context.schedule_manager
        if not manager:
            return ToolResult(
                result_for_model="Error: schedule manager unavailable",
                is_error=True,
            )

        job_id = tool_input.get("job_id", "")
        if not job_id:
            return ToolResult(
                result_for_model="Error: job_id is required",
                is_error=True,
            )

        # Fetch current job info for the response message
        try:
            job = manager.get_job(job_id)
        except Exception as exc:
            return ToolResult(
                result_for_model=f"Error fetching job: {exc}",
                is_error=True,
            )

        if not job:
            return ToolResult(
                result_for_model=f"Error: scheduled task '{job_id}' not found.",
                is_error=True,
            )

        job_dict = job if isinstance(job, dict) else job.model_dump()
        job_name = job_dict.get("name", "(unnamed)")

        try:
            manager.cancel_job(job_id)
        except Exception as exc:
            return ToolResult(
                result_for_model=f"Error cancelling scheduled task: {exc}",
                is_error=True,
            )

        return ToolResult(
            data={"job_id": job_id, "cancelled": True},
            result_for_model=f"Scheduled task cancelled and deleted: {job_name} (id: {job_id[:8]})",
        )


# ---------------------------------------------------------------------------
# ScheduleStatusTool
# ---------------------------------------------------------------------------


class ScheduleStatusTool(Tool):
    name = "ScheduleStatus"
    description = "View details and recent execution history of a scheduled task."
    is_read_only = True
    is_concurrency_safe = True
    input_schema = {
        "type": "object",
        "properties": {
            "job_id": {
                "type": "string",
                "minLength": 1,
                "description": "The ID of the scheduled task to inspect.",
            },
            "run_limit": {
                "type": "integer",
                "description": "Maximum number of recent runs to show (default: 10).",
            },
        },
        "required": ["job_id"],
    }

    async def get_prompt(self, **kwargs: Any) -> str:
        return (
            "View detailed information about a single scheduled task, "
            "including its configuration and recent execution history.\n\n"
            "Shows:\n"
            "- Full job details (name, prompt, schedule, status, etc.)\n"
            "- Recent run history with status, timing, and any error messages\n\n"
            "Use this to diagnose issues with a specific task, "
            "check when it last ran, or verify its configuration."
        )

    async def call(
        self,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        manager = context.schedule_manager
        if not manager:
            return ToolResult(
                result_for_model="Error: schedule manager unavailable",
                is_error=True,
            )

        job_id = tool_input.get("job_id", "")
        if not job_id:
            return ToolResult(
                result_for_model="Error: job_id is required",
                is_error=True,
            )

        run_limit = tool_input.get("run_limit", 10)

        # Fetch job
        try:
            job = manager.get_job(job_id)
        except Exception as exc:
            return ToolResult(
                result_for_model=f"Error fetching job: {exc}",
                is_error=True,
            )

        if not job:
            return ToolResult(
                result_for_model=f"Error: scheduled task '{job_id}' not found.",
                is_error=True,
            )

        job_dict = job if isinstance(job, dict) else job.model_dump()

        # Fetch recent runs
        try:
            runs = manager.list_runs(job_id, limit=run_limit)
        except Exception as exc:
            return ToolResult(
                result_for_model=f"Error fetching run history: {exc}",
                is_error=True,
            )

        # Build output
        lines = [_format_job_detail(job_dict)]

        # Normalize runs to dicts
        run_dicts = [
            r if isinstance(r, dict) else r.model_dump()
            for r in runs
        ]

        if run_dicts:
            lines.append("")
            lines.append(f"Recent runs (showing last {len(run_dicts)}):")
            for r in run_dicts:
                lines.append(_format_run_brief(r))
        else:
            lines.append("")
            lines.append("No execution history yet.")

        return ToolResult(
            data={"job": job_dict, "runs": run_dicts},
            result_for_model="\n".join(lines),
        )
