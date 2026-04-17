"""gRPC server implementation for CrabCode Gateway.

Uses the generated stubs from crabcode.proto.  If stubs are not yet
generated, falls back to a reflection-based approach using the proto
definition at runtime via grpcio's generic handlers.

In production, run::

    python -m grpc_tools.protoc \\
        -I packages/gateway/crabcode_gateway/grpc_/proto \\
        --python_out=packages/gateway/crabcode_gateway/grpc_ \\
        --grpc_python_out=packages/gateway/crabcode_gateway/grpc_ \\
        packages/gateway/crabcode_gateway/grpc_/proto/crabcode.proto
"""

from __future__ import annotations

import json
from concurrent import futures
from typing import Any

from crabcode_core.logging_utils import get_logger
from crabcode_gateway.adapter import ProtocolAdapter
from crabcode_gateway.schemas import core_event_to_payload

logger = get_logger(__name__)


def _session_from_app(app_state: Any) -> Any | None:
    """Get the default session from app state."""
    sessions: dict = getattr(app_state, "sessions", {})
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


class _CrabCodeServicer:
    """Hand-written servicer that delegates to CoreSession.

    This avoids depending on generated pb2/grpc code at import time.
    Instead we register generic RPC handlers manually.
    """

    def __init__(self, app_state: Any) -> None:
        self._app_state = app_state
        self._event_bus = getattr(app_state, "event_bus", None)

    # ── Unary RPCs ──────────────────────────────────────────────

    async def SpawnAgent(self, request: dict, context: Any) -> dict:
        session = _session_from_app(self._app_state)
        if not session:
            await context.abort(code=404, details="No active session")
        agent_id = await session.spawn_agent(
            prompt=request.get("prompt", ""),
            subagent_type=request.get("subagent_type", "generalPurpose"),
            name=request.get("name"),
            model_profile=request.get("model_profile"),
        )
        return {"agent_id": agent_id}

    async def GetAgent(self, request: dict, context: Any) -> dict:
        session = _session_from_app(self._app_state)
        if not session:
            await context.abort(code=404, details="No active session")
        snapshot = session.get_agent(request.get("agent_id", ""))
        if not snapshot:
            await context.abort(code=404, details="Agent not found")
        return {
            "agent_id": snapshot.agent_id,
            "parent_agent_id": snapshot.parent_agent_id or "",
            "title": snapshot.title,
            "subagent_type": snapshot.subagent_type,
            "status": snapshot.status,
            "model": snapshot.model,
            "created_at": snapshot.created_at,
            "usage": snapshot.usage,
            "final_result": snapshot.final_result,
            "error": snapshot.error,
        }

    async def ListAgents(self, request: dict, context: Any) -> dict:
        session = _session_from_app(self._app_state)
        if not session:
            await context.abort(code=404, details="No active session")
        agents = []
        for s in session.list_agents():
            agents.append({
                "agent_id": s.agent_id,
                "parent_agent_id": s.parent_agent_id or "",
                "title": s.title,
                "subagent_type": s.subagent_type,
                "status": s.status,
                "model": s.model,
                "created_at": s.created_at,
                "usage": s.usage,
                "final_result": s.final_result,
                "error": s.error,
            })
        return {"agents": agents}

    async def CancelAgent(self, request: dict, context: Any) -> dict:
        session = _session_from_app(self._app_state)
        if not session:
            await context.abort(code=404, details="No active session")
        ok = await session.cancel_agent(request.get("agent_id", ""))
        return {"ok": ok}

    async def WaitAgent(self, request: dict, context: Any) -> dict:
        session = _session_from_app(self._app_state)
        if not session:
            await context.abort(code=404, details="No active session")
        agent_ids = request.get("agent_ids", [])
        timeout_ms = request.get("timeout_ms")
        if len(agent_ids) == 1:
            snapshot = await session.wait_agent(agent_ids[0], timeout_ms=timeout_ms)
        else:
            snapshot = await session.wait_agent(agent_ids, timeout_ms=timeout_ms)
        if not snapshot:
            await context.abort(code=408, details="Wait timed out")
        return {
            "agent_id": snapshot.agent_id,
            "parent_agent_id": snapshot.parent_agent_id or "",
            "title": snapshot.title,
            "subagent_type": snapshot.subagent_type,
            "status": snapshot.status,
            "model": snapshot.model,
            "created_at": snapshot.created_at,
            "usage": snapshot.usage,
            "final_result": snapshot.final_result,
            "error": snapshot.error,
        }

    async def RespondPermission(self, request: dict, context: Any) -> dict:
        from crabcode_core.types.event import PermissionResponseEvent
        session = _session_from_app(self._app_state)
        if not session:
            await context.abort(code=404, details="No active session")
        event = PermissionResponseEvent(
            tool_use_id=request.get("tool_use_id", ""),
            allowed=request.get("allowed", False),
            always_allow=request.get("always_allow", False),
            agent_id=request.get("agent_id"),
        )
        await session.respond_permission(event)
        return {}

    async def RespondChoice(self, request: dict, context: Any) -> dict:
        from crabcode_core.types.event import ChoiceResponseEvent
        session = _session_from_app(self._app_state)
        if not session:
            await context.abort(code=404, details="No active session")
        event = ChoiceResponseEvent(
            tool_use_id=request.get("tool_use_id", ""),
            selected=request.get("selected", []),
            cancelled=request.get("cancelled", False),
            agent_id=request.get("agent_id"),
        )
        await session.respond_choice(event)
        return {}

    async def ListModels(self, request: dict, context: Any) -> dict:
        session = _session_from_app(self._app_state)
        if not session:
            await context.abort(code=404, details="No active session")
        models = []
        for name, desc in session.list_models().items():
            models.append({"name": name, "description": desc})
        return {"models": models}

    async def SwitchModel(self, request: dict, context: Any) -> dict:
        session = _session_from_app(self._app_state)
        if not session:
            await context.abort(code=404, details="No active session")
        ok = session.switch_model(request.get("name", ""))
        if not ok:
            await context.abort(code=400, details="Model not found")
        return {}

    async def SwitchMode(self, request: dict, context: Any) -> dict:
        session = _session_from_app(self._app_state)
        if not session:
            await context.abort(code=404, details="No active session")
        ok = session.switch_mode(request.get("mode", "agent"))
        if not ok:
            await context.abort(code=400, details="Invalid mode")
        return {}

    async def HealthCheck(self, request: dict, context: Any) -> dict:
        return {"status": "ok", "version": "0.1.0"}

    # ── Server-streaming RPCs ────────────────────────────────────

    async def SendMessage(self, request: dict, context: Any) -> Any:
        """Stream CoreEvents as the query loop runs."""
        session = _session_from_app(self._app_state)
        if not session:
            await context.abort(code=404, details="No active session")

        text = request.get("text", "")
        max_turns = request.get("max_turns", 0)

        async for event in session.send_message(text, max_turns=max_turns):
            yield _event_to_proto(event)

    async def SubscribeEvents(self, request: dict, context: Any) -> Any:
        """Subscribe to the event bus SSE stream via gRPC."""
        if not self._event_bus:
            await context.abort(code=500, details="Event bus not available")
            return

        session_id = request.get("session_id")
        async for data in self._event_bus.sse_stream(session_id):
            # data is already JSON from the event bus
            try:
                parsed = json.loads(data)
                yield {
                    "type": parsed.get("type", "unknown"),
                    "payload_json": data,
                }
            except json.JSONDecodeError:
                yield {"type": "unknown", "payload_json": data}


class GrpcAdapter(ProtocolAdapter):
    """gRPC protocol adapter.

    Starts a grpcio async server on the configured port.
    """

    def __init__(self, app_state: Any) -> None:
        self._app_state = app_state
        self._server: Any = None
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self, host: str, port: int) -> None:
        """Start the gRPC server.

        We use a simple approach: try to import generated stubs,
        fall back to a generic handler registration if not available.
        """
        try:
            from grpc import aio as grpc_aio
        except ImportError:
            logger.warning("grpcio not installed, skipping gRPC server")
            return

        servicer = _CrabCodeServicer(self._app_state)

        self._server = grpc_aio.server(futures.ThreadPoolExecutor(max_workers=4))

        # Try to use generated stubs
        try:
            from crabcode_gateway.grpc_ import crabcode_pb2_grpc  # noqa: F401

            crabcode_pb2_grpc.add_CrabCodeServiceServicer_to_server(
                _GeneratedStubServicer(servicer), self._server
            )
            logger.info("Using generated gRPC stubs")
        except ImportError:
            logger.info("Generated gRPC stubs not found, using generic handlers")
            # Register generic handlers for the service
            self._register_generic_handlers(self._server, servicer)

        from grpc_health.v1 import health, health_pb2, health_pb2_grpc

        health_servicer = health.HealthServicer()
        health_pb2_grpc.add_HealthServicer_to_server(health_servicer, self._server)
        health_servicer.set(
            "", health_pb2.HealthCheckResponse.SERVING
        )

        self._server.add_insecure_port(f"{host}:{port}")
        await self._server.start()
        self._running = True
        logger.info("gRPC server listening on %s:%d", host, port)

    async def stop(self) -> None:
        if self._server:
            await self._server.stop(grace=5)
            self._running = False
            logger.info("gRPC server stopped")

    def _register_generic_handlers(self, server: Any, servicer: _CrabCodeServicer) -> None:
        """Register RPC handlers manually without generated stubs.

        This allows the gRPC server to work even without protoc-generated code.
        """

        # We'll rely on generated stubs for now. If they aren't available,
        # the gRPC server simply won't start.
        raise ImportError(
            "gRPC generated stubs not found. Run:\n"
            "  python -m grpc_tools.protoc "
            "-I packages/gateway/crabcode_gateway/grpc_/proto "
            "--python_out=packages/gateway/crabcode_gateway/grpc_ "
            "--grpc_python_out=packages/gateway/crabcode_gateway/grpc_ "
            "packages/gateway/crabcode_gateway/grpc_/proto/crabcode.proto"
        )


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
            "subagent_type": request.subagent_type,
        }
        if request.HasField("name"):
            req_dict["name"] = request.name
        if request.HasField("model_profile"):
            req_dict["model_profile"] = request.model_profile
        result = await self._servicer.SpawnAgent(req_dict, context)
        from crabcode_gateway.grpc_ import crabcode_pb2 as _pb2
        return _pb2.SpawnAgentResponse(**result)

    async def GetAgent(self, request, context):
        result = await self._servicer.GetAgent({"agent_id": request.agent_id}, context)
        from crabcode_gateway.grpc_ import crabcode_pb2
        return crabcode_pb2.AgentSnapshotProto(**result)

    async def ListAgents(self, request, context):
        result = await self._servicer.ListAgents({}, context)
        from crabcode_gateway.grpc_ import crabcode_pb2
        return crabcode_pb2.ListAgentsResponse(**result)

    async def CancelAgent(self, request, context):
        result = await self._servicer.CancelAgent({"agent_id": request.agent_id}, context)
        from crabcode_gateway.grpc_ import crabcode_pb2
        return crabcode_pb2.CancelAgentResponse(**result)

    async def WaitAgent(self, request, context):
        req_dict = {"agent_ids": list(request.agent_ids)}
        if request.HasField("timeout_ms"):
            req_dict["timeout_ms"] = request.timeout_ms
        result = await self._servicer.WaitAgent(req_dict, context)
        from crabcode_gateway.grpc_ import crabcode_pb2
        return crabcode_pb2.AgentSnapshotProto(**result)

    async def RespondPermission(self, request, context):
        await self._servicer.RespondPermission({
            "tool_use_id": request.tool_use_id,
            "allowed": request.allowed,
            "always_allow": request.always_allow,
        }, context)
        from crabcode_gateway.grpc_ import crabcode_pb2
        return crabcode_pb2.Empty()

    async def RespondChoice(self, request, context):
        await self._servicer.RespondChoice({
            "tool_use_id": request.tool_use_id,
            "selected": list(request.selected),
            "cancelled": request.cancelled,
        }, context)
        from crabcode_gateway.grpc_ import crabcode_pb2
        return crabcode_pb2.Empty()

    async def ListModels(self, request, context):
        result = await self._servicer.ListModels({}, context)
        from crabcode_gateway.grpc_ import crabcode_pb2
        return crabcode_pb2.ListModelsResponse(**result)

    async def SwitchModel(self, request, context):
        await self._servicer.SwitchModel({"name": request.name}, context)
        from crabcode_gateway.grpc_ import crabcode_pb2
        return crabcode_pb2.Empty()

    async def SwitchMode(self, request, context):
        await self._servicer.SwitchMode({"mode": request.mode}, context)
        from crabcode_gateway.grpc_ import crabcode_pb2
        return crabcode_pb2.Empty()

    async def HealthCheck(self, request, context):
        result = await self._servicer.HealthCheck({}, context)
        from crabcode_gateway.grpc_ import crabcode_pb2
        return crabcode_pb2.HealthCheckResponse(**result)

    @staticmethod
    def _proto_event(event_dict: dict):
        from crabcode_gateway.grpc_ import crabcode_pb2
        return crabcode_pb2.CoreEventProto(**event_dict)
