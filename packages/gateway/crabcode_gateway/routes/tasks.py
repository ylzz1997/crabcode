"""Background monitor and managed-agent task routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from crabcode_gateway.schemas import BackgroundTaskInfo, TaskStopRequest
from crabcode_gateway.safe_file import read_regular_file_tail
from crabcode_gateway.task_registry import SessionOperationRejected, run_session_operation


router = APIRouter(tags=["tasks"])


def _task_dicts(session: Any) -> list[dict[str, Any]]:
    """Return one stable representation for monitors and managed agents."""
    result: list[dict[str, Any]] = []
    for snapshot in session.list_monitor_tasks():
        data = snapshot.to_dict() if hasattr(snapshot, "to_dict") else dict(snapshot)
        result.append(data)
    for snapshot in session.list_agents():
        result.append(
            {
                "task_id": snapshot.agent_id,
                "agent_id": snapshot.agent_id,
                "session_id": snapshot.session_id,
                "description": snapshot.title,
                "task_type": "local_agent",
                "source": "agent",
                "status": snapshot.status,
                "output_file": snapshot.transcript_path,
                "created_at": snapshot.created_at,
                "started_at": snapshot.started_at,
                "finished_at": snapshot.finished_at,
                "updated_at": snapshot.updated_at,
                "error": snapshot.error,
            }
        )
    return result


def _task_info(data: dict[str, Any]) -> BackgroundTaskInfo:
    return BackgroundTaskInfo(
        task_id=str(data.get("task_id") or data.get("agent_id") or ""),
        agent_id=data.get("agent_id"),
        session_id=str(data.get("session_id") or ""),
        description=str(data.get("description") or ""),
        task_type=str(data.get("task_type") or ""),
        source=str(data.get("source") or ""),
        status=str(data.get("status") or ""),
        output_file=data.get("output_file"),
        created_at=str(data.get("created_at") or ""),
        started_at=data.get("started_at"),
        finished_at=data.get("finished_at"),
        updated_at=str(data.get("updated_at") or ""),
        error=str(data.get("error") or ""),
        exit_code=data.get("exit_code"),
    )


def _task_matches(session: Any, selector: str) -> bool:
    value = str(selector or "").strip()
    if not value:
        return False
    return any(
        item.get("task_id", "") == value
        or str(item.get("task_id", "")).startswith(value)
        for item in _task_dicts(session)
    )


def _get_session(request: Request, session_id: str | None = None):
    sessions: dict[str, Any] = request.app.state.sessions
    closing = getattr(request.app.state, "closing_sessions", set())

    def usable(session: Any) -> bool:
        return session is not None and getattr(session, "session_id", None) not in closing

    if session_id is not None:
        session = sessions.get(session_id)
        return session if usable(session) else None

    default = sessions.get(getattr(request.app.state, "default_session_id", None))
    return default if usable(default) else None


async def _get_task_session(
    request: Request,
    session_id: str | None,
    task_id: str,
):
    """Resolve cross-session task selectors without inspecting cold sessions."""
    if session_id is not None:
        return _get_session(request, session_id)

    sessions: dict[str, Any] = request.app.state.sessions
    closing = getattr(request.app.state, "closing_sessions", set())
    default = _get_session(request)
    candidates = ([default] if default is not None else []) + [
        session
        for session in sessions.values()
        if session is not default
        and getattr(session, "session_id", None) not in closing
    ]

    async def _matches(candidate: Any) -> bool:
        initializer = getattr(candidate, "initialize", None)
        if callable(initializer):
            await initializer()
        return _task_matches(candidate, task_id)

    for candidate in candidates:
        try:
            if await run_session_operation(request.app.state, candidate, lambda: _matches(candidate)):
                return candidate
        except SessionOperationRejected:
            continue
    # Keep historical default-session behavior so callers get a task 404 rather
    # than a misleading session 404 when the selector simply does not exist.
    return default


def _matching_tasks(session: Any, task_id: str) -> list[dict[str, Any]]:
    tasks = _task_dicts(session)
    matches = [item for item in tasks if item.get("task_id") == task_id]
    if not matches:
        matches = [
            item for item in tasks
            if str(item.get("task_id") or "").startswith(task_id)
        ]
    return matches


@router.get("/tasks", response_model=list[BackgroundTaskInfo])
@router.get("/tasks/list", response_model=list[BackgroundTaskInfo])
@router.get("/task/list", response_model=list[BackgroundTaskInfo])
async def list_tasks(request: Request, session_id: str | None = None) -> list[BackgroundTaskInfo]:
    session = _get_session(request, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    async def _list() -> list[BackgroundTaskInfo]:
        initializer = getattr(session, "initialize", None)
        if callable(initializer):
            await initializer()
        return [_task_info(item) for item in _task_dicts(session)]

    try:
        return await run_session_operation(request.app.state, session, _list)
    except SessionOperationRejected as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/tasks/{task_id}", response_model=BackgroundTaskInfo)
async def get_task(task_id: str, request: Request, session_id: str | None = None) -> BackgroundTaskInfo:
    session = await _get_task_session(request, session_id, task_id)
    if not session:
        raise HTTPException(status_code=404, detail="Task not found")

    async def _get() -> list[dict[str, Any]]:
        initializer = getattr(session, "initialize", None)
        if callable(initializer):
            await initializer()
        return _matching_tasks(session, task_id)

    try:
        matches = await run_session_operation(request.app.state, session, _get)
    except SessionOperationRejected as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if len(matches) != 1:
        raise HTTPException(status_code=404, detail="Task not found or selector is ambiguous")
    return _task_info(matches[0])


@router.post("/tasks/stop")
@router.post("/task/stop")
async def stop_task(req: TaskStopRequest, request: Request) -> dict[str, Any]:
    session = await _get_task_session(request, req.session_id, req.task_id)
    if not session:
        raise HTTPException(status_code=404, detail="Task not found")
    try:
        stopped = await run_session_operation(
            request.app.state,
            session,
            lambda: session.stop_background_task(req.task_id),
        )
    except SessionOperationRejected as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not stopped:
        raise HTTPException(status_code=404, detail=f"No running background task: {req.task_id}")
    return {"status": "ok", "task_id": req.task_id, "stopped": True}


@router.get("/tasks/{task_id}/output")
async def task_output(task_id: str, request: Request, session_id: str | None = None) -> dict[str, Any]:
    """Read a bounded tail of a monitor/agent output file."""
    session = await _get_task_session(request, session_id, task_id)
    if not session:
        raise HTTPException(status_code=404, detail="Task not found")
    try:
        limit = max(1, min(10_000, int(request.query_params.get("lines", "200"))))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="lines must be an integer")
    async def _read_output() -> dict[str, Any] | None:
        initializer = getattr(session, "initialize", None)
        if callable(initializer):
            await initializer()
        matches = _matching_tasks(session, task_id)
        if len(matches) != 1:
            return None
        path_value = matches[0].get("output_file")
        if not path_value:
            return {
                "task_id": matches[0].get("task_id"),
                "path": None,
                "lines": [],
                "truncated": False,
            }
        task = matches[0]
        resolved_task_id = str(task.get("task_id") or "")
        try:
            from crabcode_core.session.storage import (
                get_agent_transcript_path,
                get_task_output_path,
            )

            if task.get("agent_id") or task.get("source") == "agent":
                expected_path = get_agent_transcript_path(
                    session.cwd,
                    session.session_id,
                    str(task.get("agent_id") or resolved_task_id),
                )
            else:
                expected_path = get_task_output_path(
                    session.cwd,
                    session.session_id,
                    resolved_task_id,
                )
            output_lines, truncated = read_regular_file_tail(
                expected_path,
                max_lines=limit,
            )
        except (OSError, TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=404,
                detail=f"Task output unavailable: {exc}",
            ) from exc
        return {
            "task_id": resolved_task_id,
            "path": str(expected_path),
            "lines": output_lines[-limit:],
            "truncated": truncated,
        }

    try:
        result = await run_session_operation(
            request.app.state,
            session,
            _read_output,
        )
    except SessionOperationRejected as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="Task not found or selector is ambiguous")
    return result
