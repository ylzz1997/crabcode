"""CrabCode ACP Agent implementation.

Implements the ACP Agent protocol by translating between ACP JSON-RPC
and CrabCode's internal Gateway REST API + EventBus.

Architecture mirrors OpenCode's agent.ts:
  - ACP client ↔ AgentSideConnection ↔ CrabCodeACPAgent ↔ httpx ↔ Gateway REST API
  - EventBus SSE → _event_listener → session_update() → ACP client
"""

from __future__ import annotations

import asyncio
from typing import Any

import acp
import acp.schema as S
from acp.exceptions import RequestError
from acp.schema import (
    AgentCapabilities,
    AllowedOutcome,
    AuthenticateResponse,
    AuthMethodAgent,
    ContentToolCallContent,
    FileEditToolCallContent,
    ForkSessionResponse,
    HttpMcpServer,
    Implementation,
    InitializeResponse,
    ListSessionsResponse,
    LoadSessionResponse,
    McpCapabilities,
    McpServerStdio,
    ModelInfo,
    NewSessionResponse,
    PermissionOption,
    PromptCapabilities,
    PromptResponse,
    ResumeSessionResponse,
    SseMcpServer,
    SessionCapabilities,
    SessionConfigOptionSelect,
    SessionConfigSelectOption,
    SessionForkCapabilities,
    SessionInfo,
    SessionListCapabilities,
    SessionMode,
    SessionModeState,
    SessionModelState,
    SessionResumeCapabilities,
    SetSessionConfigOptionResponse,
    SetSessionModeResponse,
    SetSessionModelResponse,
    TextContent,
    ToolCallProgress,
    ToolCallStart,
    Usage,
)

from crabcode_core.logging_utils import get_logger
from crabcode_gateway.acp.session import ACPSessionManager
from crabcode_gateway.acp.types import ACPConfig, ACPSessionState, ModelSelection, to_locations, to_tool_kind

logger = get_logger(__name__)

# ── Permission options ─────────────────────────────────────────

_PERMISSION_OPTIONS = [
    PermissionOption(option_id="once", kind="allow_once", name="Allow once"),
    PermissionOption(option_id="always", kind="allow_always", name="Always allow"),
    PermissionOption(option_id="reject", kind="reject_once", name="Reject"),
]


class CrabCodeACPAgent:
    """ACP Agent that bridges ACP protocol to CrabCode's Gateway API."""

    def __init__(self, config: ACPConfig) -> None:
        self._config = config
        self._session_mgr = ACPSessionManager(config)
        self._connection: Any = None  # set by on_connect
        self._event_task: asyncio.Task | None = None
        self._tool_starts: set[str] = set()

    # ── Connection lifecycle ────────────────────────────────────

    def on_connect(self, connection: Any) -> None:
        """Called by AgentSideConnection after wiring."""
        self._connection = connection
        self._event_task = asyncio.create_task(self._event_listener())

    async def _event_listener(self) -> None:
        """Subscribe to Gateway EventBus SSE and translate CoreEvents to ACP session_updates."""
        client = self._session_mgr.client
        try:
            async with client.stream("GET", "/event") as resp:
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    payload_str = line[len("data:"):].strip()
                    if not payload_str:
                        continue
                    try:
                        import json
                        payload = json.loads(payload_str)
                    except json.JSONDecodeError:
                        continue
                    try:
                        await self._handle_event_payload(payload)
                    except Exception:
                        logger.exception("acp_event_handler_error")
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("acp_event_listener_stopped")

    async def _handle_event_payload(self, payload: dict[str, Any]) -> None:
        """Translate a Gateway EventBus payload into ACP session_update(s)."""
        event_type = payload.get("type", "")
        session_id = payload.get("session_id", "")
        state = self._session_mgr.try_get(session_id)
        if not state:
            return

        if event_type == "permission_request":
            await self._handle_permission_request(session_id, payload)
        elif event_type == "tool_use":
            await self._handle_tool_use(session_id, payload)
        elif event_type == "tool_result":
            await self._handle_tool_result(session_id, payload)
        elif event_type == "stream_text":
            await self._push_agent_message_chunk(session_id, payload.get("text", ""))
        elif event_type == "thinking":
            await self._push_agent_thought_chunk(session_id, payload.get("text", ""))
        elif event_type == "turn_complete":
            # Turn completion is handled by prompt() return value
            pass

    async def _handle_permission_request(self, session_id: str, payload: dict[str, Any]) -> None:
        """Forward a CrabCode permission request to the ACP client."""
        tool_name = payload.get("tool_name", "")
        tool_use_id = payload.get("tool_use_id", "")
        tool_input = payload.get("tool_input", {})

        tool_kind = to_tool_kind(tool_name)
        locations = to_locations(tool_name, tool_input)

        tool_call = S.ToolCallUpdate(
            tool_call_id=tool_use_id,
            status="pending",
            title=tool_name,
            kind=tool_kind,
            raw_input=tool_input,
            locations=[S.ToolCallLocation(**loc) for loc in locations],
        )

        try:
            result = await self._connection.request_permission(
                options=_PERMISSION_OPTIONS,
                session_id=session_id,
                tool_call=tool_call,
            )
        except Exception:
            logger.exception("acp_request_permission_failed")
            await self._reply_permission(tool_use_id, session_id, "reject")
            return

        outcome = result.outcome if result else None
        if not outcome or not isinstance(outcome, AllowedOutcome) or outcome.outcome != "selected":
            await self._reply_permission(tool_use_id, session_id, "reject")
            return

        option_id = outcome.option_id if hasattr(outcome, "option_id") else "reject"
        await self._reply_permission(tool_use_id, session_id, option_id)

    async def _reply_permission(self, tool_use_id: str, session_id: str, reply: str) -> None:
        """Send a permission response back to the Gateway."""
        allowed = reply in ("once", "always")
        always = reply == "always"
        try:
            await self._session_mgr.client.post(
                "/permission/respond",
                json={
                    "tool_use_id": tool_use_id,
                    "allowed": allowed,
                    "always_allow": always,
                },
            )
        except Exception:
            logger.exception("acp_reply_permission_failed")

    async def _handle_tool_use(self, session_id: str, payload: dict[str, Any]) -> None:
        """Tool call started — push tool_call start notification."""
        tool_name = payload.get("tool_name", "")
        tool_use_id = payload.get("tool_use_id", "")
        tool_input = payload.get("tool_input", {})

        if tool_use_id in self._tool_starts:
            return
        self._tool_starts.add(tool_use_id)

        tool_kind = to_tool_kind(tool_name)
        locations = to_locations(tool_name, tool_input)

        update = ToolCallStart(
            session_update="tool_call",
            tool_call_id=tool_use_id,
            title=tool_name,
            kind=tool_kind,
            status="pending",
            locations=[S.ToolCallLocation(**loc) for loc in locations],
            raw_input=tool_input,
        )
        await self._connection.session_update(session_id=session_id, update=update)

    async def _handle_tool_result(self, session_id: str, payload: dict[str, Any]) -> None:
        """Tool call completed — push tool_call_update."""
        tool_name = payload.get("tool_name", "")
        tool_use_id = payload.get("tool_use_id", "")
        result_text = payload.get("result", "")
        is_error = payload.get("is_error", False)
        tool_input = payload.get("tool_input", {})

        self._tool_starts.discard(tool_use_id)
        tool_kind = to_tool_kind(tool_name)
        locations = to_locations(tool_name, tool_input)

        content: list[ContentToolCallContent | FileEditToolCallContent] = [
            ContentToolCallContent(type="content", content=TextContent(type="text", text=result_text)),
        ]

        # Add diff for edit tools
        if tool_kind == "edit":
            file_path = tool_input.get("filePath") or tool_input.get("file_path") or ""
            old_text = tool_input.get("oldString") or tool_input.get("old_string") or ""
            new_text = tool_input.get("newString") or tool_input.get("new_string") or tool_input.get("content") or ""
            content.append(
                FileEditToolCallContent(type="diff", path=file_path, old_text=old_text, new_text=new_text)
            )

        status = "failed" if is_error else "completed"

        update = ToolCallProgress(
            session_update="tool_call_update",
            tool_call_id=tool_use_id,
            title=tool_name,
            kind=tool_kind,
            status=status,
            content=content,
            locations=[S.ToolCallLocation(**loc) for loc in locations],
            raw_input=tool_input,
            raw_output={"output": result_text} if not is_error else {"error": result_text},
        )
        await self._connection.session_update(session_id=session_id, update=update)

    async def _push_agent_message_chunk(self, session_id: str, text: str) -> None:
        update = acp.update_agent_message_text(text)
        await self._connection.session_update(session_id=session_id, update=update)

    async def _push_agent_thought_chunk(self, session_id: str, text: str) -> None:
        update = acp.update_agent_thought_text(text)
        await self._connection.session_update(session_id=session_id, update=update)

    # ── ACP Agent interface ─────────────────────────────────────

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: S.ClientCapabilities | None = None,
        client_info: S.Implementation | None = None,
        **kwargs: Any,
    ) -> InitializeResponse:
        logger.info("acp_initialize", extra={"protocol_version": protocol_version})

        auth_method = AuthMethodAgent(
            id="crabcode-login",
            name="Login with CrabCode",
            description="Run `crabcode auth login` in the terminal",
        )

        return InitializeResponse(
            protocol_version=acp.PROTOCOL_VERSION,
            agent_capabilities=AgentCapabilities(
                load_session=True,
                mcp_capabilities=McpCapabilities(http=True, sse=True),
                prompt_capabilities=PromptCapabilities(embedded_context=True, image=True),
                session_capabilities=SessionCapabilities(
                    fork=SessionForkCapabilities(),
                    list=SessionListCapabilities(),
                    resume=SessionResumeCapabilities(),
                ),
            ),
            auth_methods=[auth_method],
            agent_info=Implementation(name="CrabCode", version="0.1.0"),
        )

    async def new_session(
        self,
        cwd: str,
        mcp_servers: list[McpServerStdio | HttpMcpServer | SseMcpServer] | None = None,
        **kwargs: Any,
    ) -> NewSessionResponse:
        model = self._config.default_model
        try:
            state = await self._session_mgr.create(cwd, mcp_servers or [], model)
        except Exception as e:
            raise RequestError(code=-32603, message=str(e))

        models_state = await self._build_models_state(state)
        modes_state = await self._build_modes_state(state)
        config_options = _build_config_options(models_state, modes_state)

        return NewSessionResponse(
            session_id=state.id,
            models=models_state,
            modes=modes_state,
            config_options=config_options,
        )

    async def load_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: list[McpServerStdio | HttpMcpServer | SseMcpServer] | None = None,
        **kwargs: Any,
    ) -> LoadSessionResponse | None:
        model = self._config.default_model
        try:
            state = await self._session_mgr.load(session_id, cwd, mcp_servers or [], model)
        except Exception as e:
            raise RequestError(code=-32603, message=str(e))

        models_state = await self._build_models_state(state)
        modes_state = await self._build_modes_state(state)
        config_options = _build_config_options(models_state, modes_state)

        # Replay messages for loaded session
        await self._replay_messages(state)

        # Push usage update
        await self._send_usage_update(state)

        return LoadSessionResponse(
            models=models_state,
            modes=modes_state,
            config_options=config_options,
        )

    async def list_sessions(
        self,
        cursor: str | None = None,
        cwd: str | None = None,
        **kwargs: Any,
    ) -> ListSessionsResponse:
        try:
            params: dict[str, Any] = {}
            if cwd:
                params["cwd"] = cwd
            resp = await self._session_mgr.client.get("/session/list", params=params)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            raise RequestError(code=-32603, message=str(e))

        entries: list[SessionInfo] = []
        for s in data:
            entries.append(SessionInfo(
                session_id=s.get("session_id", ""),
                cwd=s.get("cwd", cwd or ""),
                title=s.get("title"),
                updated_at=s.get("updated_at"),
            ))

        return ListSessionsResponse(sessions=entries)

    async def fork_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: list[McpServerStdio | HttpMcpServer | SseMcpServer] | None = None,
        **kwargs: Any,
    ) -> ForkSessionResponse:
        model = self._config.default_model
        # CrabCode Gateway doesn't have a fork endpoint yet — create new session
        try:
            state = await self._session_mgr.create(cwd, mcp_servers or [], model)
        except Exception as e:
            raise RequestError(code=-32603, message=str(e))

        models_state = await self._build_models_state(state)
        modes_state = await self._build_modes_state(state)
        config_options = _build_config_options(models_state, modes_state)

        return ForkSessionResponse(
            session_id=state.id,
            models=models_state,
            modes=modes_state,
            config_options=config_options,
        )

    async def resume_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: list[McpServerStdio | HttpMcpServer | SseMcpServer] | None = None,
        **kwargs: Any,
    ) -> ResumeSessionResponse:
        model = self._config.default_model
        try:
            state = await self._session_mgr.load(session_id, cwd, mcp_servers or [], model)
        except Exception as e:
            raise RequestError(code=-32603, message=str(e))

        models_state = await self._build_models_state(state)
        modes_state = await self._build_modes_state(state)
        config_options = _build_config_options(models_state, modes_state)

        await self._send_usage_update(state)

        return ResumeSessionResponse(
            models=models_state,
            modes=modes_state,
            config_options=config_options,
        )

    async def prompt(
        self,
        prompt: list,
        session_id: str,
        message_id: str | None = None,
        **kwargs: Any,
    ) -> PromptResponse:
        self._session_mgr.get(session_id)  # validate session exists

        # Convert ACP prompt parts to simple text
        text_parts: list[str] = []
        for part in prompt:
            if hasattr(part, "text"):
                text_parts.append(part.text)
            elif hasattr(part, "uri"):
                text_parts.append(f"[file: {part.uri}]")
        text = "\n".join(text_parts)

        # Send to Gateway — fire-and-forget, events arrive via SSE
        try:
            resp = await self._session_mgr.client.post(
                "/session/send",
                json={"text": text, "session_id": session_id},
            )
            resp.raise_for_status()
        except Exception as e:
            raise RequestError(code=-32603, message=str(e))

        # Wait briefly for the turn to complete by polling
        # (events stream via _event_listener; we wait for turn_complete)
        usage = await self._wait_turn_complete(session_id)

        return PromptResponse(
            stop_reason="end_turn",
            usage=usage,
        )

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        self._session_mgr.get(session_id)  # validate session exists
        try:
            await self._session_mgr.client.post(
                "/session/interrupt",
                json={"session_id": session_id},
            )
        except Exception:
            logger.exception("acp_cancel_failed")

    async def set_session_model(self, model_id: str, session_id: str, **kwargs: Any) -> SetSessionModelResponse | None:
        self._session_mgr.get(session_id)  # validate session exists
        # Parse "provider/model" format
        parts = model_id.split("/", 1)
        if len(parts) == 2:
            self._session_mgr.set_model(session_id, ModelSelection(provider_id=parts[0], model_id=parts[1]))
        return SetSessionModelResponse()

    async def set_session_mode(self, mode_id: str, session_id: str, **kwargs: Any) -> SetSessionModeResponse | None:
        self._session_mgr.set_mode(session_id, mode_id)
        return SetSessionModeResponse()

    async def set_config_option(
        self, config_id: str, session_id: str, value: str | bool, **kwargs: Any
    ) -> SetSessionConfigOptionResponse | None:
        state = self._session_mgr.get(session_id)
        if config_id == "model" and isinstance(value, str):
            parts = value.split("/", 1)
            if len(parts) == 2:
                self._session_mgr.set_model(session_id, ModelSelection(provider_id=parts[0], model_id=parts[1]))
        elif config_id == "mode" and isinstance(value, str):
            self._session_mgr.set_mode(session_id, value)
        else:
            raise RequestError(code=-32602, message=f"Unknown config option: {config_id}")

        models_state = await self._build_models_state(state)
        modes_state = await self._build_modes_state(state)
        return SetSessionConfigOptionResponse(
            config_options=_build_config_options(models_state, modes_state),
        )

    async def authenticate(self, method_id: str, **kwargs: Any) -> AuthenticateResponse | None:
        raise RequestError(code=-32601, message="Authentication not implemented")

    # ── Helpers ─────────────────────────────────────────────────

    async def _build_models_state(self, state: ACPSessionState) -> SessionModelState | None:
        """Build ACP SessionModelState from available Gateway config."""
        try:
            resp = await self._session_mgr.client.get("/config/models")
            resp.raise_for_status()
            data = resp.json()
            models = data.get("models", [])
            available = [
                ModelInfo(model_id=m.get("id", ""), name=m.get("name", m.get("id", "")))
                for m in models
            ]
            current = ""
            if state.model:
                current = f"{state.model.provider_id}/{state.model.model_id}"
            elif available:
                current = available[0].model_id
            return SessionModelState(current_model_id=current, available_models=available)
        except Exception:
            logger.exception("acp_build_models_failed")
            return None

    async def _build_modes_state(self, state: ACPSessionState) -> SessionModeState | None:
        """Build ACP SessionModeState."""
        modes = [
            SessionMode(id="agent", name="Agent", description="Autonomous coding agent"),
            SessionMode(id="plan", name="Plan", description="Plan before executing"),
        ]
        current = state.mode_id or "agent"
        return SessionModeState(current_mode_id=current, available_modes=modes)

    async def _replay_messages(self, state: ACPSessionState) -> None:
        """Replay session history as ACP updates (for load_session)."""
        try:
            resp = await self._session_mgr.client.get(
                "/session/messages",
                params={"session_id": state.id},
            )
            if resp.status_code != 200:
                return
            data = resp.json()
            if not isinstance(data, list):
                return
            for msg in data:
                role = msg.get("role", "")
                content = msg.get("content", "")
                if isinstance(content, str) and content:
                    if role == "assistant":
                        await self._push_agent_message_chunk(state.id, content)
                    elif role == "user":
                        update = acp.update_user_message_text(content)
                        await self._connection.session_update(session_id=state.id, update=update)
        except Exception:
            logger.exception("acp_replay_failed")

    async def _send_usage_update(self, state: ACPSessionState) -> None:
        """Push a usage_update notification to the ACP client."""
        try:
            resp = await self._session_mgr.client.get(
                "/session/messages",
                params={"session_id": state.id},
            )
            if resp.status_code != 200:
                return
            # Best-effort usage — if unavailable, skip
        except Exception:
            return

    async def _wait_turn_complete(self, session_id: str, timeout: float = 120.0) -> Usage | None:
        """Wait for the agent turn to complete via EventBus SSE.

        We rely on the _event_listener to process events in real-time.
        This method waits a short time then returns — the prompt()
        return value signals completion.
        """
        # Give the event stream a moment to flush; the actual
        # streaming content goes through _event_listener.
        await asyncio.sleep(0.1)
        return None

    async def close(self) -> None:
        """Clean up resources."""
        if self._event_task:
            self._event_task.cancel()
            try:
                await self._event_task
            except asyncio.CancelledError:
                pass
        await self._session_mgr.aclose()


# ── Module-level helpers ───────────────────────────────────────


def _build_config_options(
    models_state: SessionModelState | None,
    modes_state: SessionModeState | None,
) -> list[SessionConfigOptionSelect]:
    """Build ACP config option descriptors for the client."""
    options: list[SessionConfigOptionSelect] = []

    if models_state:
        options.append(SessionConfigOptionSelect(
            id="model",
            name="Model",
            category="model",
            type="select",
            current_value=models_state.current_model_id,
            options=[
                SessionConfigSelectOption(value=m.model_id, name=m.name)
                for m in models_state.available_models
            ],
        ))

    if modes_state:
        options.append(SessionConfigOptionSelect(
            id="mode",
            name="Session Mode",
            category="mode",
            type="select",
            current_value=modes_state.current_mode_id,
            options=[
                SessionConfigSelectOption(value=m.id, name=m.name, description=m.description)
                for m in modes_state.available_modes
            ],
        ))

    return options
