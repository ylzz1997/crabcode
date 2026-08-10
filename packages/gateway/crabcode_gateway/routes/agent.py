"""Agent management routes — /agent/*."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from crabcode_gateway.schemas import (
    AgentInfo,
    AgentInputRequest,
    SpawnAgentRequest,
    WaitAgentRequest,
)

router = APIRouter(prefix="/agent", tags=["agent"])


def _get_session(request: Request, session_id: str | None = None):
    sessions: dict = request.app.state.sessions
    sid = session_id or request.app.state.default_session_id
    if not sid or sid not in sessions:
        return None
    return sessions[sid]


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
    session = _get_session(request)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    agent_id = await session.spawn_agent(
        prompt=req.prompt,
        subagent_type=req.subagent_type,
        name=req.name,
        model_profile=req.model_profile,
        callback=req.callback,
    )
    snapshot = session.get_agent(agent_id)
    if not snapshot:
        raise HTTPException(status_code=500, detail="Agent spawn failed")

    return _agent_info(snapshot)


@router.get("/list", response_model=list[AgentInfo])
async def list_agents(request: Request) -> list[AgentInfo]:
    """List all managed agents."""
    session = _get_session(request)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    return [_agent_info(snapshot) for snapshot in session.list_agents()]


@router.get("/{agent_id}", response_model=AgentInfo)
async def get_agent(agent_id: str, request: Request) -> AgentInfo:
    """Get a specific agent's status."""
    session = _get_session(request)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    snapshot = session.get_agent(agent_id)
    if not snapshot:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")

    return _agent_info(snapshot)


@router.post("/{agent_id}/cancel")
async def cancel_agent(agent_id: str, request: Request):
    """Cancel a running agent."""
    session = _get_session(request)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    ok = await session.cancel_agent(agent_id)
    if not ok:
        raise HTTPException(status_code=400, detail=f"Could not cancel agent {agent_id}")
    return {"status": "ok"}


@router.post("/{agent_id}/input")
async def send_agent_input(agent_id: str, req: AgentInputRequest, request: Request):
    """Send additional input to an agent."""
    session = _get_session(request)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    ok = await session.send_agent_input(agent_id, req.prompt, interrupt=req.interrupt)
    if not ok:
        raise HTTPException(status_code=400, detail=f"Could not send input to agent {agent_id}")
    return {"status": "ok"}


@router.post("/wait", response_model=AgentInfo)
async def wait_agent(req: WaitAgentRequest, request: Request) -> AgentInfo:
    """Wait for one or more agents to complete."""
    session = _get_session(request)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    snapshot = await session.wait_agent(req.agent_id, timeout_ms=req.timeout_ms)
    if not snapshot:
        raise HTTPException(status_code=408, detail="Agent wait timed out")

    return _agent_info(snapshot)
