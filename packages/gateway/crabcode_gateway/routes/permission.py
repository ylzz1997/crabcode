"""Permission and choice interaction routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from crabcode_gateway.schemas import (
    ChoiceResponseRequest,
    PermissionResponseRequest,
)
from crabcode_gateway.task_registry import SessionOperationRejected, run_session_operation

router = APIRouter(tags=["interaction"])


def _get_session(
    request: Request,
    session_id: str | None = None,
    *,
    agent_id: str | None = None,
):
    sessions: dict = request.app.state.sessions
    closing = getattr(request.app.state, "closing_sessions", set())

    # Preserve an explicit empty selector as invalid; ``None`` alone means
    # that the legacy default/agent-owner lookup may be used.
    requested = session_id if session_id is not None else None

    def _owns(candidate: object | None) -> bool:
        if candidate is None:
            return False
        if getattr(candidate, "session_id", None) in closing:
            return False
        if not agent_id:
            return True
        try:
            return bool(candidate.get_agent(agent_id))  # type: ignore[attr-defined]
        except Exception:
            return False

    # An explicit session selector must not silently fall back to the default
    # session when the agent belongs elsewhere.
    if requested is not None:
        selected = sessions.get(requested)
        return selected if _owns(selected) else None

    default_id = request.app.state.default_session_id
    selected = sessions.get(default_id) if default_id else None
    if _owns(selected):
        return selected

    if agent_id:
        for candidate in sessions.values():
            if _owns(candidate):
                return candidate
    return None


@router.post("/permission/respond")
async def respond_permission(req: PermissionResponseRequest, request: Request):
    """Respond to a permission request from the agent."""
    from crabcode_core.types.event import PermissionResponseEvent

    session = _get_session(
        request,
        req.session_id
        if req.session_id is not None
        else request.query_params.get("session_id"),
        agent_id=req.agent_id,
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    event = PermissionResponseEvent(
        tool_use_id=req.tool_use_id,
        allowed=req.allowed,
        always_allow=req.always_allow,
        agent_id=req.agent_id,
        feedback=req.feedback,
    )
    try:
        await run_session_operation(
            request.app.state,
            session,
            lambda: session.respond_permission(event),
        )
    except SessionOperationRejected as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"status": "ok"}


@router.post("/choice/respond")
async def respond_choice(req: ChoiceResponseRequest, request: Request):
    """Respond to a choice request from the agent."""
    from crabcode_core.types.event import ChoiceResponseEvent

    session = _get_session(
        request,
        req.session_id
        if req.session_id is not None
        else request.query_params.get("session_id"),
        agent_id=req.agent_id,
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    event = ChoiceResponseEvent(
        tool_use_id=req.tool_use_id,
        selected=req.selected,
        cancelled=req.cancelled,
        agent_id=req.agent_id,
    )
    try:
        await run_session_operation(
            request.app.state,
            session,
            lambda: session.respond_choice(event),
        )
    except SessionOperationRejected as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"status": "ok"}
