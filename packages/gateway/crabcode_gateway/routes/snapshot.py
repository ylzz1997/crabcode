"""Snapshot and revert routes — /snapshot/*."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from crabcode_gateway.schemas import CheckpointRequest, RevertRequest
from crabcode_gateway.task_registry import SessionOperationRejected, run_session_operation

router = APIRouter(prefix="/snapshot", tags=["snapshot"])


async def _checkpoint(session: Any, label: str) -> str | None:
    return session.checkpoint(label=label)


async def _list_checkpoints(session: Any) -> list[dict[str, Any]]:
    return session.list_checkpoints()


async def _revert(session: Any, checkpoint_id: str) -> dict[str, Any]:
    return session.revert(checkpoint_id)


async def _rollback(session: Any, checkpoint_id: str) -> bool:
    return bool(session.rollback(checkpoint_id))


async def _rollback_with_count(session: Any, checkpoint_id: str) -> tuple[bool, int]:
    """Commit rollback and capture its response count under the same lease."""
    ok = bool(session.rollback(checkpoint_id))
    return ok, len(getattr(session, "messages", ()))


def _get_session(request: Request, session_id: str):
    """Retrieve a CoreSession from app state."""
    sessions: dict = request.app.state.sessions
    if session_id is None or session_id not in sessions:
        return None
    if session_id in getattr(request.app.state, "closing_sessions", set()):
        return None
    return sessions[session_id]


@router.post("/checkpoint")
async def create_checkpoint(req: CheckpointRequest, request: Request) -> dict[str, Any]:
    """Create a checkpoint with a file-system snapshot."""
    session = _get_session(request, req.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        cp_id = await run_session_operation(
            request.app.state,
            session,
            lambda: _checkpoint(session, req.label),
        )
    except SessionOperationRejected as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not cp_id:
        raise HTTPException(status_code=400, detail="Failed to create checkpoint")
    return {"checkpoint_id": cp_id, "snapshot_included": True}


@router.get("/list")
async def list_checkpoints(session_id: str, request: Request) -> list[dict[str, Any]]:
    """List checkpoints for a session, including file snapshot info."""
    session = _get_session(request, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        return await run_session_operation(
            request.app.state,
            session,
            lambda: _list_checkpoints(session),
        )
    except SessionOperationRejected as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/revert")
async def revert_checkpoint(req: RevertRequest, request: Request) -> dict[str, Any]:
    """Revert both files and conversation to a checkpoint."""
    session = _get_session(request, req.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        result = await run_session_operation(
            request.app.state,
            session,
            lambda: _revert(session, req.checkpoint_id),
        )
    except SessionOperationRejected as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not result.get("success"):
        raise HTTPException(status_code=400, detail="Revert failed or checkpoint not found")
    return result


@router.post("/rollback")
async def rollback_checkpoint(req: RevertRequest, request: Request) -> dict[str, Any]:
    """Rollback conversation only (no file restore) to a checkpoint."""
    session = _get_session(request, req.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        ok, message_count = await run_session_operation(
            request.app.state,
            session,
            lambda: _rollback_with_count(session, req.checkpoint_id),
        )
    except SessionOperationRejected as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not ok:
        raise HTTPException(status_code=400, detail="Rollback failed or checkpoint not found")
    return {"success": True, "messages_count": message_count}


@router.post("/undo")
async def undo_last_checkpoint(request: Request) -> dict[str, Any]:
    """Revert the most recent checkpoint (rollback conversation only)."""
    try:
        body = await request.json()
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="Request body must be valid JSON") from exc
    if body is None:
        body = {}
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")
    session_id = body.get("session_id") if body else None

    sessions: dict = request.app.state.sessions
    sid = (
        request.app.state.default_session_id
        if session_id is None
        else session_id
    )
    if (
        not sid
        or sid not in sessions
        or sid in getattr(request.app.state, "closing_sessions", set())
    ):
        raise HTTPException(status_code=404, detail="Session not found")
    session = sessions[sid]

    try:
        checkpoints = await run_session_operation(
            request.app.state,
            session,
            lambda: _list_checkpoints(session),
        )
    except SessionOperationRejected as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not checkpoints:
        raise HTTPException(status_code=400, detail="No checkpoints to undo")

    latest = checkpoints[0]
    try:
        ok, message_count = await run_session_operation(
            request.app.state,
            session,
            lambda: _rollback_with_count(session, latest["id"]),
        )
    except SessionOperationRejected as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not ok:
        raise HTTPException(status_code=400, detail="Undo failed")
    return {"success": True, "checkpoint_id": latest["id"], "messages_count": message_count}
