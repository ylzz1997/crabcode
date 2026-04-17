"""Permission and choice interaction routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from crabcode_gateway.schemas import (
    ChoiceResponseRequest,
    PermissionResponseRequest,
)

router = APIRouter(tags=["interaction"])


def _get_session(request: Request, session_id: str | None = None):
    sessions: dict = request.app.state.sessions
    sid = session_id or request.app.state.default_session_id
    if not sid or sid not in sessions:
        return None
    return sessions[sid]


@router.post("/permission/respond")
async def respond_permission(req: PermissionResponseRequest, request: Request):
    """Respond to a permission request from the agent."""
    from crabcode_core.types.event import PermissionResponseEvent

    session = _get_session(request)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    event = PermissionResponseEvent(
        tool_use_id=req.tool_use_id,
        allowed=req.allowed,
        always_allow=req.always_allow,
        agent_id=req.agent_id,
    )
    await session.respond_permission(event)
    return {"status": "ok"}


@router.post("/choice/respond")
async def respond_choice(req: ChoiceResponseRequest, request: Request):
    """Respond to a choice request from the agent."""
    from crabcode_core.types.event import ChoiceResponseEvent

    session = _get_session(request)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    event = ChoiceResponseEvent(
        tool_use_id=req.tool_use_id,
        selected=req.selected,
        cancelled=req.cancelled,
        agent_id=req.agent_id,
    )
    await session.respond_choice(event)
    return {"status": "ok"}
