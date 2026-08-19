"""gRPC server implementation for CrabCode Gateway.

Uses the generated stubs checked in alongside ``crabcode.proto``.  Keeping
the generated modules in the package makes the gRPC endpoint available in
normal installations without requiring ``grpcio-tools`` at runtime.

In production, run::

    python -m grpc_tools.protoc \\
        -I packages/gateway/crabcode_gateway/grpc/proto \\
        --python_out=packages/gateway/crabcode_gateway/grpc \\
        --grpc_python_out=packages/gateway/crabcode_gateway/grpc \\
        packages/gateway/crabcode_gateway/grpc/proto/crabcode.proto
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
from concurrent import futures
from typing import Any

import grpc

from crabcode_core import VERSION
from crabcode_core.logging_utils import get_logger
from crabcode_gateway.adapter import ProtocolAdapter
from crabcode_gateway.auth import decode_jwt, verify_password
from crabcode_gateway.schemas import core_event_to_payload
from crabcode_gateway.task_registry import (
    SessionOperationRejected,
    run_session_operation,
    session_lease,
)

logger = get_logger(__name__)

_PROTO_INT64_MIN = -(1 << 63)
_PROTO_INT64_MAX = (1 << 63) - 1


async def _call_sync(fn: Any) -> Any:
    """Adapt a synchronous CoreSession accessor to the owned-task helper."""
    return fn()


def _session_from_app(
    app_state: Any,
    session_id: str | None = None,
    *,
    agent_id: str | None = None,
    agent_ids: list[str] | None = None,
) -> Any | None:
    """Resolve a session without silently crossing an explicit boundary.

    Agent RPCs may omit ``session_id`` for backwards compatibility.  In that
    case an agent selector is resolved by ownership; requests that explicitly
    name a session must stay in that session and are never redirected to the
    process default.
    """
    sessions: dict = getattr(app_state, "sessions", {})
    if getattr(app_state, "gateway_closing", False):
        return None
    closing = getattr(app_state, "closing_sessions", set())
    # A missing selector may use the legacy default; an explicitly supplied
    # value is authoritative, including an empty/invalid value.
    # Protobuf scalar strings use ``""`` for an omitted optional selector;
    # normalize that wire representation before applying ownership rules.
    requested = session_id if session_id not in (None, "") else None
    selected = sessions.get(requested) if requested else None

    ids = [item for item in (agent_ids or []) if item]
    if agent_id:
        ids.append(agent_id)

    def owns(candidate: Any) -> bool:
        if candidate is None:
            return False
        if getattr(candidate, "session_id", None) in closing:
            return False
        if not ids:
            return True
        try:
            return all(candidate.get_agent(item) is not None for item in ids)
        except Exception:
            return False

    if requested:
        return selected if owns(selected) else None

    if ids:
        for candidate in sessions.values():
            if owns(candidate):
                return candidate
        return None

    default_id = getattr(app_state, "default_session_id", None)
    if default_id and default_id in sessions:
        return sessions[default_id]
    if sessions:
        return next(iter(sessions.values()))
    return None


def _event_to_proto(event: Any) -> dict:
    """Convert a CoreEvent to a gRPC-friendly dict."""
    payload = core_event_to_payload(event)
    return {
        "type": payload.type,
        "payload_json": payload.model_dump_json(),
    }


def _snapshot_to_proto(snapshot: Any) -> dict[str, Any]:
    """Map the complete observable agent lifecycle to the gRPC shape."""
    usage: dict[str, int] = {}
    for key, value in (snapshot.usage or {}).items():
        try:
            numeric = int(value)
        except (TypeError, ValueError, OverflowError):
            # The protobuf contract uses int64 values.  A malformed provider
            # usage field should not make an otherwise valid status unreadable.
            logger.debug("Ignoring non-integer usage value for %s: %r", key, value)
            continue
        if not _PROTO_INT64_MIN <= numeric <= _PROTO_INT64_MAX:
            logger.debug("Ignoring out-of-range usage value for %s: %r", key, value)
            continue
        usage[str(key)] = numeric
    payload: dict[str, Any] = {
        "agent_id": snapshot.agent_id,
        "session_id": snapshot.session_id,
        "parent_agent_id": snapshot.parent_agent_id or "",
        "title": snapshot.title,
        "subagent_type": snapshot.subagent_type,
        "status": snapshot.status,
        "model": snapshot.model,
        "created_at": snapshot.created_at,
        "usage": usage,
        "final_result": snapshot.final_result,
        "error": snapshot.error,
        "callback_enabled": snapshot.callback_enabled,
        "callback_state": snapshot.callback_state,
        "callback_epoch": snapshot.callback_epoch,
    }
    if snapshot.finished_at:
        payload["finished_at"] = snapshot.finished_at
    if snapshot.transcript_path:
        payload["transcript_path"] = snapshot.transcript_path
    if snapshot.callback_message_id:
        payload["callback_message_id"] = snapshot.callback_message_id
    return payload


class _CrabCodeServicer:
    """Hand-written servicer that delegates to CoreSession.

    The generated protobuf service wrapper below adapts wire messages to this
    dictionary-based implementation, keeping the core session API protocol
    agnostic.
    """

    def __init__(
        self,
        app_state: Any,
        *,
        username: str = "crabcode",
        password: str | None = None,
        password_hash: str | None = None,
        security_mode: str | None = None,
        jwt_secret: str | None = None,
    ) -> None:
        self._app_state = app_state
        self._event_bus = getattr(app_state, "event_bus", None)
        self._auth_username = username
        self._auth_password = password
        self._auth_password_hash = password_hash
        self._security_mode = security_mode or (
            "password" if password or password_hash else "none"
        )
        self._jwt_secret = jwt_secret

    async def _require_auth(self, context: Any) -> None:
        """Apply the same credentials as HTTP/WS when gRPC is protected."""
        if self._security_mode == "none":
            return
        metadata = {}
        try:
            metadata = {
                str(key).lower(): str(value)
                for key, value in context.invocation_metadata()
            }
        except Exception:
            metadata = {}
        header = metadata.get("authorization", "")
        scheme, separator, credentials = header.partition(" ")
        authorized = False
        if separator and scheme.lower() == "bearer":
            authorized = bool(self._jwt_secret and decode_jwt(credentials, self._jwt_secret))
            if not authorized and self._security_mode in ("password", "mixed"):
                authorized = bool(
                    (self._auth_password and hmac.compare_digest(credentials, self._auth_password))
                    or (self._auth_password_hash and verify_password(credentials, self._auth_password_hash))
                )
        elif separator and scheme.lower() == "basic" and self._security_mode in ("password", "mixed"):
            try:
                decoded = base64.b64decode(credentials, validate=True).decode("utf-8")
                user, supplied = decoded.split(":", 1)
                authorized = hmac.compare_digest(user, self._auth_username) and bool(
                    (self._auth_password and hmac.compare_digest(supplied, self._auth_password))
                    or (self._auth_password_hash and verify_password(supplied, self._auth_password_hash))
                )
            except (ValueError, UnicodeDecodeError, binascii.Error):
                authorized = False
        if not authorized:
            await context.abort(
                code=grpc.StatusCode.UNAUTHENTICATED,
                details="Unauthenticated",
            )

    async def _owned(self, session: Any, operation: Any, context: Any) -> Any:
        """Run a unary RPC under the session task/lifecycle fence."""
        try:
            return await run_session_operation(self._app_state, session, operation)
        except SessionOperationRejected as exc:
            await context.abort(
                code=grpc.StatusCode.UNAVAILABLE,
                details=str(exc),
            )

    # ── Unary RPCs ──────────────────────────────────────────────

    async def SpawnAgent(self, request: dict, context: Any) -> dict:
        await self._require_auth(context)
        session = _session_from_app(self._app_state, request.get("session_id"))
        if not session:
            await context.abort(code=grpc.StatusCode.NOT_FOUND, details="No active session")
        agent_id = await self._owned(
            session,
            lambda: session.spawn_agent(
                prompt=request.get("prompt", ""),
                subagent_type=request.get("subagent_type", "generalPurpose"),
                name=request.get("name"),
                model_profile=request.get("model_profile"),
                callback=bool(request.get("callback", False)),
            ),
            context,
        )
        return {"agent_id": agent_id}

    async def GetAgent(self, request: dict, context: Any) -> dict:
        await self._require_auth(context)
        session = _session_from_app(
            self._app_state,
            request.get("session_id"),
            agent_id=request.get("agent_id"),
        )
        if not session:
            await context.abort(code=grpc.StatusCode.NOT_FOUND, details="No active session")
        snapshot = await self._owned(
            session,
            lambda: _call_sync(lambda: session.get_agent(request.get("agent_id", ""))),
            context,
        )
        if not snapshot:
            await context.abort(code=grpc.StatusCode.NOT_FOUND, details="Agent not found")
        return _snapshot_to_proto(snapshot)

    async def ListAgents(self, request: dict, context: Any) -> dict:
        await self._require_auth(context)
        session = _session_from_app(self._app_state, request.get("session_id"))
        if not session:
            await context.abort(code=grpc.StatusCode.NOT_FOUND, details="No active session")
        agents = await self._owned(
            session,
            lambda: _call_sync(lambda: [_snapshot_to_proto(snapshot) for snapshot in session.list_agents()]),
            context,
        )
        return {"agents": agents}

    async def CancelAgent(self, request: dict, context: Any) -> dict:
        await self._require_auth(context)
        session = _session_from_app(
            self._app_state,
            request.get("session_id"),
            agent_id=request.get("agent_id"),
        )
        if not session:
            await context.abort(code=grpc.StatusCode.NOT_FOUND, details="No active session")
        ok = await self._owned(
            session,
            lambda: session.cancel_agent(request.get("agent_id", "")),
            context,
        )
        return {"ok": ok}

    async def WaitAgent(self, request: dict, context: Any) -> dict:
        await self._require_auth(context)
        agent_ids = [item for item in request.get("agent_ids", []) if item]
        if not agent_ids:
            await context.abort(
                code=grpc.StatusCode.INVALID_ARGUMENT,
                details="At least one agent id is required",
            )
        session = _session_from_app(
            self._app_state,
            request.get("session_id"),
            agent_ids=agent_ids,
        )
        if not session:
            await context.abort(code=grpc.StatusCode.NOT_FOUND, details="No active session")
        timeout_ms = request.get("timeout_ms")
        if len(agent_ids) == 1:
            snapshot = await self._owned(
                session,
                lambda: session.wait_agent(agent_ids[0], timeout_ms=timeout_ms),
                context,
            )
        else:
            snapshot = await self._owned(
                session,
                lambda: session.wait_agent(agent_ids, timeout_ms=timeout_ms),
                context,
            )
        if not snapshot:
            await context.abort(code=grpc.StatusCode.DEADLINE_EXCEEDED, details="Wait timed out")
        return _snapshot_to_proto(snapshot)

    async def RespondPermission(self, request: dict, context: Any) -> dict:
        from crabcode_core.types.event import PermissionResponseEvent
        await self._require_auth(context)
        session = _session_from_app(
            self._app_state,
            request.get("session_id"),
            agent_id=request.get("agent_id"),
        )
        if not session:
            await context.abort(code=grpc.StatusCode.NOT_FOUND, details="No active session")
        event = PermissionResponseEvent(
            tool_use_id=request.get("tool_use_id", ""),
            allowed=request.get("allowed", False),
            always_allow=request.get("always_allow", False),
            agent_id=request.get("agent_id"),
        )
        await self._owned(
            session,
            lambda: session.respond_permission(event),
            context,
        )
        return {}

    async def RespondChoice(self, request: dict, context: Any) -> dict:
        from crabcode_core.types.event import ChoiceResponseEvent
        await self._require_auth(context)
        session = _session_from_app(
            self._app_state,
            request.get("session_id"),
            agent_id=request.get("agent_id"),
        )
        if not session:
            await context.abort(code=grpc.StatusCode.NOT_FOUND, details="No active session")
        event = ChoiceResponseEvent(
            tool_use_id=request.get("tool_use_id", ""),
            selected=request.get("selected", []),
            cancelled=request.get("cancelled", False),
            agent_id=request.get("agent_id"),
        )
        await self._owned(
            session,
            lambda: session.respond_choice(event),
            context,
        )
        return {}

    async def ListModels(self, request: dict, context: Any) -> dict:
        await self._require_auth(context)
        session = _session_from_app(self._app_state, request.get("session_id"))
        if not session:
            await context.abort(code=grpc.StatusCode.NOT_FOUND, details="No active session")
        models = await self._owned(
            session,
            lambda: _call_sync(lambda: list(session.list_models().items())),
            context,
        )
        result_models = []
        for name, desc in models:
            result_models.append({"name": name, "description": desc})
        return {"models": result_models}

    async def SwitchModel(self, request: dict, context: Any) -> dict:
        await self._require_auth(context)
        session = _session_from_app(self._app_state, request.get("session_id"))
        if not session:
            await context.abort(code=grpc.StatusCode.NOT_FOUND, details="No active session")
        ok = await self._owned(
            session,
            lambda: _call_sync(lambda: session.switch_model(request.get("name", ""))),
            context,
        )
        if not ok:
            await context.abort(code=grpc.StatusCode.INVALID_ARGUMENT, details="Model not found")
        return {}

    async def SwitchMode(self, request: dict, context: Any) -> dict:
        await self._require_auth(context)
        session = _session_from_app(self._app_state, request.get("session_id"))
        if not session:
            await context.abort(code=grpc.StatusCode.NOT_FOUND, details="No active session")
        ok = await self._owned(
            session,
            lambda: _call_sync(lambda: session.switch_mode(request.get("mode", "agent"))),
            context,
        )
        if not ok:
            await context.abort(code=grpc.StatusCode.INVALID_ARGUMENT, details="Invalid mode")
        return {}

    async def HealthCheck(self, request: dict, context: Any) -> dict:
        # Keep health probes public, matching the HTTP /health endpoint.
        return {"status": "ok", "version": VERSION}

    # ── Server-streaming RPCs ────────────────────────────────────

    async def SendMessage(self, request: dict, context: Any) -> Any:
        """Stream CoreEvents as the query loop runs."""
        await self._require_auth(context)
        from crabcode_gateway.session_registry import get_session_lock

        async with get_session_lock(self._app_state):
            session = _session_from_app(self._app_state, request.get("session_id"))
        if not session:
            await context.abort(code=grpc.StatusCode.NOT_FOUND, details="No active session")

        text = request.get("text", "")
        max_turns = request.get("max_turns", 0)

        try:
            async with session_lease(self._app_state, session):
                async for event in session.send_message(text, max_turns=max_turns):
                    yield _event_to_proto(event)
        except SessionOperationRejected as exc:
            await context.abort(code=grpc.StatusCode.UNAVAILABLE, details=str(exc))

    async def SubscribeEvents(self, request: dict, context: Any) -> Any:
        """Subscribe to the event bus SSE stream via gRPC."""
        await self._require_auth(context)
        if not self._event_bus:
            await context.abort(code=grpc.StatusCode.INTERNAL, details="Event bus not available")
            return

        session_id = request.get("session_id") or None
        from crabcode_gateway.session_registry import get_session_lock

        subscriber = None
        async with get_session_lock(self._app_state):
            closing = getattr(self._app_state, "gateway_closing", False)
            selected = (
                _session_from_app(self._app_state, session_id)
                if session_id is not None
                else None
            )
            known = session_id is None or selected is not None
            session_closing = bool(
                session_id is not None
                and session_id in getattr(self._app_state, "closing_sessions", set())
            )
            if not closing and known and not session_closing:
                # Async generators are lazy: constructing ``sse_stream`` alone
                # does not register its subscriber until iteration begins.
                # Create it under the same lock used by gateway stop so
                # close_all() cannot run before this queue becomes visible.
                subscriber = self._event_bus.subscribe(session_id)
        if closing:
            await context.abort(
                code=grpc.StatusCode.UNAVAILABLE,
                details="Gateway is shutting down",
            )
        if not known:
            await context.abort(
                code=grpc.StatusCode.NOT_FOUND,
                details="Session not found",
            )
        if session_closing:
            await context.abort(
                code=grpc.StatusCode.UNAVAILABLE,
                details="Session is shutting down",
            )
        assert subscriber is not None
        try:
            if session_id is None:
                # A global subscription has no session lease; it is bounded by
                # the transport itself and is not tied to one CoreSession.
                stream = self._event_bus.sse_stream(
                    session_id,
                    subscriber=subscriber,
                )
                async for data in stream:
                    try:
                        parsed = json.loads(data)
                        yield {"type": parsed.get("type", "unknown"), "payload_json": data}
                    except json.JSONDecodeError:
                        yield {"type": "unknown", "payload_json": data}
            else:
                async with session_lease(self._app_state, selected):
                    async for data in self._event_bus.sse_stream(
                        session_id,
                        subscriber=subscriber,
                    ):
                        try:
                            parsed = json.loads(data)
                            yield {"type": parsed.get("type", "unknown"), "payload_json": data}
                        except json.JSONDecodeError:
                            yield {"type": "unknown", "payload_json": data}
        except SessionOperationRejected as exc:
            await context.abort(code=grpc.StatusCode.UNAVAILABLE, details=str(exc))
        finally:
            # ``sse_stream`` normally removes it in its own finally block;
            # this idempotent call also covers rejection before iteration.
            self._event_bus.unsubscribe(subscriber)


class GrpcAdapter(ProtocolAdapter):
    """gRPC protocol adapter.

    Starts a grpcio async server on the configured port.
    """

    def __init__(
        self,
        app_state: Any,
        password: str | None = None,
        password_hash: str | None = None,
        security_mode: str | None = None,
        jwt_secret: str | None = None,
    ) -> None:
        self._app_state = app_state
        self._password = password
        self._password_hash = password_hash
        self._security_mode = security_mode or (
            "password" if password or password_hash else "none"
        )
        self._jwt_secret = jwt_secret
        self._server: Any = None
        self._running = False
        self._bound_port: int | None = None

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def bound_port(self) -> int | None:
        """Return the port selected by gRPC (useful when binding port 0)."""
        return self._bound_port

    async def start(self, host: str, port: int) -> None:
        """Start the gRPC server using the bundled generated protobuf stubs."""
        if self._running or self._server is not None:
            raise RuntimeError("gRPC server is already started")
        try:
            from grpc import aio as grpc_aio
        except ImportError:
            logger.warning("grpcio not installed, skipping gRPC server")
            return

        servicer = _CrabCodeServicer(
            self._app_state,
            username=getattr(self._app_state, "gateway_username", "crabcode"),
            password=(
                self._password
                if self._password is not None
                else getattr(self._app_state, "gateway_password", None)
            ),
            password_hash=self._password_hash,
            security_mode=self._security_mode,
            jwt_secret=self._jwt_secret,
        )

        self._server = grpc_aio.server(futures.ThreadPoolExecutor(max_workers=4))

        # Register the generated stubs shipped with this package.
        try:
            from crabcode_gateway.grpc import crabcode_pb2_grpc  # noqa: F401

            crabcode_pb2_grpc.add_CrabCodeServiceServicer_to_server(
                _GeneratedStubServicer(servicer), self._server
            )
            logger.info("Using generated gRPC stubs")
        except ImportError as exc:
            # Stubs are shipped in this package.  Keep the error explicit if a
            # broken/partial installation omits them instead of silently
            # reporting a running-but-unusable gRPC endpoint.
            await self._cleanup_failed_start()
            raise RuntimeError(
                "Bundled gRPC protobuf stubs are unavailable; reinstall crabcode-gateway"
            ) from exc

        try:
            from grpc_health.v1 import health, health_pb2, health_pb2_grpc
        except ImportError as exc:
            await self._cleanup_failed_start()
            raise RuntimeError(
                "grpcio-health-checking is required for the gRPC gateway"
            ) from exc

        health_servicer = health.HealthServicer()
        health_pb2_grpc.add_HealthServicer_to_server(health_servicer, self._server)
        health_servicer.set(
            "", health_pb2.HealthCheckResponse.SERVING
        )

        try:
            self._bound_port = self._server.add_insecure_port(f"{host}:{port}")
        except Exception:
            await self._cleanup_failed_start()
            raise
        if not self._bound_port:
            await self._cleanup_failed_start()
            raise OSError(f"Unable to bind gRPC listener on {host}:{port}")
        try:
            await self._server.start()
        except Exception:
            await self._cleanup_failed_start()
            raise
        self._running = True
        logger.info("gRPC server listening on %s:%d", host, self._bound_port)

    async def _cleanup_failed_start(self) -> None:
        server = self._server
        self._server = None
        self._bound_port = None
        self._running = False
        if server is not None:
            await server.stop(grace=0)

    async def stop(self) -> None:
        server = self._server
        self._server = None
        self._running = False
        self._bound_port = None
        if server:
            await server.stop(grace=5)
            logger.info("gRPC server stopped")


class _GeneratedStubServicer:
    """Wrapper that delegates to _CrabCodeServicer using generated stub interfaces."""

    def __init__(self, servicer: _CrabCodeServicer) -> None:
        self._servicer = servicer

    async def SendMessage(self, request, context):
        req_dict = {
            "text": request.text,
            "max_turns": request.max_turns,
            "session_id": request.session_id,
        }
        async for event in self._servicer.SendMessage(req_dict, context):
            yield self._proto_event(event)

    async def SubscribeEvents(self, request, context):
        req_dict = {"session_id": request.session_id}
        async for event in self._servicer.SubscribeEvents(req_dict, context):
            yield self._proto_event(event)

    async def SpawnAgent(self, request, context):
        req_dict = {
            "prompt": request.prompt,
            "subagent_type": request.subagent_type or "generalPurpose",
            "callback": request.callback,
            "session_id": request.session_id,
        }
        if request.HasField("name"):
            req_dict["name"] = request.name
        if request.HasField("model_profile"):
            req_dict["model_profile"] = request.model_profile
        result = await self._servicer.SpawnAgent(req_dict, context)
        from crabcode_gateway.grpc import crabcode_pb2 as _pb2
        return _pb2.SpawnAgentResponse(**result)

    async def GetAgent(self, request, context):
        result = await self._servicer.GetAgent({
            "agent_id": request.agent_id,
            "session_id": request.session_id,
        }, context)
        from crabcode_gateway.grpc import crabcode_pb2
        return crabcode_pb2.AgentSnapshotProto(**result)

    async def ListAgents(self, request, context):
        result = await self._servicer.ListAgents({"session_id": request.session_id}, context)
        from crabcode_gateway.grpc import crabcode_pb2
        return crabcode_pb2.ListAgentsResponse(**result)

    async def CancelAgent(self, request, context):
        result = await self._servicer.CancelAgent({
            "agent_id": request.agent_id,
            "session_id": request.session_id,
        }, context)
        from crabcode_gateway.grpc import crabcode_pb2
        return crabcode_pb2.CancelAgentResponse(**result)

    async def WaitAgent(self, request, context):
        req_dict = {
            "agent_ids": list(request.agent_ids),
            "session_id": request.session_id,
        }
        if request.HasField("timeout_ms"):
            req_dict["timeout_ms"] = request.timeout_ms
        result = await self._servicer.WaitAgent(req_dict, context)
        from crabcode_gateway.grpc import crabcode_pb2
        return crabcode_pb2.AgentSnapshotProto(**result)

    async def RespondPermission(self, request, context):
        await self._servicer.RespondPermission({
            "tool_use_id": request.tool_use_id,
            "allowed": request.allowed,
            "always_allow": request.always_allow,
            "agent_id": request.agent_id if request.HasField("agent_id") else None,
            "session_id": request.session_id,
        }, context)
        from crabcode_gateway.grpc import crabcode_pb2
        return crabcode_pb2.Empty()

    async def RespondChoice(self, request, context):
        await self._servicer.RespondChoice({
            "tool_use_id": request.tool_use_id,
            "selected": list(request.selected),
            "cancelled": request.cancelled,
            "agent_id": request.agent_id if request.HasField("agent_id") else None,
            "session_id": request.session_id,
        }, context)
        from crabcode_gateway.grpc import crabcode_pb2
        return crabcode_pb2.Empty()

    async def ListModels(self, request, context):
        result = await self._servicer.ListModels({"session_id": request.session_id}, context)
        from crabcode_gateway.grpc import crabcode_pb2
        return crabcode_pb2.ListModelsResponse(**result)

    async def SwitchModel(self, request, context):
        await self._servicer.SwitchModel({
            "name": request.name,
            "session_id": request.session_id,
        }, context)
        from crabcode_gateway.grpc import crabcode_pb2
        return crabcode_pb2.Empty()

    async def SwitchMode(self, request, context):
        await self._servicer.SwitchMode({
            "mode": request.mode,
            "session_id": request.session_id,
        }, context)
        from crabcode_gateway.grpc import crabcode_pb2
        return crabcode_pb2.Empty()

    async def HealthCheck(self, request, context):
        result = await self._servicer.HealthCheck({}, context)
        from crabcode_gateway.grpc import crabcode_pb2
        return crabcode_pb2.HealthCheckResponse(**result)

    @staticmethod
    def _proto_event(event_dict: dict):
        from crabcode_gateway.grpc import crabcode_pb2
        return crabcode_pb2.CoreEventProto(**event_dict)
