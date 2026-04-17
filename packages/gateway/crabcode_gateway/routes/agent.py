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
    )
    snapshot = session.get_agent(agent_id)
    if not snapshot:
        raise HTTPException(status_code=500, detail="Agent spawn failed")

    return AgentInfo(
        agent_id=snapshot.agent_id,
        parent_agent_id=snapshot.parent_agent_id,
        title=snapshot.title,
        subagent_type=snapshot.subagent_type,
        status=snapshot.status,
        model=snapshot.model,
        created_at=snapshot.created_at,
        usage=snapshot.usage,
        final_result=snapshot.final_result,
        error=snapshot.error,
    )


@router.get("/list", response_model=list[AgentInfo])
async def list_agents(request: Request) -> list[AgentInfo]:
    """List all managed agents."""
    session = _get_session(request)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    return [
        AgentInfo(
            agent_id=s.agent_id,
            parent_agent_id=s.parent_agent_id,
            title=s.title,
            subagent_type=s.subagent_type,
            status=s.status,
            model=s.model,
            created_at=s.created_at,
            usage=s.usage,
            final_result=s.final_result,
            error=s.error,
        )
        for s in session.list_agents()
    ]


@router.get("/{agent_id}", response_model=AgentInfo)
async def get_agent(agent_id: str, request: Request) -> AgentInfo:
    """Get a specific agent's status."""
    session = _get_session(request)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    snapshot = session.get_agent(agent_id)
    if not snapshot:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")

    return AgentInfo(
        agent_id=snapshot.agent_id,
        parent_agent_id=snapshot.parent_agent_id,
        title=snapshot.title,
        subagent_type=snapshot.subagent_type,
        status=snapshot.status,
        model=snapshot.model,
        created_at=snapshot.created_at,
        usage=snapshot.usage,
        final_result=snapshot.final_result,
        error=snapshot.error,
    )


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

    return AgentInfo(
        agent_id=snapshot.agent_id,
        parent_agent_id=snapshot.parent_agent_id,
        title=snapshot.title,
        subagent_type=snapshot.subagent_type,
        status=snapshot.status,
        model=snapshot.model,
        created_at=snapshot.created_at,
        usage=snapshot.usage,
        final_result=snapshot.final_result,
        error=snapshot.error,
    )
