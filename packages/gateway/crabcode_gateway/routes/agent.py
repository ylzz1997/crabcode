"""Agent management routes — /agent/*."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from crabcode_gateway.schemas import (
    AgentTranscriptResponse,
    AgentInfo,
    AgentInputRequest,
    SpawnAgentRequest,
    WaitAgentRequest,
)
from crabcode_gateway.session_registry import get_session_lock
from crabcode_gateway.safe_file import read_regular_file_tail
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
            agents = list(candidate.list_agents())  # type: ignore[attr-defined]
            for selector in ids:
                exact = [item for item in agents if item.agent_id == selector]
                if exact:
                    continue
                if len([item for item in agents if item.agent_id.startswith(selector)]) != 1:
                    return False
            return True
        except Exception:
            return False

    # An explicit selector is authoritative.  It chooses the session only;
    # agent existence is resolved inside the admitted operation so a missing
    # agent is reported as an agent 404 rather than "Session not found".
    if requested is not None:
        selected = sessions.get(requested)
        if selected is None or getattr(selected, "session_id", None) in closing:
            return None
        return selected

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
        parent_tool_use_id=getattr(snapshot, "parent_tool_use_id", None),
        title=snapshot.title,
        subagent_type=snapshot.subagent_type,
        status=snapshot.status,
        model=snapshot.model,
        model_profile=getattr(snapshot, "model_profile", None),
        created_at=snapshot.created_at,
        started_at=getattr(snapshot, "started_at", None),
        finished_at=snapshot.finished_at,
        updated_at=getattr(snapshot, "updated_at", snapshot.created_at),
        usage=snapshot.usage,
        final_result=snapshot.final_result,
        error=snapshot.error,
        depth=getattr(snapshot, "depth", 0),
        transcript_path=snapshot.transcript_path,
        callback_enabled=snapshot.callback_enabled,
        callback_state=snapshot.callback_state,
        callback_message_id=snapshot.callback_message_id,
        callback_epoch=snapshot.callback_epoch,
    )


def _resolve_agent(session, selector: str):
    """Resolve an exact id or a unique CLI-style id prefix."""
    value = str(selector or "").strip()
    if not value:
        return None
    exact = session.get_agent(value)
    if exact is not None:
        return exact
    matches = [item for item in session.list_agents() if item.agent_id.startswith(value)]
    return matches[0] if len(matches) == 1 else None


def _read_agent_tail(
    session,
    agent_id: str,
    max_lines: int = 200,
) -> tuple[str, list[str], bool]:
    from crabcode_core.session.storage import get_agent_transcript_path

    path = get_agent_transcript_path(
        session.cwd,
        session.session_id,
        agent_id,
    )
    try:
        lines, truncated = read_regular_file_tail(path, max_lines=max_lines)
    except OSError:
        return str(path), [], False
    return str(path), lines, truncated


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
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
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

    async def _list():
        initializer = getattr(session, "initialize", None)
        if callable(initializer):
            await initializer()
        return list(session.list_agents())

    try:
        snapshots = await run_session_operation(request.app.state, session, _list)
    except SessionOperationRejected as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

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

    async def _get():
        initializer = getattr(session, "initialize", None)
        if callable(initializer):
            await initializer()
        return _resolve_agent(session, agent_id)

    try:
        snapshot = await run_session_operation(request.app.state, session, _get)
    except SessionOperationRejected as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not snapshot:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")

    return _agent_info(snapshot)


@router.get("/{agent_id}/transcript", response_model=AgentTranscriptResponse)
@router.get("/{agent_id}/log", response_model=AgentTranscriptResponse)
async def get_agent_transcript(agent_id: str, request: Request) -> AgentTranscriptResponse:
    """Return the stored tail of an agent transcript for desktop clients."""
    async with get_session_lock(request.app.state):
        session = _get_session(
            request,
            request.query_params.get("session_id"),
            agent_id=agent_id,
        )
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

    async def _get():
        initializer = getattr(session, "initialize", None)
        if callable(initializer):
            await initializer()
        return _resolve_agent(session, agent_id)

    try:
        snapshot = await run_session_operation(request.app.state, session, _get)
    except SessionOperationRejected as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not snapshot:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")
    try:
        limit = max(1, min(10_000, int(request.query_params.get("lines", "200"))))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="lines must be an integer")
    path, lines, truncated = _read_agent_tail(session, snapshot.agent_id, limit)
    return AgentTranscriptResponse(
        agent_id=snapshot.agent_id,
        path=path,
        lines=lines,
        truncated=truncated,
    )


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

    async def _cancel():
        initializer = getattr(session, "initialize", None)
        if callable(initializer):
            await initializer()
        snapshot = _resolve_agent(session, agent_id)
        if snapshot is None:
            return None
        return await session.cancel_agent(snapshot.agent_id)

    try:
        ok = await run_session_operation(
            request.app.state,
            session,
            _cancel,
        )
    except SessionOperationRejected as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if ok is None:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")
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

    async def _send_input():
        initializer = getattr(session, "initialize", None)
        if callable(initializer):
            await initializer()
        snapshot = _resolve_agent(session, agent_id)
        if snapshot is None:
            return None
        return await session.send_agent_input(
            snapshot.agent_id,
            req.prompt,
            interrupt=req.interrupt,
        )

    try:
        ok = await run_session_operation(
            request.app.state,
            session,
            _send_input,
        )
    except SessionOperationRejected as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if ok is None:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")
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

    async def _resolve_and_wait():
        initializer = getattr(session, "initialize", None)
        if callable(initializer):
            await initializer()
        resolved_ids: list[str] = []
        for selector in agent_ids:
            snapshot = _resolve_agent(session, selector)
            if snapshot is None:
                return selector, None
            resolved_ids.append(snapshot.agent_id)
        target: str | list[str] = (
            resolved_ids if isinstance(req.agent_id, list) else resolved_ids[0]
        )
        return None, await session.wait_agent(target, timeout_ms=req.timeout_ms)

    try:
        missing_selector, snapshot = await run_session_operation(
            request.app.state,
            session,
            _resolve_and_wait,
        )
    except SessionOperationRejected as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if missing_selector is not None:
        raise HTTPException(status_code=404, detail=f"Agent {missing_selector} not found")
    if not snapshot:
        raise HTTPException(status_code=408, detail="Agent wait timed out")

    return _agent_info(snapshot)
