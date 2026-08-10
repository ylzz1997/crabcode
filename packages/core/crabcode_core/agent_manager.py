"""Managed multi-agent runtime for CrabCode."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from crabcode_core.logging_utils import get_logger
from crabcode_core.prompts.profile import PromptProfile, resolve_agent_prompt
from crabcode_core.query.loop import QueryParams, query_loop
from crabcode_core.types.config import AgentSettings, AgentTypeConfig, CrabCodeSettings
from crabcode_core.types.event import (
    AgentOutputEvent,
    AgentStateEvent,
    ChoiceRequestEvent,
    ChoiceResponseEvent,
    CompactEvent,
    CoreEvent,
    ErrorEvent,
    PermissionRequestEvent,
    PermissionResponseEvent,
    StreamModeEvent,
    StreamTextEvent,
    ToolResultEvent,
    ToolUseEvent,
    TurnCompleteEvent,
)
from crabcode_core.types.message import (
    Message,
    find_assistant_reply,
    message_from_entry,
    create_user_message,
)
from crabcode_core.types.tool import Tool, ToolContext

logger = get_logger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _truncate_result(text: str, max_chars: int | None) -> str:
    """Bound model-facing agent output while preserving its conclusion."""
    if max_chars is None or max_chars <= 0 or len(text) <= max_chars:
        return text
    marker = "\n... [agent result truncated; see transcript for full output] ...\n"
    if max_chars <= len(marker):
        return text[-max_chars:]
    keep = max(0, max_chars - len(marker))
    head = keep // 3
    tail = keep - head
    return text[:head] + marker + text[-tail:]


@dataclass
class AgentSnapshot:
    agent_id: str
    parent_agent_id: str | None
    parent_tool_use_id: str | None
    title: str
    subagent_type: str
    status: str
    model: str
    created_at: str
    session_id: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    updated_at: str = field(default_factory=_now_iso)
    usage: dict[str, Any] = field(default_factory=dict)
    final_result: str = ""
    error: str = ""
    depth: int = 0
    transcript_path: str | None = None
    callback_enabled: bool = False
    callback_state: str = "disabled"
    callback_message_id: str | None = None
    callback_epoch: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "parent_agent_id": self.parent_agent_id,
            "parent_tool_use_id": self.parent_tool_use_id,
            "title": self.title,
            "subagent_type": self.subagent_type,
            "status": self.status,
            "model": self.model,
            "created_at": self.created_at,
            "session_id": self.session_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "updated_at": self.updated_at,
            "usage": dict(self.usage),
            "final_result": self.final_result,
            "error": self.error,
            "depth": self.depth,
            "transcript_path": self.transcript_path,
            "callback_enabled": self.callback_enabled,
            "callback_state": self.callback_state,
            "callback_message_id": self.callback_message_id,
            "callback_epoch": self.callback_epoch,
        }


@dataclass(frozen=True)
class AgentCompletion:
    """Immutable terminal result delivered to the owning session or agent."""

    session_id: str
    agent_id: str
    parent_agent_id: str | None
    parent_tool_use_id: str | None
    title: str
    subagent_type: str
    status: str
    final_result: str
    error: str
    usage: dict[str, Any]
    completed_at: str
    transcript_path: str | None
    callback_state: str = "pending"
    callback_message_id: str | None = None
    callback_epoch: int = 0

    @classmethod
    def from_snapshot(cls, snapshot: AgentSnapshot) -> "AgentCompletion":
        return cls(
            session_id=snapshot.session_id,
            agent_id=snapshot.agent_id,
            parent_agent_id=snapshot.parent_agent_id,
            parent_tool_use_id=snapshot.parent_tool_use_id,
            title=snapshot.title,
            subagent_type=snapshot.subagent_type,
            status=snapshot.status,
            final_result=snapshot.final_result,
            error=snapshot.error,
            usage=dict(snapshot.usage),
            completed_at=snapshot.finished_at or snapshot.updated_at,
            transcript_path=snapshot.transcript_path,
            callback_state=snapshot.callback_state,
            callback_message_id=snapshot.callback_message_id,
            callback_epoch=snapshot.callback_epoch,
        )


@dataclass
class _AgentRun:
    snapshot: AgentSnapshot
    task: asyncio.Task[None] | None = None
    messages: list[Message] = field(default_factory=list)
    active_model_profile: str | None = None
    permission_queue: asyncio.Queue[PermissionResponseEvent] = field(default_factory=asyncio.Queue)
    choice_queue: asyncio.Queue[ChoiceResponseEvent] = field(default_factory=asyncio.Queue)
    output_chunks: list[str] = field(default_factory=list)
    done_event: asyncio.Event = field(default_factory=asyncio.Event)
    final_text: str = ""
    cancelled: bool = False
    detached: bool = False


class AgentManager:
    """Owns all managed sub-agents for a CoreSession."""

    def __init__(
        self,
        *,
        settings: CrabCodeSettings,
        agent_settings: AgentSettings,
        tools_provider: Callable[[], list[Tool]],
        adapter_provider: Callable[[str | None], Any],
        event_sink: Callable[[CoreEvent], Awaitable[None]],
        permission_manager: Any,
        prompt_profile: PromptProfile | None,
        cwd: str,
        env: dict[str, str],
        session_id: str,
        completion_sink: Callable[[AgentCompletion], Awaitable[None]] | None = None,
        current_model_name: str | None = None,
        persistence_callback: Callable[[list[dict[str, Any]]], None] | None = None,
        transcript_writer: Callable[[str, list[Message]], None] | None = None,
        transcript_loader: Callable[[str], list[dict[str, Any]]] | None = None,
        transcript_path_getter: Callable[[str], str] | None = None,
        hook_manager: Any = None,
        lsp_manager: Any = None,
    ) -> None:
        self._settings = settings
        self._agent_settings = agent_settings
        self._tools_provider = tools_provider
        self._adapter_provider = adapter_provider
        self._event_sink = event_sink
        self._completion_sink = completion_sink
        self._permission_manager = permission_manager
        self._prompt_profile = prompt_profile
        self._cwd = cwd
        self._env = env
        self._session_id = session_id
        self._current_model_name = current_model_name
        self._persistence_callback = persistence_callback
        self._transcript_writer = transcript_writer
        self._transcript_loader = transcript_loader
        self._transcript_path_getter = transcript_path_getter
        self._hook_manager = hook_manager
        self._lsp_manager = lsp_manager
        self._team_manager: Any = None  # Set by CoreSession after construction
        self._runs: dict[str, _AgentRun] = {}
        self._lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(max(1, agent_settings.max_concurrency))

    @staticmethod
    def truncate_result(text: str, max_chars: int | None) -> str:
        return _truncate_result(text, max_chars)

    @staticmethod
    def format_snapshot(
        snapshot: AgentSnapshot,
        *,
        max_result_chars: int | None = None,
    ) -> str:
        result = AgentManager.truncate_result(
            snapshot.final_result.strip() or "(no result)",
            max_result_chars,
        )
        usage = snapshot.usage or {}
        usage_line = ", ".join(f"{k}={v}" for k, v in usage.items()) or "none"
        summary = [
            f"status: {snapshot.status}",
            f"agent_id: {snapshot.agent_id}",
            f"title: {snapshot.title}",
            f"subagent_type: {snapshot.subagent_type}",
            f"model: {snapshot.model or '(default)'}",
            f"usage: {usage_line}",
        ]
        if snapshot.error:
            summary.append(f"error: {snapshot.error}")
        if snapshot.transcript_path:
            summary.append(f"transcript_path: {snapshot.transcript_path}")
        summary.append("result:")
        summary.append(result)
        return "\n".join(summary)

    def list_agents(self) -> list[AgentSnapshot]:
        return [run.snapshot for run in sorted(self._runs.values(), key=lambda r: r.snapshot.created_at, reverse=True)]

    def get_agent(self, agent_id: str) -> AgentSnapshot | None:
        run = self._runs.get(agent_id)
        return run.snapshot if run else None

    def is_agent_active(self, agent_id: str) -> bool:
        run = self._runs.get(agent_id)
        return bool(run and run.task and not run.task.done())

    def get_agent_reply(self, agent_id: str, message_id: str) -> Message | None:
        run = self._runs.get(agent_id)
        return find_assistant_reply(run.messages, message_id) if run else None

    def has_agent_answered(self, agent_id: str, message_id: str) -> bool:
        return self.get_agent_reply(agent_id, message_id) is not None

    @property
    def max_output_chars(self) -> int:
        return max(1, self._agent_settings.max_output_chars)

    async def spawn_agent(
        self,
        *,
        prompt: str,
        subagent_type: str = "generalPurpose",
        name: str | None = None,
        model_profile: str | None = None,
        parent_agent_id: str | None = None,
        parent_tool_use_id: str | None = None,
        depth: int = 1,
        callback: bool = False,
    ) -> str:
        if callback and not self._session_id:
            raise RuntimeError("Cannot enable agent callback before a session is active")
        if depth > self._agent_settings.max_depth:
            raise ValueError(
                f"Maximum agent depth exceeded ({depth} > {self._agent_settings.max_depth})"
            )

        active_runs = sum(
            1
            for existing in self._runs.values()
            if existing.task is not None and not existing.task.done()
        )
        if active_runs >= self._agent_settings.max_active_agents_per_run:
            raise ValueError(
                "Maximum active agent count exceeded "
                f"({self._agent_settings.max_active_agents_per_run})"
            )

        profile_cfg = self._resolve_type_config(subagent_type)
        requested_model_profile = (
            model_profile if model_profile is not None else profile_cfg.model_profile
        )
        if (
            requested_model_profile is not None
            and requested_model_profile not in self._settings.models
        ):
            available = ", ".join(sorted(self._settings.models)) or "(none configured)"
            raise ValueError(
                f"Unknown model profile '{requested_model_profile}'. "
                "Model profiles must be names configured under settings.models. "
                f"Available profiles: {available}"
            )

        model_name = (
            requested_model_profile
            if requested_model_profile is not None
            else self._current_model_name
        )
        api_cfg = self._settings.get_api_config(model_name)
        agent_id = str(uuid.uuid4())
        snapshot = AgentSnapshot(
            agent_id=agent_id,
            parent_agent_id=parent_agent_id,
            parent_tool_use_id=parent_tool_use_id,
            title=(name or prompt.strip().splitlines()[0][:80] or f"{subagent_type} agent"),
            subagent_type=subagent_type,
            status="queued",
            model=api_cfg.model or "",
            created_at=_now_iso(),
            session_id=self._session_id,
            updated_at=_now_iso(),
            depth=depth,
            transcript_path=self._transcript_path_getter(agent_id) if self._transcript_path_getter else None,
            callback_enabled=callback,
            callback_state="pending" if callback else "disabled",
        )
        run = _AgentRun(
            snapshot=snapshot,
            messages=[create_user_message(content=prompt)],
            active_model_profile=model_name,
        )
        self._runs[agent_id] = run
        await self._emit_state(run, "queued", "Agent queued")
        run.task = asyncio.create_task(
            self._run_agent(
                run=run,
                model_profile=model_name,
                profile_cfg=profile_cfg,
            )
        )
        return agent_id

    async def wait_agent(
        self, agent_id: str, timeout_ms: int | None = None
    ) -> AgentSnapshot | None:
        run = self._runs.get(agent_id)
        if not run:
            return None
        timeout = None if timeout_ms is None else max(timeout_ms / 1000.0, 0)
        try:
            await asyncio.wait_for(run.done_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        return run.snapshot

    async def wait_any(
        self, agent_ids: list[str], timeout_ms: int | None = None
    ) -> AgentSnapshot | None:
        runs = [self._runs[agent_id] for agent_id in agent_ids if agent_id in self._runs]
        if not runs:
            return None
        waiter_map = {
            asyncio.create_task(run.done_event.wait()): run
            for run in runs
        }
        try:
            done, pending = await asyncio.wait(
                set(waiter_map),
                timeout=None if timeout_ms is None else max(timeout_ms / 1000.0, 0),
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in waiter_map:
                if not task.done():
                    task.cancel()
        if not done:
            return None
        first = next(iter(done))
        return waiter_map[first].snapshot

    async def cancel_agent(self, agent_id: str) -> bool:
        run = self._runs.get(agent_id)
        if not run or not run.task or run.task.done():
            return False
        run.cancelled = True
        run.task.cancel()
        try:
            await run.task
        except asyncio.CancelledError:
            pass
        return True

    async def send_input(
        self,
        agent_id: str,
        prompt: str,
        *,
        interrupt: bool = False,
        message_id: str | None = None,
        message_origin: str | None = None,
    ) -> bool:
        run = self._runs.get(agent_id)
        if not run:
            return False
        if not prompt.strip():
            return False

        existing_index = next(
            (
                index
                for index, message in enumerate(run.messages)
                if message_id is not None and message.uuid == message_id
            ),
            None,
        )
        if (
            run.snapshot.status in {"completed", "failed", "cancelled"}
            and run.snapshot.callback_enabled
            and run.snapshot.callback_state in {"pending", "injected"}
            and existing_index is None
        ):
            return False
        if existing_index is not None and find_assistant_reply(run.messages, message_id or ""):
            return True

        profile_cfg = self._resolve_type_config(run.snapshot.subagent_type)
        model_profile = run.active_model_profile

        if run.task and not run.task.done():
            if existing_index is not None:
                return True
            if not interrupt:
                return False
            run.task.cancel()
            try:
                await run.task
            except asyncio.CancelledError:
                pass

        if existing_index is None:
            message_kwargs: dict[str, Any] = {}
            if message_id:
                message_kwargs["uuid"] = message_id
            if message_origin:
                message_kwargs["origin"] = message_origin
            run.messages.append(create_user_message(content=prompt, **message_kwargs))
        run.done_event.clear()
        run.cancelled = False
        run.final_text = ""
        run.output_chunks.clear()
        run.snapshot.error = ""
        run.snapshot.final_result = ""
        run.snapshot.finished_at = None
        if run.snapshot.callback_enabled and existing_index is None:
            run.snapshot.callback_epoch += 1
            run.snapshot.callback_state = "pending"
            run.snapshot.callback_message_id = None
        run.snapshot.status = "queued"
        run.snapshot.updated_at = _now_iso()
        self._persist_transcript(run)
        await self._emit_state(run, "queued", "Agent received new input")
        run.task = asyncio.create_task(
            self._run_agent(
                run=run,
                model_profile=model_profile,
                profile_cfg=profile_cfg,
            )
        )
        return True

    async def route_permission(self, response: PermissionResponseEvent) -> bool:
        if not response.agent_id:
            return False
        run = self._runs.get(response.agent_id)
        if not run:
            return False
        await run.permission_queue.put(response)
        return True

    async def route_choice(self, response: ChoiceResponseEvent) -> bool:
        if not response.agent_id:
            return False
        run = self._runs.get(response.agent_id)
        if not run:
            return False
        await run.choice_queue.put(response)
        return True

    def update_session(self, *, env: dict[str, str], session_id: str) -> None:
        self._env = env
        self._session_id = session_id

    def abandon_active_agents(self, reason: str) -> None:
        """Cancel active runs and persist their terminal state before a session switch."""
        changed = False
        for run in self._runs.values():
            if not run.task or run.task.done():
                continue
            changed = True
            run.cancelled = True
            run.detached = True
            run.snapshot.status = "cancelled"
            run.snapshot.error = reason
            run.snapshot.updated_at = _now_iso()
            run.snapshot.finished_at = run.snapshot.updated_at
            run.done_event.set()
            run.task.cancel()
        if changed:
            self._persist()

    async def close(self) -> None:
        tasks: list[asyncio.Task[None]] = []
        for run in self._runs.values():
            if run.task and not run.task.done():
                run.cancelled = True
                run.detached = True
                run.task.cancel()
                tasks.append(run.task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            self._persist()

    def set_current_model(self, model_name: str | None) -> None:
        self._current_model_name = model_name

    def restore_snapshots(self, snapshots: list[dict[str, Any]]) -> list[AgentCompletion]:
        for run in self._runs.values():
            if run.task and not run.task.done():
                run.detached = True
                run.snapshot.callback_enabled = False
                run.snapshot.callback_state = "disabled"
                run.task.cancel()
        self._runs.clear()
        pending_callbacks: list[AgentCompletion] = []
        for item in snapshots:
            try:
                snapshot = AgentSnapshot(**item)
            except Exception:
                logger.warning("Skipping invalid agent snapshot during restore", exc_info=True)
                continue
            run = _AgentRun(snapshot=snapshot, active_model_profile=self._current_model_name)
            if self._transcript_loader is not None:
                raw_messages = self._transcript_loader(snapshot.agent_id)
                for raw in raw_messages:
                    message = message_from_entry(raw)
                    if message is not None:
                        run.messages.append(message)
            if snapshot.final_result:
                run.final_text = snapshot.final_result
            # Restored runs never have a live coroutine. Mark their waiter as settled
            # even when an older snapshot still says queued/running; callers can then
            # inspect that stale state instead of waiting forever for an impossible
            # transition. send_input() clears this event before starting a new run.
            run.done_event.set()
            if snapshot.status in {"completed", "failed", "cancelled"}:
                if snapshot.callback_enabled and snapshot.callback_state in {"pending", "injected"}:
                    pending_callbacks.append(AgentCompletion.from_snapshot(snapshot))
            self._runs[snapshot.agent_id] = run
        return pending_callbacks

    def mark_callback_injected(
        self,
        agent_id: str,
        *,
        session_id: str,
        message_id: str,
        callback_epoch: int,
    ) -> bool:
        run = self._runs.get(agent_id)
        if (
            not run
            or run.snapshot.callback_state != "pending"
            or run.snapshot.session_id != session_id
            or run.snapshot.callback_epoch != callback_epoch
        ):
            return False
        run.snapshot.callback_state = "injected"
        run.snapshot.callback_message_id = message_id
        run.snapshot.updated_at = _now_iso()
        self._persist()
        return True

    def mark_callback_delivered(
        self,
        agent_id: str,
        *,
        session_id: str,
        callback_epoch: int,
    ) -> bool:
        run = self._runs.get(agent_id)
        if (
            not run
            or run.snapshot.callback_state != "injected"
            or run.snapshot.session_id != session_id
            or run.snapshot.callback_epoch != callback_epoch
        ):
            return False
        run.snapshot.callback_state = "delivered"
        run.snapshot.updated_at = _now_iso()
        self._persist()
        return True

    def _persist(self) -> None:
        if self._persistence_callback is None:
            return
        self._persistence_callback([snapshot.to_dict() for snapshot in self.list_agents()])

    def _persist_transcript(self, run: _AgentRun) -> None:
        if self._transcript_writer is None:
            return
        self._transcript_writer(run.snapshot.agent_id, run.messages)

    def _resolve_type_config(self, subagent_type: str) -> AgentTypeConfig:
        cfg = self._agent_settings.types.get(subagent_type)
        if cfg:
            return cfg
        if subagent_type == "explore":
            return AgentTypeConfig(allowed_tools=[])
        return AgentTypeConfig()

    def _resolve_tools(self, subagent_type: str, profile_cfg: AgentTypeConfig) -> list[Tool]:
        tools = list(self._tools_provider())
        allowed = list(profile_cfg.allowed_tools)
        if not allowed and subagent_type == "explore":
            allowed = [tool.name for tool in tools if tool.is_read_only]
        if allowed:
            allowed_set = set(allowed)
            tools = [tool for tool in tools if tool.name in allowed_set]
        return tools

    async def _emit_state(self, run: _AgentRun, status: str, message: str) -> None:
        run.snapshot.status = status
        run.snapshot.updated_at = _now_iso()
        if status == "running" and run.snapshot.started_at is None:
            run.snapshot.started_at = run.snapshot.updated_at
        if status in {"completed", "failed", "cancelled"}:
            run.snapshot.finished_at = run.snapshot.updated_at
        if run.detached:
            return
        await self._event_sink(
            AgentStateEvent(
                agent_id=run.snapshot.agent_id,
                parent_agent_id=run.snapshot.parent_agent_id,
                status=run.snapshot.status,
                subagent_type=run.snapshot.subagent_type,
                title=run.snapshot.title,
                message=message,
                usage=run.snapshot.usage,
            )
        )
        self._persist()

    async def _emit_completion(self, run: _AgentRun) -> None:
        if (
            run.detached
            or self._completion_sink is None
            or not run.snapshot.callback_enabled
            or run.snapshot.callback_state != "pending"
        ):
            return
        try:
            await self._completion_sink(AgentCompletion.from_snapshot(run.snapshot))
        except Exception:
            logger.exception(
                "Failed to enqueue completion for managed agent %s",
                run.snapshot.agent_id,
            )

    async def _run_agent(
        self,
        *,
        run: _AgentRun,
        model_profile: str | None,
        profile_cfg: AgentTypeConfig,
    ) -> None:
        async with self._semaphore:
            try:
                await self._emit_state(run, "running", "Agent started")
                tools = self._resolve_tools(run.snapshot.subagent_type, profile_cfg)
                adapter = self._adapter_provider(model_profile)

                resolved_cw = 0
                if hasattr(adapter, "resolve_context_window"):
                    try:
                        resolved_cw = await adapter.resolve_context_window()
                    except Exception:
                        pass
                if resolved_cw == 0:
                    from crabcode_core.api.model_info import DEFAULT_CONTEXT_WINDOW
                    resolved_cw = DEFAULT_CONTEXT_WINDOW

                agent_api_config = None
                if model_profile and hasattr(self._settings, "get_api_config"):
                    agent_api_config = self._settings.get_api_config(model_profile)
                elif hasattr(self._settings, "get_api_config"):
                    agent_api_config = self._settings.get_api_config(None)

                agent_prompt = (
                    profile_cfg.prompt
                    if profile_cfg.prompt is not None
                    else resolve_agent_prompt(self._prompt_profile)
                )
                tool_context = ToolContext(
                    cwd=self._cwd,
                    messages=run.messages,
                    session_id=self._session_id,
                    env=self._env,
                    choice_queue=run.choice_queue,
                    tool_event_queue=asyncio.Queue(),
                    agent_id=run.snapshot.agent_id,
                    agent_depth=run.snapshot.depth,
                    agent_manager=self,
                    lsp_manager=self._lsp_manager if profile_cfg.enable_lsp else None,
                    team_manager=self._team_manager,
                )
                params = QueryParams(
                    messages=list(run.messages),
                    system_prompt=[agent_prompt],
                    user_context={},
                    system_context={},
                    tools=tools,
                    tool_context=tool_context,
                    api_adapter=adapter,
                    max_turns=self._agent_settings.max_turns,
                    permission_manager=self._permission_manager,
                    permission_queue=run.permission_queue,
                    hook_manager=self._hook_manager,
                    agent_mode="agent",
                    api_config=agent_api_config,
                    context_window=resolved_cw,
                    ai_reviewer=getattr(self, "_ai_reviewer", None),
                    tool_call_timeout=self._settings.tool_call_timeout,
                    auto_compact_enabled=self._settings.auto_compact_enabled,
                    compact_threshold=self._settings.max_context_length,
                    reply_to_uuid=(
                        run.messages[-1].uuid
                        if run.messages and run.messages[-1].origin == "task-notification"
                        else None
                    ),
                )
                final_usage: dict[str, Any] = {}
                async for event in query_loop(params):
                    await self._handle_agent_event(run, event)
                    if isinstance(event, TurnCompleteEvent):
                        final_usage = event.usage
                run.messages = params.messages
                run.snapshot.usage = dict(final_usage)
                run.snapshot.final_result = run.final_text.strip()
                self._persist_transcript(run)
                await self._emit_state(run, "completed", "Agent completed")
            except asyncio.CancelledError:
                run.snapshot.error = "cancelled"
                run.snapshot.final_result = run.final_text.strip()
                self._persist_transcript(run)
                await self._emit_state(run, "cancelled", "Agent cancelled")
            except Exception as exc:
                run.snapshot.error = str(exc)
                run.snapshot.final_result = run.final_text.strip()
                self._persist_transcript(run)
                await self._emit_state(run, "failed", f"Agent failed: {exc}")
                await self._event_sink(ErrorEvent(message=str(exc), recoverable=True, error_type="agent"))
            finally:
                run.done_event.set()
                await self._emit_completion(run)

    async def _handle_agent_event(self, run: _AgentRun, event: CoreEvent) -> None:
        agent_id = run.snapshot.agent_id
        if isinstance(event, StreamModeEvent):
            if event.mode == "thinking":
                await self._event_sink(
                    AgentOutputEvent(agent_id=agent_id, stream="thinking", text="thinking")
                )
            return
        if isinstance(event, StreamTextEvent):
            run.output_chunks.append(event.text)
            run.final_text += event.text
            await self._event_sink(
                AgentOutputEvent(agent_id=agent_id, stream="text", text=event.text)
            )
            return
        if isinstance(event, ToolUseEvent):
            await self._event_sink(
                ToolUseEvent(
                    tool_name=event.tool_name,
                    tool_input=event.tool_input,
                    tool_use_id=event.tool_use_id,
                    agent_id=agent_id,
                )
            )
            await self._event_sink(
                AgentOutputEvent(
                    agent_id=agent_id,
                    stream="tool_use",
                    text=event.tool_name,
                    tool_name=event.tool_name,
                )
            )
            return
        if isinstance(event, ToolResultEvent):
            await self._event_sink(
                ToolResultEvent(
                    tool_use_id=event.tool_use_id,
                    tool_name=event.tool_name,
                    result=event.result,
                    is_error=event.is_error,
                    result_for_display=event.result_for_display,
                    tool_input=event.tool_input,
                    agent_id=agent_id,
                )
            )
            return
        if isinstance(event, PermissionRequestEvent):
            await self._event_sink(
                PermissionRequestEvent(
                    tool_name=event.tool_name,
                    tool_input=event.tool_input,
                    tool_use_id=event.tool_use_id,
                    reason=event.reason,
                    permission_key=event.permission_key,
                    agent_id=agent_id,
                )
            )
            return
        if isinstance(event, ChoiceRequestEvent):
            await self._event_sink(
                ChoiceRequestEvent(
                    tool_use_id=event.tool_use_id,
                    question=event.question,
                    options=event.options,
                    multiple=event.multiple,
                    agent_id=agent_id,
                )
            )
            return
        if isinstance(event, ErrorEvent):
            run.snapshot.error = event.message
            await self._event_sink(event)
            return
        if isinstance(event, CompactEvent):
            await self._event_sink(
                CompactEvent(
                    summary=event.summary,
                    messages_before=event.messages_before,
                    messages_after=event.messages_after,
                    trigger=event.trigger,
                    agent_id=agent_id,
                )
            )
            return
        if isinstance(event, TurnCompleteEvent):
            # AgentStateEvent carries sub-agent completion. Forwarding this raw
            # event would be indistinguishable from the parent turn completing.
            return
        await self._event_sink(event)
