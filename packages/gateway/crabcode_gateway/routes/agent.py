"""Agent management routes — /agent/*."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from crabcode_gateway.schemas import (
    AgentInfo,
    AgentInputRequest,
    SpawnAgentRequest,
    WaitAgentRequest,
)
from crabcode_gateway.session_registry import get_session_lock
from crabcode_gateway.task_registry import SessionOperationRejected, run_session_operation

router = APIRouter(prefix="/agent", tags=["agent"])


def _get_session(
    request: Request,
    session_id: str | None = None,
    *,
    agent_id: str | None = None,
    agent_ids: list[str] | None = None,
):
    """Resolve a session, falling back to the owner of ``agent_id``.

    Older clients rely on the process-wide default session.  Keep that
    behavior when no selector is supplied, but do not silently send an agent
    operation to the wrong session when the requested agent lives elsewhere.
    """
    sessions: dict = request.app.state.sessions
    closing = getattr(request.app.state, "closing_sessions", set())

    # JSON/query selectors preserve an explicitly supplied empty string as an
    # invalid selector; only ``None`` means the caller omitted it.
    requested = session_id if session_id is not None else None
    ids = [item for item in (agent_ids or []) if item]
    if agent_id:
        ids.append(agent_id)

    def _owns(candidate: object | None) -> bool:
        if candidate is None:
            return False
        if getattr(candidate, "session_id", None) in closing:
            return False
        if not ids:
            return True
        try:
            return all(candidate.get_agent(item) is not None for item in ids)  # type: ignore[attr-defined]
        except Exception:
            return False

    # An explicit selector is authoritative.  Falling back to the default
    # session here can cancel or inject input into an unrelated conversation.
    if requested is not None:
        selected = sessions.get(requested)
        return selected if _owns(selected) else None

    default_id = request.app.state.default_session_id
    selected = sessions.get(default_id) if default_id else None
    if _owns(selected):
        return selected

    if ids:
        for candidate in sessions.values():
            if candidate is selected:
                continue
            if _owns(candidate):
                return candidate
    return None


def _agent_info(snapshot) -> AgentInfo:
    """Expose lifecycle and callback delivery state for monitoring clients."""
    return AgentInfo(
        agent_id=snapshot.agent_id,
        session_id=snapshot.session_id,
        parent_agent_id=snapshot.parent_agent_id,
        title=snapshot.title,
        subagent_type=snapshot.subagent_type,
        status=snapshot.status,
        model=snapshot.model,
        created_at=snapshot.created_at,
        finished_at=snapshot.finished_at,
        usage=snapshot.usage,
        final_result=snapshot.final_result,
        error=snapshot.error,
        transcript_path=snapshot.transcript_path,
        callback_enabled=snapshot.callback_enabled,
        callback_state=snapshot.callback_state,
        callback_message_id=snapshot.callback_message_id,
        callback_epoch=snapshot.callback_epoch,
    )


@router.post("/spawn", response_model=AgentInfo)
async def spawn_agent(req: SpawnAgentRequest, request: Request) -> AgentInfo:
    """Spawn a managed sub-agent."""
    session = _get_session(
        request,
        req.session_id
        if req.session_id is not None
        else request.query_params.get("session_id"),
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    async def _spawn_and_snapshot():
        agent_id = await session.spawn_agent(
            prompt=req.prompt,
            subagent_type=req.subagent_type,
            name=req.name,
            model_profile=req.model_profile,
            callback=req.callback,
        )
        return session.get_agent(agent_id)

    try:
        snapshot = await run_session_operation(
            request.app.state,
            session,
            _spawn_and_snapshot,
        )
    except SessionOperationRejected as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not snapshot:
        raise HTTPException(status_code=500, detail="Agent spawn failed")

    return _agent_info(snapshot)


@router.get("/list", response_model=list[AgentInfo])
async def list_agents(request: Request) -> list[AgentInfo]:
    """List all managed agents."""
    async with get_session_lock(request.app.state):
        session = _get_session(request, request.query_params.get("session_id"))
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        snapshots = list(session.list_agents())

    return [_agent_info(snapshot) for snapshot in snapshots]


@router.get("/{agent_id}", response_model=AgentInfo)
async def get_agent(agent_id: str, request: Request) -> AgentInfo:
    """Get a specific agent's status."""
    async with get_session_lock(request.app.state):
        session = _get_session(
            request,
            request.query_params.get("session_id"),
            agent_id=agent_id,
        )
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        snapshot = session.get_agent(agent_id)
    if not snapshot:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")

    return _agent_info(snapshot)


@router.post("/{agent_id}/cancel")
async def cancel_agent(agent_id: str, request: Request):
    """Cancel a running agent."""
    session = _get_session(
        request,
        request.query_params.get("session_id"),
        agent_id=agent_id,
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        ok = await run_session_operation(
            request.app.state,
            session,
            lambda: session.cancel_agent(agent_id),
        )
    except SessionOperationRejected as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not ok:
        raise HTTPException(status_code=400, detail=f"Could not cancel agent {agent_id}")
    return {"status": "ok"}


@router.post("/{agent_id}/input")
async def send_agent_input(agent_id: str, req: AgentInputRequest, request: Request):
    """Send additional input to an agent."""
    session = _get_session(
        request,
        req.session_id
        if req.session_id is not None
        else request.query_params.get("session_id"),
        agent_id=agent_id,
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        ok = await run_session_operation(
            request.app.state,
            session,
            lambda: session.send_agent_input(agent_id, req.prompt, interrupt=req.interrupt),
        )
    except SessionOperationRejected as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not ok:
        raise HTTPException(status_code=400, detail=f"Could not send input to agent {agent_id}")
    return {"status": "ok"}


@router.post("/wait", response_model=AgentInfo)
async def wait_agent(req: WaitAgentRequest, request: Request) -> AgentInfo:
    """Wait for one or more agents to complete."""
    requested_session_id = (
        req.session_id
        if req.session_id is not None
        else request.query_params.get("session_id")
    )
    agent_ids = req.agent_id if isinstance(req.agent_id, list) else [req.agent_id]
    session = _get_session(
        request,
        requested_session_id,
        agent_ids=agent_ids,
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        snapshot = await run_session_operation(
            request.app.state,
            session,
            lambda: session.wait_agent(req.agent_id, timeout_ms=req.timeout_ms),
        )
    except SessionOperationRejected as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not snapshot:
        raise HTTPException(status_code=408, detail="Agent wait timed out")

    return _agent_info(snapshot)
