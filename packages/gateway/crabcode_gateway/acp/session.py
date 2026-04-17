"""ACP session manager — tracks ACP-side session state.

Each ACP session wraps an internal CrabCode session (created via the
Gateway REST API).  The manager holds the mapping between ACP session
IDs and their associated state (cwd, model, mode, MCP servers).
"""

from __future__ import annotations

import time

import httpx
from acp.exceptions import RequestError
from acp.schema import HttpMcpServer, McpServerStdio, SseMcpServer

from crabcode_core.logging_utils import get_logger
from crabcode_gateway.acp.types import ACPConfig, ACPSessionState, ModelSelection

logger = get_logger(__name__)


class ACPSessionManager:
    """Manages ACP session lifecycle backed by the Gateway HTTP API."""

    def __init__(self, config: ACPConfig) -> None:
        self._config = config
        self._sessions: dict[str, ACPSessionState] = {}
        self._client = httpx.AsyncClient(base_url=config.base_url, timeout=60.0)

    # ── CRUD ────────────────────────────────────────────────────

    async def create(
        self,
        cwd: str,
        mcp_servers: list[McpServerStdio | HttpMcpServer | SseMcpServer],
        model: ModelSelection | None = None,
    ) -> ACPSessionState:
        """Create a new session via the Gateway API and store ACP state."""
        resp = await self._client.post("/session/new", json={"cwd": cwd})
        resp.raise_for_status()
        data = resp.json()
        session_id = data["session_id"]

        state = ACPSessionState(
            id=session_id,
            cwd=cwd,
            mcp_servers=mcp_servers,
            created_at=time.time(),
            model=model,
        )
        self._sessions[session_id] = state
        logger.info("acp_session_created", extra={"session_id": session_id})
        return state

    async def load(
        self,
        session_id: str,
        cwd: str,
        mcp_servers: list[McpServerStdio | HttpMcpServer | SseMcpServer],
        model: ModelSelection | None = None,
    ) -> ACPSessionState:
        """Load an existing session via the Gateway API."""
        resp = await self._client.post("/session/resume", json={"session_id": session_id})
        resp.raise_for_status()

        state = ACPSessionState(
            id=session_id,
            cwd=cwd,
            mcp_servers=mcp_servers,
            created_at=time.time(),
            model=model,
        )
        self._sessions[session_id] = state
        logger.info("acp_session_loaded", extra={"session_id": session_id})
        return state

    def try_get(self, session_id: str) -> ACPSessionState | None:
        """Return session state if it exists, else None."""
        return self._sessions.get(session_id)

    def get(self, session_id: str) -> ACPSessionState:
        """Return session state or raise ACP RequestError."""
        state = self._sessions.get(session_id)
        if not state:
            raise RequestError(code=-32602, message=f"Session not found: {session_id}")
        return state

    def set_model(self, session_id: str, model: ModelSelection) -> None:
        state = self.get(session_id)
        state.model = model

    def get_model(self, session_id: str) -> ModelSelection | None:
        return self.get(session_id).model

    def set_variant(self, session_id: str, variant: str | None) -> None:
        self.get(session_id).variant = variant

    def get_variant(self, session_id: str) -> str | None:
        return self.get(session_id).variant

    def set_mode(self, session_id: str, mode_id: str) -> None:
        self.get(session_id).mode_id = mode_id

    def get_mode(self, session_id: str) -> str | None:
        return self.get(session_id).mode_id

    # ── HTTP helpers exposed to Agent ───────────────────────────

    @property
    def client(self) -> httpx.AsyncClient:
        return self._client

    async def aclose(self) -> None:
        await self._client.aclose()
