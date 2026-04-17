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
from typing import Any

import uvicorn
from fastapi import FastAPI

from crabcode_core.logging_utils import get_logger
from crabcode_gateway.event_bus import EventBus
from crabcode_gateway.middleware import register_middleware
from crabcode_gateway.routes import agent, config, event, health, permission, session, snapshot

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
        cors_origins: list[str] | None = None,
        log_level: str = "info",
    ) -> None:
        self.host = host
        self.port = port
        self.grpc_port = grpc_port
        self.password = password
        self.cors_origins = cors_origins
        self.log_level = log_level

        self._app: FastAPI | None = None
        self._http_server: uvicorn.Server | None = None
        self._grpc_adapter: Any = None
        self._event_bus = EventBus()

    def build_app(self) -> FastAPI:
        """Build and configure the FastAPI application."""
        app = FastAPI(
            title="crabcode",
            version="0.1.0",
            description="CrabCode Gateway API",
        )

        # App-level state shared across routes
        app.state.sessions: dict[str, Any] = {}
        app.state.default_session_id: str | None = None
        app.state.event_bus = self._event_bus
        app.state.client_contexts: dict[str, Any] = {}

        # Middleware stack
        register_middleware(
            app,
            password=self.password,
            cors_origins=self.cors_origins,
        )

        # Routes
        app.include_router(health.router)
        app.include_router(session.router)
        app.include_router(agent.router)
        app.include_router(permission.router)
        app.include_router(config.router)
        app.include_router(event.router)
        app.include_router(snapshot.router)

        self._app = app
        return app

    async def start(self) -> None:
        """Start HTTP and optionally gRPC servers."""
        if self._app is None:
            self.build_app()

        # Start HTTP server
        config = uvicorn.Config(
            app=self._app,
            host=self.host,
            port=self.port,
            log_level=self.log_level,
            loop="asyncio",
        )
        self._http_server = uvicorn.Server(config)

        # Start gRPC server if configured
        if self.grpc_port is not None:
            try:
                from crabcode_gateway.grpc_.server import GrpcAdapter

                self._grpc_adapter = GrpcAdapter(self._app.state)
                await self._grpc_adapter.start(self.host, self.grpc_port)
            except Exception:
                logger.warning("Failed to start gRPC server", exc_info=True)
                self._grpc_adapter = None

        logger.info("CrabCode Gateway starting on %s:%d", self.host, self.port)
        await self._http_server.serve()

    async def start_background(self) -> None:
        """Start HTTP server in the background (non-blocking).

        Unlike ``start()``, this returns immediately so the caller
        can proceed (e.g. to start an ACP agent on stdio).
        """
        if self._app is None:
            self.build_app()

        config = uvicorn.Config(
            app=self._app,
            host=self.host,
            port=self.port,
            log_level=self.log_level,
            loop="asyncio",
        )
        self._http_server = uvicorn.Server(config)

        logger.info("CrabCode Gateway starting (background) on %s:%d", self.host, self.port)
        asyncio.ensure_future(self._http_server.serve())

    async def stop(self) -> None:
        """Gracefully stop all servers."""
        if self._http_server:
            self._http_server.should_exit = True
            await self._http_server.shutdown()

        if self._grpc_adapter:
            await self._grpc_adapter.stop()

        # Close all sessions
        if self._app:
            sessions: dict = self._app.state.sessions
            for sid, s in list(sessions.items()):
                try:
                    await s.close()
                except Exception:
                    logger.warning("Failed to close session %s", sid, exc_info=True)
            sessions.clear()

        logger.info("CrabCode Gateway stopped")

    @property
    def is_running(self) -> bool:
        return self._http_server is not None and not self._http_server.should_exit


def run_server(
    host: str = "127.0.0.1",
    port: int = 4096,
    grpc_port: int | None = None,
    password: str | None = None,
    cors_origins: list[str] | None = None,
    log_level: str = "info",
) -> None:
    """Synchronous entry point for the gateway server."""
    server = GatewayServer(
        host=host,
        port=port,
        grpc_port=grpc_port,
        password=password,
        cors_origins=cors_origins,
        log_level=log_level,
    )
    asyncio.run(server.start())
