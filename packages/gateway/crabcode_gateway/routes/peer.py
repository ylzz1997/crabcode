"""Cross-session peer discovery and messaging routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from crabcode_gateway.schemas import PeerDeliveryInfo, PeerInfo, PeerSendRequest
from crabcode_gateway.task_registry import SessionOperationRejected, run_session_operation


router = APIRouter(tags=["peer"])


def _get_session(request: Request, session_id: str | None = None):
    sessions: dict[str, Any] = request.app.state.sessions
    sid = request.app.state.default_session_id if session_id is None else session_id
    session = sessions.get(sid) if sid else None
    if session is None or getattr(session, "session_id", None) in getattr(request.app.state, "closing_sessions", set()):
        return None
    return session


def _peer_info(peer: Any) -> PeerInfo:
    data = peer.model_dump(exclude={"auth_token"}) if hasattr(peer, "model_dump") else dict(peer)
    return PeerInfo.model_validate(data)


@router.get("/peer/list", response_model=list[PeerInfo])
@router.get("/peers", response_model=list[PeerInfo])
async def list_peers(request: Request, session_id: str | None = None) -> list[PeerInfo]:
    session = _get_session(request, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    async def _list() -> list[PeerInfo]:
        runtime = await session.ensure_peer_runtime()
        if runtime is None:
            return []
        return [_peer_info(peer) for peer in runtime.list_peers()]

    try:
        return await run_session_operation(request.app.state, session, _list)
    except SessionOperationRejected as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (RuntimeError, OSError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/peer/send", response_model=PeerDeliveryInfo)
@router.post("/peers/send", response_model=PeerDeliveryInfo)
async def send_peer_message(req: PeerSendRequest, request: Request) -> PeerDeliveryInfo:
    session = _get_session(request, req.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    async def _send() -> PeerDeliveryInfo:
        runtime = await session.ensure_peer_runtime()
        if runtime is None:
            raise RuntimeError("Cross-session messaging is disabled")
        delivery = await runtime.send(req.to, req.text)
        return PeerDeliveryInfo.model_validate(
            delivery.model_dump() if hasattr(delivery, "model_dump") else delivery
        )

    try:
        delivery = await run_session_operation(request.app.state, session, _send)
    except SessionOperationRejected as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (RuntimeError, OSError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if delivery.status in {"failed", "refused"}:
        # Preserve the structured acknowledgement while giving HTTP clients a
        # useful status code for automation.
        return delivery
    return delivery

