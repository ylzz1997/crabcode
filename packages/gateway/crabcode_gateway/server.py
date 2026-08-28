"""Gateway server — the main entry point.

Builds the FastAPI app, registers middleware and routes, and starts
both HTTP and gRPC servers.  Mirrors OpenCode's server.ts architecture:
  - Adapter pattern for multiple protocols
  - Middleware stack (auth → logger → cors → error)
  - Route groups (session, agent, config, event)
  - SSE + WebSocket for real-time events
  - EventBus for multi-subscriber broadcast
"""

from __future__ import annotations

import asyncio
import hmac
import os
import secrets
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI

from crabcode_core import VERSION
from crabcode_core.logging_utils import get_logger
from crabcode_core.types.config import GatewaySecuritySettings
from crabcode_gateway.auth import verify_password
from crabcode_gateway.event_bus import EventBus
from crabcode_gateway.middleware import register_middleware
from crabcode_gateway.routes import (
    agent,
    auth,
    config,
    document,
    event,
    health,
    peer,
    permission,
    schedule,
    session,
    snapshot,
    tasks,
    team,
    workspace,
)
from crabcode_gateway.session_registry import get_session_lock
from crabcode_gateway.task_registry import (
    ensure_task_state,
    mark_session_closing,
    shielded_cleanup_session,
    unmark_session_closing,
)

logger = get_logger(__name__)


class GatewayServer:
    """CrabCode Gateway server.

    Usage::

        server = GatewayServer(port=4096)
        await server.start()
        # ... later
        await server.stop()
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 4096,
        grpc_port: int | None = None,
        password: str | None = None,
        security_mode: str | None = None,
        password_hash: str | None = None,
        jwt_secret: str | None = None,
        authorized_keys_path: str | None = None,
        token_ttl_seconds: int = 900,
        cors_origins: list[str] | None = None,
        log_level: str = "info",
    ) -> None:
        self.host = host
        self.port = port
        self.grpc_port = grpc_port
        self.password = password
        self.security_mode = security_mode
        self.password_hash = password_hash
        self.jwt_secret = jwt_secret
        self.authorized_keys_path = authorized_keys_path
        self.token_ttl_seconds = token_ttl_seconds
        self.cors_origins = cors_origins
        self.log_level = log_level

        self._app: FastAPI | None = None
        self._http_server: uvicorn.Server | None = None
        self._http_task: asyncio.Task[Any] | None = None
        self._grpc_adapter: Any = None
        self._event_bus = EventBus()
        self._stop_task: asyncio.Task[Any] | None = None
        # Serialize start transitions with creation of the shared stop task.
        # The lock is held only during setup/task admission, never while an
        # HTTP server is serving requests.
        self._lifecycle_lock = asyncio.Lock()

    def build_app(self) -> FastAPI:
        """Build and configure the FastAPI application."""
        app = FastAPI(
            title="crabcode",
            version=VERSION,
            description="CrabCode Gateway API",
        )

        # App-level state shared across routes
        app.state.sessions: dict[str, Any] = {}
        app.state.default_session_id: str | None = None
        app.state.event_bus = self._event_bus
        app.state.client_contexts: dict[str, Any] = {}
        app.state.standalone_schedule_manager = None
        app.state.standalone_schedule_manager_lock = asyncio.Lock()
        from crabcode_core.config.manager import ConfigManager

        workspace_settings = ConfigManager(cwd=os.getcwd()).load_gateway_workspace()
        app.state.workspace_info = workspace.build_workspace_info(
            os.getcwd(),
            workspace_settings.browse_roots,
        )
        app.state.session_lock = asyncio.Lock()
        app.state.session_load_lock = asyncio.Lock()
        app.state.model_settings_lock = asyncio.Lock()
        ensure_task_state(app.state)
        app.state.gateway_closing = False
        # gRPC is a separate transport and cannot see FastAPI middleware; make
        # the same gateway credentials available to its adapter explicitly.
        app.state.gateway_username = "crabcode"
        mode = self.security_mode or (
            "password" if self.password or self.password_hash else "none"
        )
        if mode not in ("none", "password", "publickey", "mixed"):
            raise ValueError("security_mode must be none, password, publickey, or mixed")
        security = GatewaySecuritySettings(
            mode=mode,
            password=self.password or os.getenv("CRABCODE_GATEWAY_PASSWORD"),
            password_hash=self.password_hash,
            jwt_secret=self.jwt_secret or os.getenv("CRABCODE_GATEWAY_JWT_SECRET") or secrets.token_urlsafe(32),
            authorized_keys=self.authorized_keys_path or "~/.ssh/authorized_keys",
            token_ttl_seconds=self.token_ttl_seconds,
        )
        if mode in ("password", "mixed") and not (
            security.password or security.password_hash
        ):
            raise ValueError(f"Gateway security mode {mode!r} requires a password")
        if mode != "none" and (
            security.jwt_secret is None or len(security.jwt_secret.encode()) < 32
        ):
            raise ValueError("Gateway jwt_secret must be at least 32 bytes")
        if mode in ("publickey", "mixed") and not Path(
            security.authorized_keys
        ).expanduser().is_file():
            raise ValueError(
                f"Gateway authorized_keys file does not exist: {security.authorized_keys}"
            )
        self._security = security
        app.state.gateway_password = security.password
        app.state.gateway_security = security
        app.state.gateway_jwt_secret = security.jwt_secret
        app.state.auth_challenges: dict[str, int] = {}
        app.state.auth_failures: dict[str, list[float]] = {}
        app.state.verify_gateway_password = lambda value: (
            (security.password is not None and hmac.compare_digest(value, security.password))
            or (security.password_hash is not None and verify_password(value, security.password_hash))
        )
        if mode == "none" and self.host not in ("127.0.0.1", "localhost", "::1"):
            logger.warning(
                "Gateway is listening on %s without authentication", self.host
            )

        # Middleware stack
        register_middleware(
            app,
            password=security.password,
            password_hash=security.password_hash,
            security_mode=security.mode,
            jwt_secret=security.jwt_secret,
            cors_origins=self.cors_origins,
        )

        # Routes
        app.include_router(health.router)
        app.include_router(session.router)
        app.include_router(agent.router)
        app.include_router(permission.router)
        app.include_router(schedule.router)
        app.include_router(config.router)
        app.include_router(auth.router)
        app.include_router(event.router)
        app.include_router(snapshot.router)
        app.include_router(tasks.router)
        app.include_router(peer.router)
        app.include_router(team.router)
        app.include_router(workspace.router)
        app.include_router(document.router)

        self._app = app
        return app

    async def _start_standalone_schedule_manager(self) -> None:
        """Start the scheduler that covers jobs when no chat is connected."""
        if self._app is None or self._app.state.standalone_schedule_manager is not None:
            return
        from crabcode_core.config.manager import ConfigManager
        from crabcode_core.schedule.manager import ScheduleManager

        cwd = os.getcwd()
        manager = ScheduleManager(
            settings=ConfigManager(cwd=cwd).load().schedule,
            cwd=cwd,
            session_id="",
        )
        await manager.start()
        self._app.state.standalone_schedule_manager = manager

    async def start(self) -> None:
        """Start HTTP and optionally gRPC servers."""
        async with self._lifecycle_lock:
            await self._wait_for_previous_stop()
            self._ensure_not_running()
            if self._app is None:
                self.build_app()

            await self._start_standalone_schedule_manager()

            async with get_session_lock(self._app.state):
                self._app.state.gateway_closing = False

            # Start HTTP server
            config = uvicorn.Config(
                app=self._app,
                host=self.host,
                port=self.port,
                log_level=self.log_level,
                loop="asyncio",
            )
            self._http_server = uvicorn.Server(config)

            await self._start_grpc()

        logger.info("CrabCode Gateway starting on %s:%d", self.host, self.port)
        try:
            await self._http_server.serve()
        except asyncio.CancelledError:
            # ``serve`` can be cancelled by an embedding application.  Run
            # the same fenced cleanup path as an explicit stop before
            # propagating cancellation.
            await self.stop()
            raise
        except Exception:
            await self.stop()
            raise
        else:
            # Uvicorn can terminate without an explicit GatewayServer.stop
            # (for example after an internal startup/runtime failure).  Do
            # not leave the session registry and gRPC adapter alive in that
            # case.
            await self.stop()

    async def start_background(self) -> None:
        """Start HTTP server in the background (non-blocking).

        Unlike ``start()``, this returns immediately so the caller
        can proceed (e.g. to start an ACP agent on stdio).
        """
        async with self._lifecycle_lock:
            await self._wait_for_previous_stop()
            self._ensure_not_running()
            if self._app is None:
                self.build_app()

            await self._start_standalone_schedule_manager()

            async with get_session_lock(self._app.state):
                self._app.state.gateway_closing = False

            await self._start_grpc()

            config = uvicorn.Config(
                app=self._app,
                host=self.host,
                port=self.port,
                log_level=self.log_level,
                loop="asyncio",
            )
            self._http_server = uvicorn.Server(config)

            logger.info("CrabCode Gateway starting (background) on %s:%d", self.host, self.port)
            self._http_task = asyncio.create_task(self._serve_background())

    async def _wait_for_previous_stop(self) -> None:
        """Wait for an earlier stop task before reopening the gateway."""
        task = self._stop_task
        if task is not None and not task.done():
            await asyncio.shield(task)
        # Surface an internal cleanup failure instead of starting with a
        # partially torn-down registry/resources.
        if task is not None:
            await task

    def _ensure_not_running(self) -> None:
        """Reject duplicate starts while a transport is still active."""
        if self._http_task is not None and not self._http_task.done():
            raise RuntimeError("Gateway server is already running")
        if self._http_server is not None and not self._http_server.should_exit:
            raise RuntimeError("Gateway server is already running")

    async def _serve_background(self) -> None:
        """Run the background HTTP server and fence cleanup on termination.

        ``start_background`` returns the task handle to the embedding process,
        so that process can cancel it directly.  A raw ``uvicorn.serve`` task
        would bypass :meth:`stop` in that case and leave sessions and the gRPC
        adapter alive.  Keep the same cleanup contract as foreground
        ``start()`` for both cancellation and unexpected server failures.
        """
        try:
            assert self._http_server is not None
            await self._http_server.serve()
        except asyncio.CancelledError:
            # This task is the HTTP task awaited by ``_stop_impl`` during a
            # normal shutdown.  Detach its handle before entering cleanup so
            # cancellation of the task itself cannot create a stop -> await
            # HTTP task -> stop cycle.  If another stop is already draining,
            # that owner will finish the cleanup after this task terminates.
            if self._http_task is asyncio.current_task():
                self._http_task = None
            if self._stop_task is None or self._stop_task.done():
                await self.stop()
            raise
        except Exception:
            if self._http_task is asyncio.current_task():
                self._http_task = None
            if self._stop_task is None or self._stop_task.done():
                await self.stop()
            raise
        else:
            # A server can also return normally after an internal shutdown or
            # an embedding caller setting ``should_exit`` directly. Apply the
            # same cleanup contract unless another stop already owns it.
            if self._http_task is asyncio.current_task():
                self._http_task = None
            if self._stop_task is None or self._stop_task.done():
                await self.stop()

    async def _start_grpc(self) -> None:
        """Start the optional gRPC adapter for both foreground/background modes."""
        if self.grpc_port is None or self._app is None:
            return
        try:
            from crabcode_gateway.grpc.server import GrpcAdapter

            self._grpc_adapter = GrpcAdapter(
                self._app.state,
                password=self._security.password,
                password_hash=self._security.password_hash,
                security_mode=self._security.mode,
                jwt_secret=self._security.jwt_secret,
            )
            await self._grpc_adapter.start(self.host, self.grpc_port)
        except Exception:
            logger.warning("Failed to start gRPC server", exc_info=True)
            self._grpc_adapter = None

    async def stop(self) -> None:
        """Gracefully stop all servers."""
        # Keep one cleanup operation for concurrent callers and shield it from
        # cancellation.  A cancelled shutdown must not leave sessions in the
        # registry with detached query tasks still running.
        async with self._lifecycle_lock:
            if self._stop_task is None or self._stop_task.done():
                self._stop_task = asyncio.create_task(self._stop_impl())
            stop_task = self._stop_task
        cancelled = False
        try:
            # Keep draining through repeated cancellation requests.  Shutdown
            # is a resource-release operation, so returning before the shared
            # child finishes can leak HTTP/gRPC servers and CoreSessions.
            while not stop_task.done():
                try:
                    await asyncio.shield(stop_task)
                except asyncio.CancelledError:
                    cancelled = True
            await stop_task
        except asyncio.CancelledError:
            cancelled = True
        if cancelled:
            raise asyncio.CancelledError

    async def _stop_impl(self) -> None:
        """Perform the actual graceful shutdown."""
        app = self._app

        # Fence all route handlers before stopping either transport.  The
        # marker is set while holding the same lock used by new/send/resume
        # and by archive, so no operation can resolve a session after this
        # snapshot and then register new work for it.
        pending: list[tuple[str, Any]] = []
        if app:
            async with get_session_lock(app.state):
                app.state.gateway_closing = True
                sessions: dict = app.state.sessions
                pending = list(sessions.items())
                for sid, _session in pending:
                    mark_session_closing(app.state, sid)
                    close_session_events = getattr(app.state.event_bus, "close_session", None)
                    if callable(close_session_events):
                        close_session_events(sid, _session)
                sessions.clear()
                app.state.default_session_id = None
                app.state.client_contexts.clear()

        if self._http_server:
            self._http_server.should_exit = True
            try:
                await self._http_server.shutdown()
            except Exception:
                logger.warning("Failed to shut down HTTP server cleanly", exc_info=True)

        http_task = self._http_task
        if http_task and http_task is not asyncio.current_task():
            try:
                await http_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.warning("HTTP server task failed during shutdown", exc_info=True)
        if self._http_task is http_task:
            self._http_task = None

        if self._grpc_adapter:
            try:
                await self._grpc_adapter.stop()
            except Exception:
                logger.warning("Failed to shut down gRPC server cleanly", exc_info=True)

        # Close all sessions
        if app:
            schedule_manager = getattr(app.state, "standalone_schedule_manager", None)
            if schedule_manager is not None:
                try:
                    await schedule_manager.close()
                except Exception:
                    logger.warning("Failed to close standalone schedule manager", exc_info=True)
                finally:
                    app.state.standalone_schedule_manager = None
            for sid, session in pending:
                try:
                    # The marker was installed under the registry lock above;
                    # keep it until CoreSession.close() has completed.
                    await shielded_cleanup_session(app.state, sid, session)
                except Exception:
                    logger.warning("Failed to close session %s", sid, exc_info=True)
                finally:
                    # ``shielded_cleanup_session`` normally clears this.  The
                    # explicit discard also handles a partially initialized
                    # or custom session whose close path raises unexpectedly.
                    unmark_session_closing(app.state, sid)
            close_all_events = getattr(app.state.event_bus, "close_all", None)
            if callable(close_all_events):
                close_all_events()

        logger.info("CrabCode Gateway stopped")

    @property
    def is_running(self) -> bool:
        return self._http_server is not None and not self._http_server.should_exit


def run_server(
    host: str = "127.0.0.1",
    port: int = 4096,
    grpc_port: int | None = None,
    password: str | None = None,
    security_mode: str | None = None,
    password_hash: str | None = None,
    jwt_secret: str | None = None,
    authorized_keys_path: str | None = None,
    token_ttl_seconds: int = 900,
    cors_origins: list[str] | None = None,
    log_level: str = "info",
) -> None:
    """Synchronous entry point for the gateway server."""
    server = GatewayServer(
        host=host,
        port=port,
        grpc_port=grpc_port,
        password=password,
        security_mode=security_mode,
        password_hash=password_hash,
        jwt_secret=jwt_secret,
        authorized_keys_path=authorized_keys_path,
        token_ttl_seconds=token_ttl_seconds,
        cors_origins=cors_origins,
        log_level=log_level,
    )
    asyncio.run(server.start())
