"""Agent-team lifecycle, messaging, and task-board routes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from inspect import isawaitable
from typing import Any, TypeVar

from fastapi import APIRouter, HTTPException, Request

from crabcode_core.team.models import BridgePolicy, TeammateRole
from crabcode_gateway.schemas import (
    CrossTeamMessageInfo,
    TeamBroadcastRequest,
    TeamBridgeInfo,
    TeamBridgeRequest,
    TeamCreateRequest,
    TeamCrossMessageRequest,
    TeamMessageInfo,
    TeamMessageRequest,
    TeamMessagesReadRequest,
    TeamRemoveRequest,
    TeamShutdownRequest,
    TeamSpawnRequest,
    TeamStatusInfo,
    TeamTaskAddRequest,
    TeamTaskClaimRequest,
    TeamTaskCompleteRequest,
    TeamTaskFailRequest,
    TeamTaskInfo,
)
from crabcode_gateway.task_registry import SessionOperationRejected, run_session_operation


router = APIRouter(prefix="/team", tags=["team"])
T = TypeVar("T")


def _get_session(request: Request, session_id: str | None = None):
    sessions: dict[str, Any] = request.app.state.sessions
    sid = request.app.state.default_session_id if session_id is None else session_id
    session = sessions.get(sid) if sid else None
    if session is None or getattr(session, "session_id", None) in getattr(request.app.state, "closing_sessions", set()):
        return None
    return session


async def _run_team_operation(
    request: Request,
    session_id: str | None,
    operation: Callable[[Any], T | Awaitable[T]],
) -> T:
    session = _get_session(request, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    async def _owned() -> T:
        initializer = getattr(session, "initialize", None)
        if callable(initializer):
            await initializer()
        manager = getattr(session, "_team_manager", None)
        if manager is None:
            raise HTTPException(status_code=503, detail="Team manager is unavailable")
        result = operation(manager)
        if isawaitable(result):
            return await result
        return result

    try:
        return await run_session_operation(
            request.app.state,
            session,
            _owned,
        )
    except SessionOperationRejected as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _message_info(message: Any) -> TeamMessageInfo:
    data = message.model_dump() if hasattr(message, "model_dump") else dict(message)
    return TeamMessageInfo.model_validate(data)


def _task_info(task: Any) -> TeamTaskInfo:
    data = task.model_dump() if hasattr(task, "model_dump") else dict(task)
    return TeamTaskInfo.model_validate(data)


def _cross_message_info(message: Any) -> CrossTeamMessageInfo:
    data = message.model_dump() if hasattr(message, "model_dump") else dict(message)
    return CrossTeamMessageInfo.model_validate(data)


@router.get("/list")
async def list_teams(request: Request, session_id: str | None = None) -> list[str]:
    return await _run_team_operation(request, session_id, lambda manager: list(manager.list_teams()))


@router.get("/status/{team_id}", response_model=TeamStatusInfo)
@router.get("/{team_id}/status", response_model=TeamStatusInfo)
async def team_status(team_id: str, request: Request, session_id: str | None = None) -> TeamStatusInfo:
    status = await _run_team_operation(request, session_id, lambda manager: manager.get_team_status(team_id))
    if not status:
        raise HTTPException(status_code=404, detail=f"Team '{team_id}' not found")
    return TeamStatusInfo.model_validate(status)


@router.get("/{team_id}/messages", response_model=list[TeamMessageInfo])
async def team_messages(
    team_id: str,
    request: Request,
    session_id: str | None = None,
    agent_id: str | None = None,
    unread: bool = False,
) -> list[TeamMessageInfo]:
    def _messages(manager: Any) -> list[TeamMessageInfo] | None:
        status = manager.get_team_status(team_id)
        if not status:
            return None
        teammate_ids = {
            str(teammate.get("agent_id") or "")
            for teammate in status.get("teammates", [])
        }
        if agent_id is not None:
            if agent_id not in teammate_ids:
                return None
            source = (
                manager.get_unread_messages(team_id, agent_id)
                if unread
                else manager.get_all_messages(team_id, agent_id)
            )
            return [_message_info(message) for message in source]
        seen: set[str] = set()
        result: list[TeamMessageInfo] = []
        for teammate in status.get("teammates", []):
            teammate_id = str(teammate.get("agent_id") or "")
            source = (
                manager.get_unread_messages(team_id, teammate_id)
                if unread
                else manager.get_all_messages(team_id, teammate_id)
            )
            for message in source:
                info = _message_info(message)
                if info.id in seen:
                    continue
                seen.add(info.id)
                result.append(info)
        result.sort(key=lambda item: item.timestamp)
        return result

    result = await _run_team_operation(request, session_id, _messages)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Team '{team_id}' not found")
    return result


@router.get("/{team_id}/tasks", response_model=list[TeamTaskInfo])
async def team_tasks(team_id: str, request: Request, session_id: str | None = None) -> list[TeamTaskInfo]:
    def _tasks(manager: Any) -> list[TeamTaskInfo] | None:
        if not manager.get_team_status(team_id):
            return None
        return [_task_info(task) for task in manager.list_tasks(team_id)]

    result = await _run_team_operation(request, session_id, _tasks)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Team '{team_id}' not found")
    return result


@router.post("/create")
async def create_team(req: TeamCreateRequest, request: Request) -> dict[str, Any]:
    try:
        team_id = await _run_team_operation(
            request,
            req.session_id,
            lambda manager: manager.create_team(req.name, max_teammates=req.max_teammates),
        )
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"team_id": team_id}


@router.post("/spawn")
async def spawn_teammate(req: TeamSpawnRequest, request: Request) -> dict[str, Any]:
    try:
        role = TeammateRole(req.role)
        agent_id = await _run_team_operation(
            request,
            req.session_id,
            lambda manager: manager.add_teammate(
                req.team_id,
                role=role,
                prompt=req.prompt,
                name=req.name,
                model_profile=req.model_profile,
            ),
        )
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"team_id": req.team_id, "agent_id": agent_id}


@router.post("/remove")
@router.post("/teammate/remove")
async def remove_teammate(req: TeamRemoveRequest, request: Request) -> dict[str, Any]:
    removed = await _run_team_operation(
        request,
        req.session_id,
        lambda manager: manager.remove_teammate(req.team_id, req.agent_id),
    )
    if not removed:
        raise HTTPException(status_code=404, detail="Team or teammate not found")
    return {"team_id": req.team_id, "agent_id": req.agent_id, "removed": True}


@router.post("/message")
async def send_team_message(req: TeamMessageRequest, request: Request) -> dict[str, Any]:
    message = await _run_team_operation(
        request,
        req.session_id,
        lambda manager: manager.send_message(req.team_id, req.from_agent, req.to, req.text),
    )
    if message is None:
        raise HTTPException(status_code=400, detail="Team message delivery failed")
    return {"message_id": message.id, "delivered": True}


@router.post("/broadcast")
async def broadcast_team_message(req: TeamBroadcastRequest, request: Request) -> dict[str, Any]:
    messages = await _run_team_operation(
        request,
        req.session_id,
        lambda manager: manager.broadcast(req.team_id, req.from_agent, req.text),
    )
    return {"recipient_count": len(messages)}


@router.post("/messages/read")
async def mark_team_messages_read(
    req: TeamMessagesReadRequest,
    request: Request,
) -> dict[str, Any]:
    def _mark(manager: Any):
        if manager.get_teammate(req.team_id, req.agent_id) is None:
            return None
        return manager.mark_read(req.team_id, req.agent_id, req.message_ids)

    count = await _run_team_operation(request, req.session_id, _mark)
    if count is None:
        raise HTTPException(status_code=404, detail="Team or teammate not found")
    return {
        "team_id": req.team_id,
        "agent_id": req.agent_id,
        "marked_read": count,
    }


@router.post("/task/add")
async def add_team_task(req: TeamTaskAddRequest, request: Request) -> dict[str, Any]:
    try:
        task_id = await _run_team_operation(
            request,
            req.session_id,
            lambda manager: manager.add_task(req.team_id, req.description),
        )
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"task_id": task_id}


@router.post("/task/claim")
async def claim_team_task(req: TeamTaskClaimRequest, request: Request) -> dict[str, Any]:
    claimed = await _run_team_operation(
        request,
        req.session_id,
        lambda manager: manager.claim_task(req.team_id, req.task_id, req.agent_id),
    )
    if not claimed:
        raise HTTPException(status_code=400, detail="Task not found or already claimed")
    return {"task_id": req.task_id, "claimed": True}


@router.post("/task/complete")
async def complete_team_task(req: TeamTaskCompleteRequest, request: Request) -> dict[str, Any]:
    completed = await _run_team_operation(
        request,
        req.session_id,
        lambda manager: manager.complete_task(req.team_id, req.task_id, req.result, req.agent_id),
    )
    if not completed:
        raise HTTPException(status_code=400, detail="Task not found or not in claimed state")
    return {"task_id": req.task_id, "completed": True}


@router.post("/task/fail")
async def fail_team_task(req: TeamTaskFailRequest, request: Request) -> dict[str, Any]:
    failed = await _run_team_operation(
        request,
        req.session_id,
        lambda manager: manager.fail_task(
            req.team_id,
            req.task_id,
            req.reason,
            req.agent_id,
        ),
    )
    if not failed:
        raise HTTPException(status_code=400, detail="Task not found or not in claimed state")
    return {"task_id": req.task_id, "failed": True}


@router.get("/{team_a}/bridge/{team_b}", response_model=TeamBridgeInfo)
async def get_team_bridge(
    team_a: str,
    team_b: str,
    request: Request,
    session_id: str | None = None,
) -> TeamBridgeInfo:
    def _get(manager: Any) -> TeamBridgeInfo | None:
        if not manager.get_team_status(team_a) or not manager.get_team_status(team_b):
            return None
        policy = manager.get_bridge_policy(team_a, team_b)
        return TeamBridgeInfo(
            team_a=team_a,
            team_b=team_b,
            policy=getattr(policy, "value", policy),
        )

    result = await _run_team_operation(request, session_id, _get)
    if result is None:
        raise HTTPException(status_code=404, detail="One or both teams were not found")
    return result


@router.post("/bridge", response_model=TeamBridgeInfo)
async def register_team_bridge(
    req: TeamBridgeRequest,
    request: Request,
) -> TeamBridgeInfo:
    def _register(manager: Any) -> TeamBridgeInfo | None:
        if not manager.get_team_status(req.team_a) or not manager.get_team_status(req.team_b):
            return None
        manager.register_bridge(req.team_a, req.team_b, BridgePolicy(req.policy))
        return TeamBridgeInfo(
            team_a=req.team_a,
            team_b=req.team_b,
            policy=req.policy,
        )

    try:
        result = await _run_team_operation(request, req.session_id, _register)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="One or both teams were not found")
    return result


@router.post("/cross-message", response_model=CrossTeamMessageInfo)
async def send_cross_team_message(
    req: TeamCrossMessageRequest,
    request: Request,
) -> CrossTeamMessageInfo:
    message = await _run_team_operation(
        request,
        req.session_id,
        lambda manager: manager.send_cross_team(
            req.from_team,
            req.from_agent,
            req.to_team,
            req.to_agent,
            req.text,
        ),
    )
    if message is None:
        raise HTTPException(
            status_code=400,
            detail="Cross-team message was refused or could not be delivered",
        )
    return _cross_message_info(message)


@router.post("/shutdown")
async def shutdown_team(req: TeamShutdownRequest, request: Request) -> dict[str, Any]:
    shutdown = await _run_team_operation(
        request,
        req.session_id,
        lambda manager: manager.shutdown_team(req.team_id),
    )
    if not shutdown:
        raise HTTPException(status_code=404, detail=f"Team '{req.team_id}' not found")
    return {"team_id": req.team_id, "shutdown": True}
