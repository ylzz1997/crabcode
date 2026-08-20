"""Persistent schedule management routes for desktop and remote clients."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from inspect import isawaitable
from typing import Any, TypeVar

from fastapi import APIRouter, HTTPException, Request

from crabcode_gateway.schemas import (
    ScheduleCreateRequest,
    ScheduleJobInfo,
    ScheduleJobRequest,
    ScheduleRunInfo,
)
from crabcode_gateway.task_registry import SessionOperationRejected, run_session_operation


router = APIRouter(prefix="/schedule", tags=["schedule"])
T = TypeVar("T")


def _get_session(request: Request, session_id: str | None):
    sessions: dict[str, Any] = request.app.state.sessions
    sid = request.app.state.default_session_id if session_id is None else session_id
    session = sessions.get(sid) if sid else None
    if session is None or getattr(session, "session_id", None) in getattr(
        request.app.state,
        "closing_sessions",
        set(),
    ):
        return None
    return session


async def _run_schedule_operation(
    request: Request,
    session_id: str | None,
    operation: Callable[[Any], T | Awaitable[T]],
) -> T:
    session = _get_session(request, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    async def _owned() -> T:
        initializer = getattr(session, "initialize", None)
        if callable(initializer):
            await initializer()
        manager = getattr(session, "_schedule_manager", None)
        if manager is None:
            raise HTTPException(status_code=503, detail="Schedule manager is unavailable")
        result = operation(manager)
        if isawaitable(result):
            return await result
        return result

    try:
        return await run_session_operation(request.app.state, session, _owned)
    except SessionOperationRejected as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _job_info(job: Any) -> ScheduleJobInfo:
    data = (
        job.model_dump(mode="json")
        if hasattr(job, "model_dump")
        else dict(job)
    )
    return ScheduleJobInfo.model_validate(data)


@router.get("", response_model=list[ScheduleJobInfo])
@router.get("/list", response_model=list[ScheduleJobInfo])
async def list_schedules(
    request: Request,
    session_id: str | None = None,
    status: str | None = None,
    schedule_type: str | None = None,
    enabled: bool | None = None,
    limit: int = 100,
) -> list[ScheduleJobInfo]:
    if session_id is None and _get_session(request, None) is None:
        # The desktop workbench can open the schedule view before a chat
        # session exists. Read the shared persistent store directly in that
        # case; mutations still use a live session manager below.
        from crabcode_core.schedule.store import ScheduleStore

        store = ScheduleStore()
        try:
            rows = store.list_schedules(
                status=status,
                schedule_type=schedule_type,
                enabled=enabled,
                limit=max(1, min(int(limit), 1000)),
            )
        finally:
            store.close()
        return [_job_info(row) for row in rows]
    jobs = await _run_schedule_operation(
        request,
        session_id,
        lambda manager: manager.list_jobs(
            status=status,
            schedule_type=schedule_type,
            enabled=enabled,
            limit=limit,
        ),
    )
    return [_job_info(job) for job in jobs]


@router.post("/create", response_model=ScheduleJobInfo)
async def create_schedule(
    req: ScheduleCreateRequest,
    request: Request,
) -> ScheduleJobInfo:
    try:
        job = await _run_schedule_operation(
            request,
            req.session_id,
            lambda manager: manager.create_job(
                name=req.name,
                prompt=req.prompt,
                schedule=req.schedule,
                schedule_type=req.schedule_type,
                cwd=req.cwd,
                enabled=req.enabled,
                max_runs=req.max_runs,
                next_run=req.next_run,
                description=req.description,
                tags=req.tags,
                timeout=req.timeout,
                model_profile=req.model_profile,
                session_id=req.job_session_id,
                extra=req.extra,
            ),
        )
    except (RuntimeError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _job_info(job)


@router.post("/cancel")
async def cancel_schedule(req: ScheduleJobRequest, request: Request) -> dict[str, Any]:
    cancelled = await _run_schedule_operation(
        request,
        req.session_id,
        lambda manager: manager.cancel_job(req.job_id),
    )
    if not cancelled:
        raise HTTPException(status_code=404, detail="Schedule not found or selector is ambiguous")
    return {"job_id": req.job_id, "cancelled": True}


@router.post("/pause", response_model=ScheduleJobInfo)
async def pause_schedule(req: ScheduleJobRequest, request: Request) -> ScheduleJobInfo:
    job = await _run_schedule_operation(
        request,
        req.session_id,
        lambda manager: manager.pause_job(req.job_id),
    )
    if job is None:
        raise HTTPException(status_code=404, detail="Schedule not found or selector is ambiguous")
    return _job_info(job)


@router.post("/resume", response_model=ScheduleJobInfo)
async def resume_schedule(req: ScheduleJobRequest, request: Request) -> ScheduleJobInfo:
    job = await _run_schedule_operation(
        request,
        req.session_id,
        lambda manager: manager.resume_job(req.job_id),
    )
    if job is None:
        raise HTTPException(status_code=404, detail="Schedule not found or selector is ambiguous")
    return _job_info(job)


@router.post("/trigger")
async def trigger_schedule(req: ScheduleJobRequest, request: Request) -> dict[str, Any]:
    started = await _run_schedule_operation(
        request,
        req.session_id,
        lambda manager: manager.trigger_job(req.job_id),
    )
    if not started:
        raise HTTPException(
            status_code=409,
            detail="Schedule is missing, disabled, completed, or already running",
        )
    return {"job_id": req.job_id, "started": True}


@router.get("/{job_id}/runs", response_model=list[ScheduleRunInfo])
async def list_schedule_runs(
    job_id: str,
    request: Request,
    session_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[ScheduleRunInfo]:
    result = await _run_schedule_operation(
        request,
        session_id,
        lambda manager: (
            manager.get_job(job_id),
            manager.list_runs(job_id, status=status, limit=limit),
        ),
    )
    job, runs = result
    if job is None:
        raise HTTPException(status_code=404, detail="Schedule not found or selector is ambiguous")
    return [ScheduleRunInfo.model_validate(run) for run in runs]


@router.get("/{job_id}", response_model=ScheduleJobInfo)
async def get_schedule(
    job_id: str,
    request: Request,
    session_id: str | None = None,
) -> ScheduleJobInfo:
    job = await _run_schedule_operation(
        request,
        session_id,
        lambda manager: manager.get_job(job_id),
    )
    if job is None:
        raise HTTPException(status_code=404, detail="Schedule not found or selector is ambiguous")
    return _job_info(job)
