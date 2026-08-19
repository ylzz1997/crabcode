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


def _resolve_checkpoint_id(checkpoints: list[dict[str, Any]], selector: str) -> str | None:
    """Resolve a full ID, unique prefix, or one-based checkpoint index."""
    value = selector.strip()
    if not value:
        return None
    try:
        index = int(value) - 1
    except ValueError:
        index = -1
    if 0 <= index < len(checkpoints):
        return str(checkpoints[index].get("id") or "") or None
    exact = [str(cp.get("id") or "") for cp in checkpoints if cp.get("id") == value]
    if exact:
        return exact[0]
    matches = [str(cp.get("id") or "") for cp in checkpoints if str(cp.get("id") or "").startswith(value)]
    return matches[0] if len(matches) == 1 else None


async def _resolve_and_revert(
    session: Any,
    selector: str,
) -> tuple[str, dict[str, Any]] | None:
    checkpoint_id = _resolve_checkpoint_id(session.list_checkpoints(), selector)
    if checkpoint_id is None:
        return None
    return checkpoint_id, session.revert(checkpoint_id)


async def _resolve_and_rollback(
    session: Any,
    selector: str,
) -> tuple[str, bool, int] | None:
    checkpoint_id = _resolve_checkpoint_id(session.list_checkpoints(), selector)
    if checkpoint_id is None:
        return None
    ok = bool(session.rollback(checkpoint_id))
    return checkpoint_id, ok, len(getattr(session, "messages", ()))


async def _undo_latest(session: Any) -> tuple[str, dict[str, Any]] | None:
    checkpoints = session.list_checkpoints()
    if not checkpoints:
        return None
    checkpoint_id = str(checkpoints[0].get("id") or "")
    if not checkpoint_id:
        return None
    return checkpoint_id, session.revert(checkpoint_id)


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
        resolved = await run_session_operation(
            request.app.state,
            session,
            lambda: _resolve_and_revert(session, req.checkpoint_id),
        )
    except SessionOperationRejected as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if resolved is None:
        raise HTTPException(status_code=400, detail="Checkpoint not found or prefix is ambiguous")
    _checkpoint_id, result = resolved
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
        resolved = await run_session_operation(
            request.app.state,
            session,
            lambda: _resolve_and_rollback(session, req.checkpoint_id),
        )
    except SessionOperationRejected as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if resolved is None:
        raise HTTPException(status_code=400, detail="Checkpoint not found or prefix is ambiguous")
    _checkpoint_id, ok, message_count = resolved
    if not ok:
        raise HTTPException(status_code=400, detail="Rollback failed or checkpoint not found")
    return {"success": True, "messages_count": message_count}


@router.post("/undo")
async def undo_last_checkpoint(request: Request) -> dict[str, Any]:
    """Revert files and conversation to the most recent checkpoint."""
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
        resolved = await run_session_operation(
            request.app.state,
            session,
            lambda: _undo_latest(session),
        )
    except SessionOperationRejected as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if resolved is None:
        raise HTTPException(status_code=400, detail="No checkpoints to undo")
    checkpoint_id, result = resolved
    if not result.get("success"):
        raise HTTPException(status_code=400, detail="Undo failed")
    return {"checkpoint_id": checkpoint_id, **result}
