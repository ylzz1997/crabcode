"""Snapshot and revert routes — /snapshot/*."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from crabcode_gateway.schemas import CheckpointRequest, RevertRequest

router = APIRouter(prefix="/snapshot", tags=["snapshot"])


def _get_session(request: Request, session_id: str):
    """Retrieve a CoreSession from app state."""
    sessions: dict = request.app.state.sessions
    if session_id not in sessions:
        return None
    return sessions[session_id]


@router.post("/checkpoint")
async def create_checkpoint(req: CheckpointRequest, request: Request) -> dict[str, Any]:
    """Create a checkpoint with a file-system snapshot."""
    session = _get_session(request, req.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    cp_id = session.checkpoint(label=req.label)
    if not cp_id:
        raise HTTPException(status_code=400, detail="Failed to create checkpoint")
    return {"checkpoint_id": cp_id, "snapshot_included": True}


@router.get("/list")
async def list_checkpoints(session_id: str, request: Request) -> list[dict[str, Any]]:
    """List checkpoints for a session, including file snapshot info."""
    session = _get_session(request, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session.list_checkpoints()


@router.post("/revert")
async def revert_checkpoint(req: RevertRequest, request: Request) -> dict[str, Any]:
    """Revert both files and conversation to a checkpoint."""
    session = _get_session(request, req.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    result = session.revert(req.checkpoint_id)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail="Revert failed or checkpoint not found")
    return result


@router.post("/rollback")
async def rollback_checkpoint(req: RevertRequest, request: Request) -> dict[str, Any]:
    """Rollback conversation only (no file restore) to a checkpoint."""
    session = _get_session(request, req.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    ok = session.rollback(req.checkpoint_id)
    if not ok:
        raise HTTPException(status_code=400, detail="Rollback failed or checkpoint not found")
    return {"success": True, "messages_count": len(session.messages)}
