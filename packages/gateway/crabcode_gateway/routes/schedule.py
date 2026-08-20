"""Persistent schedule management routes for desktop and remote clients."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from inspect import isawaitable
import asyncio
import os
from datetime import datetime, timezone
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


async def _get_standalone_manager(request: Request):
    """Return a gateway-owned scheduler when no chat session is loaded.

    Listing schedules intentionally works without a live session. Mutations
    must do the same; otherwise the desktop automation deck can display a
    persistent job but cannot pause, run, or delete it until a chat WebSocket
    happens to be connected.
    """
    manager = getattr(request.app.state, "standalone_schedule_manager", None)
    if manager is not None:
        return manager
    lock = getattr(request.app.state, "standalone_schedule_manager_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        request.app.state.standalone_schedule_manager_lock = lock
    async with lock:
        manager = getattr(request.app.state, "standalone_schedule_manager", None)
        if manager is not None:
            return manager
        from crabcode_core.config.manager import ConfigManager
        from crabcode_core.schedule.manager import ScheduleManager

        cwd = os.getcwd()
        settings = ConfigManager(cwd=cwd).load().schedule
        manager = ScheduleManager(settings=settings, cwd=cwd, session_id="")
        try:
            await manager.start()
        except Exception:
            await manager.close()
            raise
        request.app.state.standalone_schedule_manager = manager
        return manager


async def _run_schedule_operation(
    request: Request,
    session_id: str | None,
    operation: Callable[[Any], T | Awaitable[T]],
    *,
    global_scope: bool = False,
) -> T:
    if getattr(request.app.state, "gateway_closing", False):
        raise HTTPException(status_code=503, detail="Gateway is shutting down")
    session = None if global_scope else _get_session(request, session_id)
    if not global_scope and session_id is not None and session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    if session is None:
        manager = await _get_standalone_manager(request)
    else:
        manager = None

    async def _owned() -> T:
        operation_manager = manager
        if session is not None:
            initializer = getattr(session, "initialize", None)
            if callable(initializer):
                await initializer()
            operation_manager = getattr(session, "_schedule_manager", None)
            if operation_manager is None:
                raise HTTPException(status_code=503, detail="Schedule manager is unavailable")
        result = operation(operation_manager)
        if isawaitable(result):
            return await result
        return result

    try:
        if session is None:
            return await _owned()
        return await run_session_operation(request.app.state, session, _owned)
    except SessionOperationRejected as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _job_info(job: Any) -> ScheduleJobInfo:
    data = (
        job.model_dump(mode="json")
        if hasattr(job, "model_dump")
        else dict(job)
    )
    claimed_until = data.get("claimed_until")
    running = False
    if claimed_until:
        try:
            running = datetime.fromisoformat(str(claimed_until)) > datetime.now(timezone.utc)
        except (TypeError, ValueError):
            pass
    return ScheduleJobInfo.model_validate({**data, "running": running})


@router.get("", response_model=list[ScheduleJobInfo])
@router.get("/list", response_model=list[ScheduleJobInfo])
async def list_schedules(
    request: Request,
    session_id: str | None = None,
    scope: str | None = None,
    status: str | None = None,
    schedule_type: str | None = None,
    enabled: bool | None = None,
    limit: int = 100,
) -> list[ScheduleJobInfo]:
    if scope == "global":
        # The automation deck is gateway-scoped, so its list must not change
        # when the user opens or closes an unrelated chat session.
        manager = await _get_standalone_manager(request)
        rows = manager.store.list_schedules(
            status=status,
            schedule_type=schedule_type,
            enabled=enabled,
            limit=max(1, min(int(limit), 1000)),
        )
        return [_job_info(row) for row in rows]
    if session_id is None and _get_session(request, None) is None:
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
        global_scope=req.scope == "global",
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
        global_scope=req.scope == "global",
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
        global_scope=req.scope == "global",
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
        global_scope=req.scope == "global",
    )
    if not started:
        raise HTTPException(
            status_code=409,
            detail="Schedule is missing, paused, or already running",
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
