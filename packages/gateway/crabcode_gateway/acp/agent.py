"""CrabCode ACP Agent implementation.

Implements the ACP Agent protocol by translating between ACP JSON-RPC
and CrabCode's internal Gateway REST API + EventBus.

Architecture mirrors OpenCode's agent.ts:
  - ACP client ↔ AgentSideConnection ↔ CrabCodeACPAgent ↔ httpx ↔ Gateway REST API
  - EventBus SSE → _event_listener → session_update() → ACP client
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

import acp
import acp.schema as S
from acp.exceptions import RequestError
from acp.schema import (
    AgentCapabilities,
    AllowedOutcome,
    AuthenticateResponse,
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
    SessionInfo,
    SessionListCapabilities,
    SessionMode,
    SessionModeState,
    SessionModelState,
    SessionResumeCapabilities,
    SessionForkCapabilities,
    SetSessionConfigOptionResponse,
    SetSessionModeResponse,
    SetSessionModelResponse,
    TextContent,
    ToolCallProgress,
    ToolCallStart,
    Usage,
    UsageUpdate,
)

from crabcode_core import VERSION
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

_LISTENER_READY_TIMEOUT = 10.0


class CrabCodeACPAgent:
    """ACP Agent that bridges ACP protocol to CrabCode's Gateway API."""

    def __init__(self, config: ACPConfig) -> None:
        self._config = config
        self._session_mgr = ACPSessionManager(config)
        self._connection: Any = None  # set by on_connect
        self._event_task: asyncio.Task | None = None
        self._listener_generation = 0
        self._listener_ready = asyncio.Event()
        self._listener_error: str | None = None
        self._interaction_tasks: dict[
            asyncio.Task[Any],
            tuple[int, str, str],
        ] = {}
        self._closed = False
        # Tool-use ids are only unique within a conversation in some ACP
        # clients.  Include the session id so two sessions cannot suppress
        # each other's notifications.
        self._tool_starts: set[tuple[str, str]] = set()
        self._turn_events: dict[str, asyncio.Event] = {}
        self._turn_operations: dict[str, str] = {}
        self._turn_releases: dict[tuple[str, str], asyncio.Event] = {}
        self._turn_send_settled: dict[str, tuple[str, asyncio.Event]] = {}
        self._turn_send_accepted: dict[str, str] = {}
        self._turn_cancel_requests: dict[str, str] = {}
        self._turn_cancellations: dict[str, str] = {}
        self._turn_usage: dict[str, Usage | None] = {}
        self._turn_errors: dict[str, str] = {}
        self._turn_reasons: dict[str, str] = {}

    # ── Connection lifecycle ────────────────────────────────────

    def on_connect(self, connection: Any) -> None:
        """Called by AgentSideConnection after wiring."""
        self._fail_active_prompts("ACP event connection replaced")
        self._closed = False
        self._connection = connection
        self._start_event_listener()

    def _start_event_listener(self) -> None:
        """Start a new listener generation and fence its predecessor."""
        previous = self._event_task
        previous_ready = self._listener_ready
        previous_ready.set()
        self._listener_generation += 1
        generation = self._listener_generation
        ready = asyncio.Event()
        self._listener_ready = ready
        self._listener_error = None
        if previous is not None and not previous.done():
            previous.cancel()
        self._event_task = asyncio.create_task(
            self._event_listener(generation, ready)
        )
        self._event_task.add_done_callback(self._consume_task_result)

    def _fail_active_prompts(self, message: str) -> None:
        for session_id, waiter in list(self._turn_events.items()):
            if session_id not in self._turn_operations or waiter.is_set():
                continue
            self._turn_errors.setdefault(session_id, message)
            waiter.set()

    async def _ensure_event_listener_ready(self) -> None:
        """Join a ready listener, restarting a failed/ended generation."""
        while True:
            if self._closed:
                raise RequestError(code=-32603, message="ACP agent is closed")
            task = self._event_task
            if task is None or task.done() or self._listener_error is not None:
                self._start_event_listener()
            generation = self._listener_generation
            ready = self._listener_ready
            try:
                await asyncio.wait_for(
                    ready.wait(),
                    timeout=_LISTENER_READY_TIMEOUT,
                )
            except asyncio.TimeoutError as exc:
                if generation == self._listener_generation:
                    self._listener_error = "Timed out waiting for Gateway event stream"
                    if self._event_task is not None:
                        self._event_task.cancel()
                raise RequestError(
                    code=-32001,
                    message="Timed out waiting for Gateway event stream",
                ) from exc
            if generation != self._listener_generation:
                continue
            if self._listener_error:
                raise RequestError(code=-32603, message=self._listener_error)
            return

    async def _wait_for_listener_or_turn_stop(
        self,
        session_id: str,
        operation_id: str,
        turn_event: asyncio.Event,
    ) -> bool:
        """Wait for SSE readiness, returning false for a pre-send cancel."""
        listener_ready = asyncio.create_task(self._ensure_event_listener_ready())
        turn_stopped = asyncio.create_task(turn_event.wait())
        try:
            await asyncio.wait(
                {listener_ready, turn_stopped},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if self._turn_cancellations.get(session_id) == operation_id:
                return False
            if turn_event.is_set():
                message = self._turn_errors.get(
                    session_id,
                    "ACP operation stopped before Gateway admission",
                )
                raise RequestError(code=-32603, message=message)
            await listener_ready
            return True
        finally:
            for task in (listener_ready, turn_stopped):
                if not task.done():
                    task.cancel()
            for task in (listener_ready, turn_stopped):
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass

    @staticmethod
    def _consume_task_result(task: asyncio.Task[Any]) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("ACP event listener task failed", exc_info=True)

    def _start_interaction_task(
        self,
        generation: int,
        session_id: str,
        operation_id: str,
        payload: dict[str, Any],
    ) -> None:
        task = asyncio.create_task(
            self._run_interaction(
                generation,
                session_id,
                operation_id,
                payload,
            )
        )
        self._interaction_tasks[task] = (generation, session_id, operation_id)
        task.add_done_callback(self._interaction_task_done)

    def _interaction_task_done(self, task: asyncio.Task[Any]) -> None:
        self._interaction_tasks.pop(task, None)
        self._consume_task_result(task)

    async def _cancel_interaction_tasks(self, generation: int | None = None) -> None:
        tasks = [
            task
            for task, (task_generation, _session_id, _operation_id)
            in self._interaction_tasks.items()
            if generation is None or task_generation == generation
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _cancel_operation_interactions(
        self,
        session_id: str,
        operation_id: str,
    ) -> None:
        tasks = [
            task
            for task, (_generation, task_session, task_operation) in list(
                self._interaction_tasks.items()
            )
            if task_session == session_id and task_operation == operation_id
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _interaction_is_current(
        self,
        generation: int,
        session_id: str,
        operation_id: str,
    ) -> bool:
        return (
            not self._closed
            and generation == self._listener_generation
            and self._turn_operations.get(session_id) == operation_id
            and (session_id, operation_id) not in self._turn_releases
        )

    async def _run_interaction(
        self,
        generation: int,
        session_id: str,
        operation_id: str,
        payload: dict[str, Any],
    ) -> None:
        if not self._interaction_is_current(generation, session_id, operation_id):
            return
        event_type = payload.get("type")
        try:
            if event_type == "permission_request":
                await self._handle_permission_request(
                    session_id,
                    payload,
                    generation=generation,
                    operation_id=operation_id,
                )
            elif event_type == "choice_request":
                await self._handle_choice_request(
                    session_id,
                    payload,
                    generation=generation,
                    operation_id=operation_id,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("acp_interaction_failed")
            if not self._interaction_is_current(
                generation,
                session_id,
                operation_id,
            ):
                return
            if event_type == "permission_request":
                delivered = await self._reply_permission(
                    str(payload.get("tool_use_id") or ""),
                    session_id,
                    "reject",
                    agent_id=payload.get("agent_id"),
                )
                if not delivered:
                    await self._fail_interaction_reply(
                        session_id,
                        operation_id,
                        "Failed to deliver permission response to Gateway",
                    )
            elif event_type == "choice_request":
                delivered = await self._reply_choice(
                    str(payload.get("tool_use_id") or ""),
                    session_id,
                    selected=[],
                    cancelled=True,
                    agent_id=payload.get("agent_id"),
                )
                if not delivered:
                    await self._fail_interaction_reply(
                        session_id,
                        operation_id,
                        "Failed to deliver choice response to Gateway",
                    )

    async def _event_listener(
        self,
        generation: int,
        ready: asyncio.Event,
    ) -> None:
        """Subscribe to Gateway EventBus SSE and translate CoreEvents to ACP session_updates."""
        client = self._session_mgr.client
        failure: str | None = None
        try:
            async with client.stream("GET", "/event") as resp:
                resp.raise_for_status()
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
                    if payload.get("type") == "server.connected":
                        if generation == self._listener_generation:
                            ready.set()
                        continue
                    if generation != self._listener_generation:
                        return
                    try:
                        await self._handle_event_payload(
                            payload,
                            generation=generation,
                        )
                    except Exception:
                        logger.exception("acp_event_handler_error")
                failure = "Gateway event stream ended"
        except asyncio.CancelledError:
            return
        except Exception as exc:
            failure = f"Gateway event stream failed: {exc}"
            logger.exception("acp_event_listener_stopped")
        finally:
            if failure and generation == self._listener_generation and not self._closed:
                self._listener_error = failure
                ready.set()
                self._fail_active_prompts(failure)
            await self._cancel_interaction_tasks(generation)

    async def _handle_event_payload(
        self,
        payload: dict[str, Any],
        *,
        generation: int | None = None,
    ) -> None:
        """Translate a Gateway EventBus payload into ACP session_update(s)."""
        event_type = payload.get("type", "")
        session_id = payload.get("session_id", "")
        state = self._session_mgr.try_get(session_id)
        if not state:
            return

        active_operation = self._turn_operations.get(session_id)
        if (
            active_operation is None
            or payload.get("operation_scope") != "foreground"
            or payload.get("operation_id") != active_operation
            or (session_id, active_operation) in self._turn_releases
        ):
            return

        if event_type in {"permission_request", "choice_request"}:
            if generation is None:
                if event_type == "permission_request":
                    await self._handle_permission_request(session_id, payload)
                else:
                    await self._handle_choice_request(session_id, payload)
            else:
                self._start_interaction_task(
                    generation,
                    session_id,
                    active_operation,
                    payload,
                )
        elif event_type == "tool_use":
            await self._handle_tool_use(session_id, payload)
        elif event_type == "tool_result":
            await self._handle_tool_result(session_id, payload)
        elif event_type == "stream_text":
            await self._push_agent_message_chunk(session_id, payload.get("text", ""))
        elif event_type == "thinking":
            await self._push_agent_thought_chunk(session_id, payload.get("text", ""))
        elif event_type == "turn_complete":
            usage = _usage_from_payload(payload.get("usage"))
            self._turn_usage[session_id] = usage
            self._turn_reasons.setdefault(
                session_id,
                str(payload.get("reason") or "end_turn"),
            )
            waiter = self._turn_events.get(session_id)
            if waiter is not None:
                waiter.set()
        elif event_type == "error":
            if self._turn_cancellations.get(session_id) == active_operation:
                return
            # Managed-agent failures are lifecycle events for that background
            # run, not failures of the active top-level ACP prompt. The agent
            # state/callback channels report them separately. Callback and
            # monitor continuation failures predate agent attribution but are
            # likewise emitted outside the active foreground turn.
            if (
                payload.get("agent_id") is not None
                or payload.get("error_type") in {"agent_callback", "monitor_callback"}
            ):
                return
            # A failed query may not emit turn_complete.  Wake the ACP
            # prompt waiter and surface the error instead of hanging for the
            # full timeout.
            self._turn_errors[session_id] = str(payload.get("message") or "Query failed")
            waiter = self._turn_events.get(session_id)
            if waiter is not None:
                waiter.set()

    async def _handle_permission_request(
        self,
        session_id: str,
        payload: dict[str, Any],
        *,
        generation: int | None = None,
        operation_id: str | None = None,
    ) -> None:
        """Forward a CrabCode permission request to the ACP client."""
        tool_name = payload.get("tool_name", "")
        tool_use_id = payload.get("tool_use_id", "")
        tool_input = payload.get("tool_input", {})
        agent_id = payload.get("agent_id")

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
            if self._interaction_reply_allowed(
                generation,
                session_id,
                operation_id,
            ):
                delivered = await self._reply_permission(
                    tool_use_id,
                    session_id,
                    "reject",
                    agent_id=agent_id,
                )
                if not delivered:
                    await self._fail_interaction_reply(
                        session_id,
                        operation_id,
                        "Failed to deliver permission response to Gateway",
                    )
            return

        if not self._interaction_reply_allowed(generation, session_id, operation_id):
            return
        outcome = result.outcome if result else None
        if not outcome or not isinstance(outcome, AllowedOutcome) or outcome.outcome != "selected":
            delivered = await self._reply_permission(
                tool_use_id,
                session_id,
                "reject",
                agent_id=agent_id,
            )
            if not delivered:
                await self._fail_interaction_reply(
                    session_id,
                    operation_id,
                    "Failed to deliver permission response to Gateway",
                )
            return

        option_id = outcome.option_id if hasattr(outcome, "option_id") else "reject"
        delivered = await self._reply_permission(
            tool_use_id,
            session_id,
            option_id,
            agent_id=agent_id,
        )
        if not delivered:
            await self._fail_interaction_reply(
                session_id,
                operation_id,
                "Failed to deliver permission response to Gateway",
            )

    def _interaction_reply_allowed(
        self,
        generation: int | None,
        session_id: str,
        operation_id: str | None,
    ) -> bool:
        if generation is None or operation_id is None:
            return True
        return self._interaction_is_current(generation, session_id, operation_id)

    async def _reply_permission(
        self,
        tool_use_id: str,
        session_id: str,
        reply: str,
        *,
        agent_id: str | None = None,
    ) -> bool:
        """Send a permission response back to the Gateway."""
        allowed = reply in ("once", "always")
        always = reply == "always"
        try:
            response = await self._session_mgr.client.post(
                "/permission/respond",
                json={
                    "tool_use_id": tool_use_id,
                    "allowed": allowed,
                    "always_allow": always,
                    "session_id": session_id,
                    "agent_id": agent_id,
                },
            )
            response.raise_for_status()
            return True
        except Exception:
            logger.exception("acp_reply_permission_failed")
            return False

    async def _handle_choice_request(
        self,
        session_id: str,
        payload: dict[str, Any],
        *,
        generation: int | None = None,
        operation_id: str | None = None,
    ) -> None:
        tool_use_id = str(payload.get("tool_use_id") or "")
        question = str(payload.get("question") or "Choose an option")
        options = [str(option) for option in payload.get("options") or []]
        agent_id = payload.get("agent_id")

        if payload.get("multiple") or not options:
            if self._interaction_reply_allowed(generation, session_id, operation_id):
                delivered = await self._reply_choice(
                    tool_use_id,
                    session_id,
                    selected=[],
                    cancelled=True,
                    agent_id=agent_id,
                )
                if not delivered:
                    await self._fail_interaction_reply(
                        session_id,
                        operation_id,
                        "Failed to deliver choice response to Gateway",
                    )
            return

        option_ids = {f"choice-{index}": option for index, option in enumerate(options)}
        permission_options = [
            PermissionOption(
                option_id=option_id,
                kind="allow_once",
                name=option,
            )
            for option_id, option in option_ids.items()
        ]
        tool_call = S.ToolCallUpdate(
            tool_call_id=tool_use_id,
            status="pending",
            title=question,
            kind="other",
            raw_input={"question": question, "options": options},
        )
        try:
            result = await self._connection.request_permission(
                options=permission_options,
                session_id=session_id,
                tool_call=tool_call,
            )
        except Exception:
            logger.exception("acp_choice_request_failed")
            result = None

        if not self._interaction_reply_allowed(generation, session_id, operation_id):
            return
        outcome = result.outcome if result else None
        selected = (
            option_ids.get(outcome.option_id)
            if isinstance(outcome, AllowedOutcome) and outcome.outcome == "selected"
            else None
        )
        delivered = await self._reply_choice(
            tool_use_id,
            session_id,
            selected=[selected] if selected is not None else [],
            cancelled=selected is None,
            agent_id=agent_id,
        )
        if not delivered:
            await self._fail_interaction_reply(
                session_id,
                operation_id,
                "Failed to deliver choice response to Gateway",
            )

    async def _reply_choice(
        self,
        tool_use_id: str,
        session_id: str,
        *,
        selected: list[str],
        cancelled: bool,
        agent_id: str | None = None,
    ) -> bool:
        try:
            response = await self._session_mgr.client.post(
                "/choice/respond",
                json={
                    "tool_use_id": tool_use_id,
                    "selected": selected,
                    "cancelled": cancelled,
                    "session_id": session_id,
                    "agent_id": agent_id,
                },
            )
            response.raise_for_status()
            return True
        except Exception:
            logger.exception("acp_reply_choice_failed")
            return False

    async def _fail_interaction_reply(
        self,
        session_id: str,
        operation_id: str | None,
        message: str,
    ) -> None:
        """Stop an operation whose interactive response could not be routed."""
        if operation_id is None or self._turn_operations.get(session_id) != operation_id:
            return
        if not await self._interrupt_operation(session_id, operation_id):
            return
        if self._turn_operations.get(session_id) != operation_id:
            return
        waiter = self._turn_events.get(session_id)
        if waiter is None or waiter.is_set():
            return
        self._turn_errors[session_id] = message
        waiter.set()

    async def _handle_tool_use(self, session_id: str, payload: dict[str, Any]) -> None:
        """Tool call started — push tool_call start notification."""
        tool_name = payload.get("tool_name", "")
        tool_use_id = payload.get("tool_use_id", "")
        tool_input = payload.get("tool_input", {})

        tool_key = (session_id, tool_use_id)
        if tool_key in self._tool_starts:
            return
        self._tool_starts.add(tool_key)

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

        self._tool_starts.discard((session_id, tool_use_id))
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
            # Gateway authentication is handled by the transport (Basic Auth
            # / bearer middleware).  Do not advertise an ACP login method that
            # the CLI does not implement.
            auth_methods=[],
            agent_info=Implementation(name="CrabCode", version=VERSION),
        )

    async def new_session(
        self,
        cwd: str,
        mcp_servers: list[McpServerStdio | HttpMcpServer | SseMcpServer] | None = None,
        **kwargs: Any,
    ) -> NewSessionResponse:
        if self._closed:
            raise RequestError(code=-32603, message="ACP agent is closed")
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
        if self._closed:
            raise RequestError(code=-32603, message="ACP agent is closed")
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
        if self._closed:
            raise RequestError(code=-32603, message="ACP agent is closed")
        try:
            # ACP currently identifies the source session, not a message. Use
            # its latest completed assistant reply; Desktop exposes the more
            # precise message-level fork endpoint directly.
            messages_resp = await self._session_mgr.client.get(
                "/session/messages",
                params={"session_id": session_id},
            )
            messages_resp.raise_for_status()
            messages = messages_resp.json()
            requested_uuid = kwargs.get("message_uuid") or kwargs.get("messageUuid")
            assistant_uuid = str(requested_uuid) if requested_uuid else next(
                (
                    str(message.get("uuid"))
                    for message in reversed(messages)
                    if isinstance(message, dict)
                    and message.get("role") == "assistant"
                    and message.get("uuid")
                ),
                None,
            )
            if assistant_uuid is None:
                raise RequestError(code=-32602, message="Session has no assistant reply to fork")
            response = await self._session_mgr.client.post(
                "/session/fork",
                json={"session_id": session_id, "message_uuid": assistant_uuid},
            )
            response.raise_for_status()
            data = response.json()
            forked_id = str(data["session_id"])
            state = ACPSessionState(
                id=forked_id,
                cwd=str(data.get("cwd") or cwd),
                mcp_servers=mcp_servers or [],
                created_at=time.time(),
                model=self._config.default_model,
            )
            self._session_mgr._sessions[forked_id] = state
            models_state = await self._build_models_state(state)
            modes_state = await self._build_modes_state(state)
            return ForkSessionResponse(
                session_id=forked_id,
                models=models_state,
                modes=modes_state,
                config_options=_build_config_options(models_state, modes_state),
            )
        except RequestError:
            raise
        except Exception as e:
            raise RequestError(code=-32603, message=str(e))

    async def resume_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: list[McpServerStdio | HttpMcpServer | SseMcpServer] | None = None,
        **kwargs: Any,
    ) -> ResumeSessionResponse:
        if self._closed:
            raise RequestError(code=-32603, message="ACP agent is closed")
        model = self._config.default_model
        try:
            state = await self._session_mgr.load(session_id, cwd, mcp_servers or [], model)
        except Exception as e:
            raise RequestError(code=-32603, message=str(e))

        models_state = await self._build_models_state(state)
        modes_state = await self._build_modes_state(state)
        config_options = _build_config_options(models_state, modes_state)

        await self._send_usage_update(state)
        await self._replay_messages(state)

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
        if self._closed:
            raise RequestError(code=-32603, message="ACP agent is closed")
        self._session_mgr.get(session_id)  # validate session exists

        # Convert ACP prompt parts to simple text
        text_parts: list[str] = []
        for part in prompt:
            if hasattr(part, "text"):
                text_parts.append(part.text)
            elif hasattr(part, "uri"):
                text_parts.append(f"[file: {part.uri}]")
        text = "\n".join(text_parts)

        # Install the waiter before sending.  The gateway can complete a very
        # short turn before the HTTP request returns, and that event must not
        # be lost between the response and waiter setup.
        if session_id in self._turn_operations:
            raise RequestError(code=-32000, message=f"Session {session_id} already has a prompt in progress")
        operation_id = uuid.uuid4().hex
        turn_event = asyncio.Event()
        self._turn_events[session_id] = turn_event
        self._turn_operations[session_id] = operation_id
        self._turn_send_settled.pop(session_id, None)
        self._turn_send_accepted.pop(session_id, None)
        self._turn_cancel_requests.pop(session_id, None)
        self._turn_cancellations.pop(session_id, None)
        self._turn_usage.pop(session_id, None)
        self._turn_errors.pop(session_id, None)
        self._turn_reasons.pop(session_id, None)

        # Do not launch the foreground operation until the global SSE listener
        # has consumed its connected handshake. Otherwise a fast operation can
        # publish its terminal event before this ACP process is subscribed.
        send_attempted = False
        try:
            listener_ready = await self._wait_for_listener_or_turn_stop(
                session_id,
                operation_id,
                turn_event,
            )
            if not listener_ready:
                await self._release_turn_ownership(
                    session_id,
                    operation_id,
                    turn_event,
                )
                self._discard_turn_result(session_id)
                return PromptResponse(
                    stop_reason="cancelled",
                    usage=None,
                    user_message_id=message_id,
                )
            send_attempted = True
            send_settled = asyncio.Event()
            self._turn_send_settled[session_id] = (operation_id, send_settled)
            try:
                resp = await self._session_mgr.client.post(
                    "/session/send",
                    json={
                        "text": text,
                        "session_id": session_id,
                        "operation_id": operation_id,
                    },
                )
                resp.raise_for_status()
            finally:
                send_settled.set()
            self._turn_send_accepted[session_id] = operation_id
            if self._turn_cancel_requests.get(session_id) == operation_id:
                # cancel() may have raced the POST while the operation did not
                # yet exist server-side. Re-issue after successful admission.
                interrupted = await self._interrupt_operation(
                    session_id,
                    operation_id,
                )
                if interrupted:
                    self._confirm_turn_cancel(session_id, operation_id)
                elif self._turn_cancel_requests.get(session_id) == operation_id:
                    self._turn_cancel_requests.pop(session_id, None)
        except asyncio.CancelledError:
            await self._release_turn_ownership(
                session_id,
                operation_id,
                turn_event,
            )
            self._discard_turn_result(session_id)
            if send_attempted:
                await self._interrupt_after_task_cancellation(
                    session_id,
                    operation_id,
                )
            raise
        except Exception as e:
            await self._release_turn_ownership(
                session_id,
                operation_id,
                turn_event,
            )
            self._discard_turn_result(session_id)
            if send_attempted:
                await self._interrupt_operation(session_id, operation_id)
            if isinstance(e, RequestError):
                raise
            raise RequestError(code=-32603, message=str(e))

        try:
            usage = await self._wait_turn_complete(
                session_id,
                operation_id=operation_id,
                event=turn_event,
            )
        except asyncio.CancelledError:
            self._discard_turn_result(session_id)
            await self._interrupt_after_task_cancellation(session_id, operation_id)
            raise
        except RequestError:
            # A timeout must not leave the Gateway query running indefinitely.
            self._discard_turn_result(session_id)
            if not self._closed:
                await self._interrupt_operation(session_id, operation_id)
            raise

        reason = self._turn_reasons.pop(session_id, "end_turn")
        stop_reason = {
            "interrupted": "cancelled",
            "cancelled": "cancelled",
            "max_tokens": "max_tokens",
            "max_turns": "max_turn_requests",
        }.get(reason, "end_turn")

        return PromptResponse(
            stop_reason=stop_reason,
            usage=usage,
            user_message_id=message_id,
        )

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        self._session_mgr.get(session_id)  # validate session exists
        operation_id = self._turn_operations.get(session_id)
        if operation_id is None:
            return
        send_state = self._turn_send_settled.get(session_id)
        send_started = send_state is not None and send_state[0] == operation_id
        if not send_started:
            self._confirm_turn_cancel(session_id, operation_id)
            return

        self._turn_cancel_requests[session_id] = operation_id
        if self._turn_send_accepted.get(session_id) != operation_id:
            return
        interrupted = await self._interrupt_operation(session_id, operation_id)
        if self._turn_operations.get(session_id) != operation_id:
            return
        if interrupted:
            self._confirm_turn_cancel(session_id, operation_id)
        elif self._turn_cancel_requests.get(session_id) == operation_id:
            self._turn_cancel_requests.pop(session_id, None)

    def _confirm_turn_cancel(self, session_id: str, operation_id: str) -> None:
        if self._turn_operations.get(session_id) != operation_id:
            return
        waiter = self._turn_events.get(session_id)
        if waiter is None or waiter.is_set():
            self._turn_cancel_requests.pop(session_id, None)
            return
        self._turn_cancel_requests.pop(session_id, None)
        self._turn_cancellations[session_id] = operation_id
        self._turn_reasons[session_id] = "cancelled"
        waiter.set()

    async def _interrupt_operation(
        self,
        session_id: str,
        operation_id: str,
    ) -> bool:
        """Best-effort interrupt for one owned Gateway operation."""
        try:
            response = await self._session_mgr.client.post(
                "/session/interrupt",
                json={
                    "session_id": session_id,
                    "operation_id": operation_id,
                },
            )
            response.raise_for_status()
            return True
        except Exception:
            logger.debug("acp_operation_interrupt_failed", exc_info=True)
            return False

    async def _interrupt_after_task_cancellation(
        self,
        session_id: str,
        operation_id: str,
    ) -> None:
        """Finish a targeted interrupt despite repeated caller cancellation."""
        cleanup = asyncio.create_task(
            self._interrupt_operation(session_id, operation_id)
        )
        while True:
            try:
                await asyncio.shield(cleanup)
                return
            except asyncio.CancelledError:
                if cleanup.done():
                    try:
                        cleanup.result()
                    except asyncio.CancelledError:
                        pass
                    return

    async def set_session_model(self, model_id: str, session_id: str, **kwargs: Any) -> SetSessionModelResponse | None:
        self._session_mgr.get(session_id)  # validate session exists
        try:
            resp = await self._session_mgr.client.post(
                "/config/switch-model",
                json={"name": model_id, "session_id": session_id},
            )
            resp.raise_for_status()
        except Exception as exc:
            raise RequestError(code=-32603, message=str(exc)) from exc
        parts = model_id.split("/", 1)
        self._session_mgr.set_model(
            session_id,
            ModelSelection(
                provider_id=parts[0] if len(parts) == 2 else "",
                model_id=parts[1] if len(parts) == 2 else model_id,
            ),
        )
        return SetSessionModelResponse()

    async def set_session_mode(self, mode_id: str, session_id: str, **kwargs: Any) -> SetSessionModeResponse | None:
        self._session_mgr.get(session_id)
        try:
            resp = await self._session_mgr.client.post(
                "/config/switch-mode",
                json={"mode": mode_id, "session_id": session_id},
            )
            resp.raise_for_status()
        except Exception as exc:
            raise RequestError(code=-32603, message=str(exc)) from exc
        self._session_mgr.set_mode(session_id, mode_id)
        return SetSessionModeResponse()

    async def set_config_option(
        self, config_id: str, session_id: str, value: str | bool, **kwargs: Any
    ) -> SetSessionConfigOptionResponse | None:
        state = self._session_mgr.get(session_id)
        if config_id == "model" and isinstance(value, str):
            await self.set_session_model(value, session_id)
        elif config_id == "mode" and isinstance(value, str):
            await self.set_session_mode(value, session_id)
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
            resp = await self._session_mgr.client.get(
                "/config/models",
                params={"session_id": state.id},
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list):
                models = data
            elif isinstance(data, dict):
                models = data.get("models", data.get("items", []))
            else:
                models = []
            if not isinstance(models, list):
                models = []
            available = [
                ModelInfo(
                    model_id=str(
                        m.get("modelId")
                        or m.get("model_id")
                        or m.get("id")
                        or m.get("name")
                        or ""
                    ),
                    name=str(m.get("name") or m.get("id") or m.get("modelId") or ""),
                    description=m.get("description"),
                )
                for m in models
                if isinstance(m, dict)
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
                if isinstance(content, list):
                    content = "\n".join(
                        str(item.get("text", ""))
                        for item in content
                        if isinstance(item, dict) and item.get("text")
                    )
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
                "/session/status",
                params={"session_id": state.id},
            )
            if resp.status_code != 200:
                return
            payload = resp.json()
            if not isinstance(payload, dict):
                return
            size = max(0, int(payload.get("context_window_tokens") or 0))
            used = max(0, int(payload.get("context_used_tokens") or 0))
            if not size and not used:
                return
            update = UsageUpdate(session_update="usage_update", size=size, used=used)
            if self._connection is not None:
                await self._connection.session_update(session_id=state.id, update=update)
        except Exception:
            logger.debug("acp_usage_update_failed", exc_info=True)

    async def _wait_turn_complete(
        self,
        session_id: str,
        timeout: float = 120.0,
        *,
        operation_id: str | None = None,
        event: asyncio.Event | None = None,
    ) -> Usage | None:
        """Wait for the agent turn to complete via EventBus SSE.

        The event listener resolves a per-session event when the gateway
        publishes ``turn_complete`` (or an error).  A timeout is reported to
        the ACP client rather than falsely acknowledging an unfinished turn.
        """
        waiter = event or self._turn_events.get(session_id)
        if waiter is None:
            raise RequestError(code=-32602, message=f"Session not found: {session_id}")
        try:
            await asyncio.wait_for(waiter.wait(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise RequestError(
                code=-32001,
                message=f"Timed out waiting for session {session_id} to complete",
            ) from exc
        finally:
            if operation_id is not None:
                await self._release_turn_ownership(
                    session_id,
                    operation_id,
                    waiter,
                )
            elif self._turn_events.get(session_id) is waiter:
                self._turn_events.pop(session_id, None)

        error = self._turn_errors.pop(session_id, None)
        if error:
            raise RequestError(code=-32603, message=error)
        return self._turn_usage.pop(session_id, None)

    async def _release_turn_ownership(
        self,
        session_id: str,
        operation_id: str,
        waiter: asyncio.Event,
    ) -> None:
        if (
            self._turn_operations.get(session_id) != operation_id
            or self._turn_events.get(session_id) is not waiter
        ):
            return

        key = (session_id, operation_id)
        release_done = self._turn_releases.get(key)
        if release_done is not None:
            await release_done.wait()
            return

        release_done = asyncio.Event()
        self._turn_releases[key] = release_done
        try:
            await self._cancel_operation_interactions(session_id, operation_id)
        finally:
            if (
                self._turn_operations.get(session_id) == operation_id
                and self._turn_events.get(session_id) is waiter
            ):
                self._turn_operations.pop(session_id, None)
                self._turn_events.pop(session_id, None)
                send_state = self._turn_send_settled.get(session_id)
                if send_state is not None and send_state[0] == operation_id:
                    self._turn_send_settled.pop(session_id, None)
                if self._turn_send_accepted.get(session_id) == operation_id:
                    self._turn_send_accepted.pop(session_id, None)
                if self._turn_cancel_requests.get(session_id) == operation_id:
                    self._turn_cancel_requests.pop(session_id, None)
                if self._turn_cancellations.get(session_id) == operation_id:
                    self._turn_cancellations.pop(session_id, None)
            if self._turn_releases.get(key) is release_done:
                self._turn_releases.pop(key, None)
            release_done.set()

    def _discard_turn_result(self, session_id: str) -> None:
        self._turn_usage.pop(session_id, None)
        self._turn_errors.pop(session_id, None)
        self._turn_reasons.pop(session_id, None)

    async def close(self) -> None:
        """Clean up resources."""
        admitted_operations: list[
            tuple[str, str, asyncio.Event | None, asyncio.Event | None]
        ] = []
        for session_id, operation_id in self._turn_operations.items():
            send_state = self._turn_send_settled.get(session_id)
            send_settled = (
                send_state[1]
                if send_state is not None and send_state[0] == operation_id
                else None
            )
            if (
                self._turn_send_accepted.get(session_id) == operation_id
                or send_settled is not None
            ):
                admitted_operations.append(
                    (
                        session_id,
                        operation_id,
                        self._turn_events.get(session_id),
                        send_settled,
                    )
                )
        self._closed = True
        self._listener_generation += 1
        self._listener_ready.set()
        self._listener_error = "ACP agent closed"
        self._fail_active_prompts("ACP agent closed")
        for session_id, operation_id, waiter, send_settled in admitted_operations:
            if send_settled is not None:
                await send_settled.wait()
            await self._interrupt_operation(session_id, operation_id)
            if waiter is not None:
                await self._release_turn_ownership(
                    session_id,
                    operation_id,
                    waiter,
                )
        if self._event_task:
            self._event_task.cancel()
            try:
                await self._event_task
            except asyncio.CancelledError:
                pass
            self._event_task = None
        await self._cancel_interaction_tasks()
        self._turn_usage.clear()
        self._turn_reasons.clear()
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


def _usage_from_payload(raw: Any) -> Usage | None:
    """Translate a gateway usage mapping to ACP's required token fields."""
    if not isinstance(raw, dict):
        return None

    def _number(*keys: str) -> int:
        for key in keys:
            value = raw.get(key)
            if isinstance(value, (int, float)) and value >= 0:
                return int(value)
        return 0

    input_tokens = _number(
        "total_input_tokens",
        "input_tokens",
        "inputTokens",
        "prompt_tokens",
    )
    output_tokens = _number("output_tokens", "outputTokens", "completion_tokens")
    thought_tokens = _number("thought_tokens", "thoughtTokens", "reasoning_tokens")
    cached_read_tokens = _number("cache_read_tokens", "cachedReadTokens")
    cached_write_tokens = _number("cache_write_tokens", "cachedWriteTokens")
    total_tokens = _number("total_tokens", "totalTokens")
    if total_tokens == 0:
        total_tokens = input_tokens + output_tokens + thought_tokens
    if not (
        input_tokens
        or output_tokens
        or total_tokens
        or cached_read_tokens
        or cached_write_tokens
    ):
        return None
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        thought_tokens=thought_tokens or None,
        cached_read_tokens=cached_read_tokens or None,
        cached_write_tokens=cached_write_tokens or None,
        total_tokens=total_tokens,
    )
