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
    create_assistant_message,
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
    # Named settings profile used for this run.  Persisting the profile keeps
    # send_input() on a resumed agent from silently falling back to the
    # session's current model.
    model_profile: str | None = None
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
            "model_profile": self.model_profile,
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
    # Internal ownership token.  It is deliberately not persisted in the
    # public snapshot: the manager assigns a fresh value whenever the active
    # session context changes, so a late completion from an abandoned run can
    # be rejected even when the replacement session reuses the same ID.
    run_generation: int | None = None

    @classmethod
    def from_snapshot(
        cls,
        snapshot: AgentSnapshot,
        *,
        run_generation: int | None = None,
    ) -> "AgentCompletion":
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
            run_generation=run_generation,
        )


@dataclass
class _AgentRun:
    snapshot: AgentSnapshot
    task: asyncio.Task[None] | None = None
    messages: list[Message] = field(default_factory=list)
    active_model_profile: str | None = None
    # Capture session-scoped execution context at spawn time.  The owning
    # CoreSession can switch sessions while cancellation is being delivered;
    # an in-flight run must continue to identify itself as the old run.
    run_cwd: str = "."
    run_env: dict[str, str] = field(default_factory=dict)
    run_generation: int = 0
    # Private routing identity for the foreground event stream that launched
    # (or most recently restarted) this run. It is process-local by design and
    # must never be serialized with AgentSnapshot.
    event_stream_token: object | None = None
    run_settings: CrabCodeSettings | None = None
    run_agent_settings: AgentSettings | None = None
    run_permission_manager: Any = None
    run_prompt_profile: PromptProfile | None = None
    run_hook_manager: Any = None
    run_lsp_manager: Any = None
    run_ai_reviewer: Any = None
    run_team_manager: Any = None
    run_tools: list[Tool] = field(default_factory=list)
    run_adapter: Any = None
    start_event: asyncio.Event = field(default_factory=asyncio.Event)
    control_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
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
        ai_reviewer: Any = None,
        schedule_manager: Any = None,
        event_stream_token_provider: Callable[[], object | None] | None = None,
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
        self._ai_reviewer = ai_reviewer
        self._schedule_manager = schedule_manager
        self._event_stream_token_provider = event_stream_token_provider
        self._team_manager: Any = None  # Set by CoreSession after construction
        self._runs: dict[str, _AgentRun] = {}
        # Runs disappear from _runs when a different session projection is
        # restored, but their coroutines remain owned by this manager until
        # they actually terminate.
        self._detached_tasks: set[asyncio.Task[None]] = set()
        self._lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None
        self._semaphore = asyncio.Semaphore(max(1, agent_settings.max_concurrency))
        self._closed = False
        self._session_generation = 0

    def _capture_event_stream_token(self) -> object | None:
        provider = self._event_stream_token_provider
        if provider is None:
            return None
        try:
            return provider()
        except Exception:
            logger.warning("Managed-agent event token provider failed", exc_info=True)
            return None

    @staticmethod
    def truncate_result(text: str, max_chars: int | None) -> str:
        return _truncate_result(text, max_chars)

    @staticmethod
    def _consume_task_result(task: asyncio.Task[Any]) -> None:
        """Drain terminal exceptions from detached agent tasks.

        Agent tasks are intentionally fire-and-forget from the caller's point
        of view.  A provider or event sink failure must still be observable in
        logs, but it must not produce an unhandled-task warning during later
        session shutdown.
        """
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception:
            logger.error("Managed agent task failed", exc_info=True)

    def _track_detached_task(self, task: asyncio.Task[None] | None) -> None:
        if task is None or task.done():
            return
        self._detached_tasks.add(task)
        task.add_done_callback(self._detached_tasks.discard)

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
        if snapshot.callback_enabled:
            callback = (
                f"{snapshot.callback_state} (epoch={snapshot.callback_epoch})"
            )
            if snapshot.callback_message_id:
                callback += f", message_id={snapshot.callback_message_id}"
            summary.append(f"callback: {callback}")
        summary.append("result:")
        summary.append(result)
        return "\n".join(summary)

    def list_agents(self) -> list[AgentSnapshot]:
        return [
            run.snapshot
            for run in sorted(
                self._runs.values(),
                key=lambda r: r.snapshot.created_at,
                reverse=True,
            )
            if not self._session_id or run.snapshot.session_id == self._session_id
        ]

    def get_agent(self, agent_id: str) -> AgentSnapshot | None:
        run = self._runs.get(agent_id)
        if run is None:
            return None
        if self._session_id and run.snapshot.session_id != self._session_id:
            return None
        return run.snapshot

    def is_agent_active(self, agent_id: str) -> bool:
        run = self._runs.get(agent_id)
        return bool(
            run
            and (not self._session_id or run.snapshot.session_id == self._session_id)
            and run.task
            and not run.task.done()
        )

    def get_agent_reply(self, agent_id: str, message_id: str) -> Message | None:
        run = self._runs.get(agent_id)
        if run is not None and self._session_id and run.snapshot.session_id != self._session_id:
            return None
        return find_assistant_reply(run.messages, message_id) if run else None

    def _is_current_run(self, run: _AgentRun) -> bool:
        """Return whether *run* is still owned by the active session.

        Session switches are synchronous from the manager's point of view, but
        callers such as ``send_input`` can be suspended while a task is being
        cancelled or while an event sink is consuming a state event.  Checking
        the map as well as the session id prevents those stale callers from
        resurrecting a detached run after the switch.
        """
        return (
            not self._closed
            and not run.detached
            and self._runs.get(run.snapshot.agent_id) is run
            and bool(self._session_id)
            and run.snapshot.session_id == self._session_id
            and run.run_generation == self._session_generation
        )

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
        if self._closed:
            raise RuntimeError("Agent manager is closed")
        if not self._session_id:
            raise RuntimeError("Cannot spawn an agent before a session is active")

        # Reserve the active-agent slot before the first await.  Counting only
        # live asyncio tasks allowed concurrent callers to all pass the limit
        # while their runs were still being queued.
        async with self._lock:
            if self._closed:
                raise RuntimeError("Agent manager is closed")
            if depth > self._agent_settings.max_depth:
                raise ValueError(
                    f"Maximum agent depth exceeded ({depth} > {self._agent_settings.max_depth})"
                )

            active_runs = sum(
                1
                for existing in self._runs.values()
                if not existing.done_event.is_set()
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
            # Capture all mutable runtime dependencies before the first await.
            # A session switch can replace these manager fields while a
            # provider is still unwinding cancellation; old runs must keep the
            # resources they were created with and never execute in the new
            # project's context.
            run_adapter = self._adapter_provider(model_name)
            run_tools = list(self._tools_provider())
            agent_id = str(uuid.uuid4())
            snapshot = AgentSnapshot(
                agent_id=agent_id,
                parent_agent_id=parent_agent_id,
                parent_tool_use_id=parent_tool_use_id,
                title=(name or prompt.strip().splitlines()[0][:80] or f"{subagent_type} agent"),
                subagent_type=subagent_type,
                status="queued",
                model=api_cfg.model or "",
                model_profile=model_name,
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
                run_cwd=self._cwd,
                run_env=dict(self._env),
                run_generation=self._session_generation,
                event_stream_token=self._capture_event_stream_token(),
                run_settings=self._settings,
                run_agent_settings=self._agent_settings,
                run_permission_manager=self._permission_manager,
                run_prompt_profile=self._prompt_profile,
                run_hook_manager=self._hook_manager,
                run_lsp_manager=self._lsp_manager,
                run_ai_reviewer=self._ai_reviewer,
                run_team_manager=self._team_manager,
                run_tools=run_tools,
                run_adapter=run_adapter,
            )
            self._runs[agent_id] = run
            # Create the task before yielding so cancel_agent can cancel a run
            # while its initial queued event is being delivered.  The guarded
            # runner waits on start_event to preserve queued -> running order.
            run.task = asyncio.create_task(
                self._run_agent_guarded(
                    run=run,
                    model_profile=model_name,
                    profile_cfg=profile_cfg,
                )
            )
            run.task.add_done_callback(self._consume_task_result)

        try:
            await self._emit_state(run, "queued", "Agent queued")
            async with self._lock:
                valid = (
                    self._runs.get(agent_id) is run
                    and not run.detached
                    and not self._closed
                    and run.snapshot.session_id == self._session_id
                    and run.run_generation == self._session_generation
                )
                run.start_event.set()
                task = run.task
        except BaseException:
            async with self._lock:
                run.cancelled = True
                run.detached = True
                run.start_event.set()
                task = run.task
                # The queued-state sink can raise CancelledError before the
                # newly created task gets its first scheduling turn. In that
                # case _run_agent_guarded() never enters its cancellation
                # handler, so settle and unregister the failed reservation
                # here instead of leaking an active-agent slot forever.
                if self._runs.get(agent_id) is run:
                    self._runs.pop(agent_id, None)
                run.done_event.set()
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            raise
        if not valid:
            async with self._lock:
                run.cancelled = True
                run.detached = True
                if self._runs.get(agent_id) is run:
                    self._runs.pop(agent_id, None)
                run.done_event.set()
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            raise RuntimeError("Session changed while spawning agent")
        return agent_id

    async def wait_agent(
        self, agent_id: str, timeout_ms: int | None = None
    ) -> AgentSnapshot | None:
        run = self._runs.get(agent_id)
        if not run or (
            self._session_id and run.snapshot.session_id != self._session_id
        ):
            return None
        # A task can be terminal while its coroutine's final callback has not
        # published ``done_event`` yet (notably for integrations that provide
        # their own task or when cancellation lands at the task boundary).
        # Fence that state before waiting so callers cannot block forever on a
        # run that has already stopped executing.
        self._fence_finished_task(run)
        timeout = None if timeout_ms is None else max(timeout_ms / 1000.0, 0)
        try:
            # ``wait_for(..., timeout=0)`` can time out before a waiter gets a
            # scheduling turn, even when the event was set already. Inspect
            # the state synchronously first so a zero-timeout poll remains a
            # useful non-blocking completion check.
            if not run.done_event.is_set():
                if timeout == 0:
                    return None
                await asyncio.wait_for(run.done_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        # A session restore can replace an agent with the same id (for
        # example, when a persisted snapshot is reloaded).  The old waiter
        # must not return that run's terminal state as if it belonged to the
        # replacement.  Keep the historical behavior for a run that was
        # removed altogether: restore_snapshots() settles such waiters by
        # setting done_event before dropping the map entry.
        current = self._runs.get(agent_id)
        if current is not None and (
            current is not run
            or run.detached
            or run.run_generation != self._session_generation
        ):
            return None
        if self._session_id and run.snapshot.session_id != self._session_id:
            return None
        return run.snapshot

    async def wait_any(
        self, agent_ids: list[str], timeout_ms: int | None = None
    ) -> AgentSnapshot | None:
        runs = [
            self._runs[agent_id]
            for agent_id in agent_ids
            if agent_id in self._runs
            and (
                not self._session_id
                or self._runs[agent_id].snapshot.session_id == self._session_id
            )
        ]
        if not runs:
            return None
        for run in runs:
            self._fence_finished_task(run)
        timeout = None if timeout_ms is None else max(timeout_ms / 1000.0, 0)

        def _valid_snapshot(run: _AgentRun) -> AgentSnapshot | None:
            current = self._runs.get(run.snapshot.agent_id)
            if current is not None and (
                current is not run
                or run.detached
                or run.run_generation != self._session_generation
            ):
                return None
            if self._session_id and run.snapshot.session_id != self._session_id:
                return None
            return run.snapshot

        # Handle completed runs before creating zero-timeout waiter tasks. A
        # task scheduled by a just-finished run gets one event-loop turn below,
        # which keeps timeout_ms=0 deterministic without blocking.
        for run in runs:
            if run.done_event.is_set():
                snapshot = _valid_snapshot(run)
                if snapshot is not None:
                    return snapshot
        if timeout == 0:
            await asyncio.sleep(0)
            for run in runs:
                if run.done_event.is_set():
                    snapshot = _valid_snapshot(run)
                    if snapshot is not None:
                        return snapshot
            return None

        waiter_map = {
            asyncio.create_task(run.done_event.wait()): run
            for run in runs
        }
        done: set[asyncio.Task[bool]] = set()
        try:
            done, _pending = await asyncio.wait(
                set(waiter_map),
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            pending_waiters = [task for task in waiter_map if not task.done()]
            for task in pending_waiters:
                task.cancel()
            if pending_waiters:
                await asyncio.gather(*pending_waiters, return_exceptions=True)
        if not done:
            return None
        # Prefer a still-owned completed run.  A same-id replacement can race
        # the wake-up; returning its predecessor would let callers act on a
        # stale snapshot.  If the old map entry was removed completely, retain
        # the established settled-waiter behavior used by session restore.
        valid_run = None
        for waiter in done:
            candidate = waiter_map[waiter]
            if _valid_snapshot(candidate) is not None:
                valid_run = candidate
                break
        if valid_run is None:
            return None
        snapshot = valid_run.snapshot
        if self._session_id and snapshot.session_id != self._session_id:
            return None
        return snapshot

    @staticmethod
    def _fence_finished_task(run: _AgentRun) -> None:
        """Wake waiters when a terminal task missed its final event fence.

        Managed runs normally set ``done_event`` from their ``finally`` block,
        but callers may inject a completed task or observe the tiny scheduling
        window between task completion and a done callback.  Setting the event
        is idempotent and prevents that run from retaining waiters forever.
        """
        task = run.task
        if task is not None and task.done() and not run.done_event.is_set():
            run.done_event.set()

    async def cancel_agent(self, agent_id: str) -> bool:
        if self._closed:
            return False
        run = self._runs.get(agent_id)
        if not run or (
            self._session_id and run.snapshot.session_id != self._session_id
        ):
            return False
        async with run.control_lock:
            # The session can be replaced while waiting to acquire this lock.
            # Re-check ownership before touching messages or cancelling the
            # previous task; otherwise a callback from the old session can
            # restart a detached agent in the new session.
            if not self._is_current_run(run):
                return False
            task = run.task
            if task is None:
                self._fence_finished_task(run)
                return False
            if task.done():
                self._fence_finished_task(run)
                return False
            run.cancelled = True
            run.start_event.set()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            if not self._is_current_run(run):
                return False
            await self._finalize_cancelled_run(run)
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
        if self._closed:
            return False
        run = self._runs.get(agent_id)
        if not run or (
            self._session_id and run.snapshot.session_id != self._session_id
        ):
            return False
        if not prompt.strip():
            return False

        # Serialize restart/callback-input operations for one run. Without a
        # per-run lock, two concurrent completion deliveries could both cancel
        # the old task, append duplicate messages, and launch two replacements.
        async with run.control_lock:
            if not self._is_current_run(run):
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
                run.snapshot.status in {"completed", "failed", "stopped", "cancelled"}
                and run.snapshot.callback_enabled
                and run.snapshot.callback_state in {"pending", "injected"}
                and existing_index is None
            ):
                return False
            if existing_index is not None and find_assistant_reply(run.messages, message_id or ""):
                return True

            profile_cfg = self._resolve_type_config(run.snapshot.subagent_type)
            model_profile = run.active_model_profile

            # Treat a completed task as historical before clearing the event
            # for a replacement run.  Without this fence a stale task that
            # missed its final callback could wake waiters for the new run.
            self._fence_finished_task(run)
            if run.task and not run.task.done():
                if existing_index is not None:
                    return True
                if not interrupt:
                    return False
                run.cancelled = True
                run.start_event.set()
                run.task.cancel()
                try:
                    await run.task
                except asyncio.CancelledError:
                    pass

                # ``abandon_active_agents``/``restore_snapshots`` may have
                # switched the session while cancellation was in flight.
                if not self._is_current_run(run):
                    return False

            # The run may have become detached while the caller was waiting on
            # the previous state event.  Keep this check immediately before
            # mutating the message history as well as after the event below.
            if not self._is_current_run(run):
                return False

            if existing_index is None:
                message_kwargs: dict[str, Any] = {}
                if message_id:
                    message_kwargs["uuid"] = message_id
                if message_origin:
                    message_kwargs["origin"] = message_origin
                run.messages.append(create_user_message(content=prompt, **message_kwargs))
            run.done_event.clear()
            run.cancelled = False
            run.detached = False
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
            run.start_event = asyncio.Event()
            # A continuation is a new execution boundary. In particular, a
            # foreground AgentSendInput tool must not retain the token from the
            # turn that originally spawned this agent.
            run.event_stream_token = self._capture_event_stream_token()
            self._persist_transcript(run)
            try:
                await self._emit_state(run, "queued", "Agent received new input")
            except BaseException:
                # Cancellation can arrive while a frontend is consuming the
                # queued-state event, before the replacement task is created.
                # Settle the run here so it cannot occupy an active slot or
                # leave wait_agent() blocked forever.
                run.cancelled = True
                run.start_event.set()
                if not run.detached:
                    run.snapshot.status = "stopped"
                    run.snapshot.error = "stopped"
                    run.snapshot.updated_at = _now_iso()
                    run.snapshot.finished_at = run.snapshot.updated_at
                run.done_event.set()
                self._persist()
                raise
            if not self._is_current_run(run):
                # A session restore can clear the run map while the queued
                # state event is awaiting a frontend sink. Do not resurrect
                # that stale run after the switch.
                run.cancelled = True
                run.start_event.set()
                run.done_event.set()
                return False
            run.task = asyncio.create_task(
                self._run_agent_guarded(
                    run=run,
                    model_profile=model_profile,
                    profile_cfg=profile_cfg,
                )
            )
            run.task.add_done_callback(self._consume_task_result)
            run.start_event.set()
            return True

    async def route_permission(self, response: PermissionResponseEvent) -> bool:
        if not response.agent_id:
            return False
        run = self._runs.get(response.agent_id)
        if (
            not run
            or not self._is_current_run(run)
            or run.done_event.is_set()
            or run.task is None
            or run.task.done()
            or run.snapshot.status not in {"queued", "running"}
        ):
            return False
        await run.permission_queue.put(response)
        return True

    async def route_choice(self, response: ChoiceResponseEvent) -> bool:
        if not response.agent_id:
            return False
        run = self._runs.get(response.agent_id)
        if (
            not run
            or not self._is_current_run(run)
            or run.done_event.is_set()
            or run.task is None
            or run.task.done()
            or run.snapshot.status not in {"queued", "running"}
        ):
            return False
        await run.choice_queue.put(response)
        return True

    def update_session(
        self,
        *,
        env: dict[str, str],
        session_id: str,
        cwd: str | None = None,
        force_generation: bool = False,
    ) -> None:
        """Switch the manager's default context for subsequently spawned runs."""
        if (
            force_generation
            or
            session_id != self._session_id
            or dict(env) != self._env
            or (cwd is not None and cwd != self._cwd)
        ):
            self._session_generation += 1
        self._env = dict(env)
        self._session_id = session_id
        if cwd is not None:
            self._cwd = cwd

    def abandon_active_agents(self, reason: str) -> None:
        """Cancel active runs and persist their terminal state before a session switch."""
        changed = False
        for run in self._runs.values():
            if run.done_event.is_set():
                continue
            changed = True
            run.cancelled = True
            run.detached = True
            run.start_event.set()
            if run.snapshot.status not in {"completed", "failed", "stopped", "cancelled"}:
                run.snapshot.status = "stopped"
                run.snapshot.error = reason
                run.snapshot.updated_at = _now_iso()
                run.snapshot.finished_at = run.snapshot.updated_at
            run.done_event.set()
            if run.task is not None and not run.task.done():
                self._track_detached_task(run.task)
                run.task.cancel()
        if changed:
            self._persist()

    async def wait_for_detached_agents(self) -> None:
        """Wait until runs detached by a session switch have stopped.

        ``abandon_active_agents`` is intentionally synchronous so the
        synchronous ``new_session`` API can mark ownership immediately.  An
        async resume can use this companion method before tearing down
        cwd-scoped resources such as LSP/MCP clients.
        """
        tasks = [task for task in self._detached_tasks if not task.done()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self) -> None:
        """Cancel and settle all managed runs exactly once.

        The cleanup task is owned by the manager rather than by the caller.
        If a transport request awaiting ``close`` is cancelled, a later caller
        still joins the same task instead of observing ``_closed`` and
        returning while agent tasks are alive.
        """
        async with self._close_lock:
            if self._close_task is None:
                if self._closed:
                    return
                # Mark the manager closed before scheduling teardown so no new
                # run can be admitted while the owned cleanup task is pending.
                self._closed = True
                self._close_task = asyncio.create_task(self._close_impl())
                self._close_task.add_done_callback(self._finish_close_task)
            task = self._close_task
        await asyncio.shield(task)

    def _finish_close_task(self, task: asyncio.Task[None]) -> None:
        """Consume terminal errors from an owned cleanup task."""
        if task.cancelled():
            return
        try:
            task.exception()
        except Exception:
            logger.warning("Managed agent cleanup task failed", exc_info=True)

    async def _close_impl(self) -> None:
        current_task = asyncio.current_task()
        active_runs: list[_AgentRun] = []
        owned_tasks: set[asyncio.Task[None]] = {
            task for task in self._detached_tasks if not task.done()
        }
        # Serialize the snapshot/mark phase with spawn_agent so a run cannot
        # be inserted into ``_runs`` while this loop is traversing the dict.
        async with self._lock:
            runs = list(self._runs.values())
            for run in runs:
                if run.done_event.is_set():
                    continue
                if run.task and not run.task.done():
                    run.cancelled = True
                    run.detached = True
                    run.start_event.set()
                    if run.task is not current_task:
                        self._track_detached_task(run.task)
                        run.task.cancel()
                        active_runs.append(run)
                        owned_tasks.add(run.task)
                else:
                    # A task can already be terminal while its finally block
                    # has not published done_event yet.  Settle waiters before
                    # close returns instead of leaving them attached forever.
                    run.done_event.set()
        for task in owned_tasks:
            if task is not current_task and not task.done():
                task.cancel()
        if owned_tasks:
            await asyncio.gather(
                *owned_tasks,
                return_exceptions=True,
            )
        if active_runs:
            for run in active_runs:
                await self._finalize_cancelled_run(run)
        for run in runs:
            if not run.done_event.is_set():
                run.done_event.set()
        self._persist()

    def set_current_model(self, model_name: str | None) -> None:
        self._current_model_name = model_name

    def restore_snapshots(self, snapshots: list[dict[str, Any]]) -> list[AgentCompletion]:
        if self._closed:
            return []
        for run in self._runs.values():
            if run.task and not run.task.done():
                run.detached = True
                run.cancelled = True
                run.snapshot.callback_enabled = False
                run.snapshot.callback_state = "disabled"
                run.start_event.set()
                self._track_detached_task(run.task)
                run.task.cancel()
            if run.done_event.is_set():
                continue
            # The old run is about to be removed from the map. Wake any
            # caller that still holds its handle immediately, including the
            # task-done/finally-not-yet-run edge case.
            run.done_event.set()
        self._runs.clear()
        pending_callbacks: list[AgentCompletion] = []
        changed = False
        for item in snapshots:
            try:
                snapshot = AgentSnapshot(**item)
            except Exception:
                logger.warning("Skipping invalid agent snapshot during restore", exc_info=True)
                # Drop malformed entries from the persisted projection so a
                # bad record does not trigger the same warning on every resume.
                changed = True
                continue
            if snapshot.session_id and self._session_id and snapshot.session_id != self._session_id:
                logger.warning(
                    "Skipping agent %s restored for session %s while active session is %s",
                    snapshot.agent_id,
                    snapshot.session_id,
                    self._session_id,
                )
                # Persist the filtered projection below so stale snapshots do
                # not get reconsidered (and warned about) on every resume.
                changed = True
                continue
            if not snapshot.session_id:
                # Snapshots written by pre-session-id versions can still be
                # resumed, but must be bound to the active session now.
                snapshot.session_id = self._session_id
            if snapshot.status in {"queued", "running"}:
                # Background agents are owned by the session process. Once that
                # process is gone there is no coroutine to resume, so never expose
                # a phantom running task after --resume. Settle it as stopped and,
                # when callbacks were enabled, deliver that terminal state exactly
                # like any other completion.
                snapshot.status = "stopped"
                snapshot.error = snapshot.error or "stopped because the session ended"
                snapshot.updated_at = _now_iso()
                snapshot.finished_at = snapshot.updated_at
                changed = True
            run = _AgentRun(
                snapshot=snapshot,
                active_model_profile=(
                    snapshot.model_profile
                    if snapshot.model_profile in self._settings.models
                    else self._current_model_name
                ),
                run_cwd=self._cwd,
                run_env=dict(self._env),
                run_generation=self._session_generation,
                run_settings=self._settings,
                run_agent_settings=self._agent_settings,
                run_permission_manager=self._permission_manager,
                run_prompt_profile=self._prompt_profile,
                run_hook_manager=self._hook_manager,
                run_lsp_manager=self._lsp_manager,
                run_ai_reviewer=self._ai_reviewer,
                run_team_manager=self._team_manager,
                run_tools=list(self._tools_provider()),
            )
            if self._transcript_loader is not None:
                try:
                    raw_messages = self._transcript_loader(snapshot.agent_id)
                except Exception:
                    logger.warning(
                        "Failed to load transcript for restored agent %s",
                        snapshot.agent_id,
                        exc_info=True,
                    )
                    raw_messages = []
                if not isinstance(raw_messages, list):
                    raw_messages = []
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
            if snapshot.status in {"completed", "failed", "stopped", "cancelled"}:
                if snapshot.callback_enabled and snapshot.callback_state in {"pending", "injected"}:
                    pending_callbacks.append(
                        AgentCompletion.from_snapshot(
                            snapshot,
                            run_generation=run.run_generation,
                        )
                    )
            self._runs[snapshot.agent_id] = run
        if changed:
            self._persist()
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
        snapshots = self.list_agents()
        if self._session_id:
            snapshots = [
                snapshot
                for snapshot in snapshots
                if snapshot.session_id == self._session_id
            ]
        try:
            self._persistence_callback([snapshot.to_dict() for snapshot in snapshots])
        except Exception:
            # Persistence is important for resume, but a transient disk error
            # must not terminate the running agent coroutine and strand its
            # waiters before the terminal state is set.
            logger.warning("Failed to persist managed-agent snapshots", exc_info=True)

    def _persist_transcript(self, run: _AgentRun) -> None:
        # A detached run belongs to a previous session. Its cancellation can
        # arrive after CoreSession has switched this manager to a new storage
        # object, so never write that stale run through the new writer.
        if (
            self._transcript_writer is None
            or run.detached
            or run.snapshot.session_id != self._session_id
            or run.run_generation != self._session_generation
        ):
            return
        try:
            self._transcript_writer(run.snapshot.agent_id, run.messages)
        except Exception:
            logger.warning(
                "Failed to persist transcript for managed agent %s",
                run.snapshot.agent_id,
                exc_info=True,
            )

    def _capture_partial_output(
        self,
        run: _AgentRun,
        params: QueryParams | None,
        baseline_message_ids: set[str] | None = None,
    ) -> None:
        """Retain a streamed assistant prefix when a run is interrupted."""
        if params is not None:
            run.messages = params.messages
        if run.detached or not run.final_text.strip():
            return
        # ``query_loop`` can complete one or more model/tool turns before the
        # cancellation arrives. Those assistant messages are already in the
        # projection, while ``final_text`` contains the concatenated stream for
        # the whole run. Append only the uncommitted suffix; appending the
        # cumulative value would duplicate text from earlier tool turns.
        committed_text = ""
        baseline = baseline_message_ids or set()
        for message in run.messages:
            if (
                getattr(message, "role", None) == "assistant"
                and getattr(message, "uuid", None) not in baseline
            ):
                committed_text += getattr(message, "text_content", "")
        partial_text = run.final_text
        if committed_text:
            if partial_text.startswith(committed_text):
                partial_text = partial_text[len(committed_text):]
            elif partial_text == committed_text:
                partial_text = ""
        if not partial_text.strip():
            return
        parent_uuid = run.messages[-1].uuid if run.messages else None
        run.messages.append(
            create_assistant_message(
                content=partial_text,
                parent_uuid=parent_uuid,
            )
        )

    def _resolve_type_config(self, subagent_type: str) -> AgentTypeConfig:
        cfg = self._agent_settings.types.get(subagent_type)
        if cfg:
            return cfg
        if subagent_type == "explore":
            return AgentTypeConfig(allowed_tools=[])
        return AgentTypeConfig()

    def _resolve_tools(
        self,
        subagent_type: str,
        profile_cfg: AgentTypeConfig,
        base_tools: list[Tool] | None = None,
    ) -> list[Tool]:
        tools = list(self._tools_provider()) if base_tools is None else list(base_tools)
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
        if status in {"completed", "failed", "stopped", "cancelled"}:
            run.snapshot.finished_at = run.snapshot.updated_at
        if not self._is_current_run(run):
            return
        await self._safe_event_sink(
            AgentStateEvent(
                agent_id=run.snapshot.agent_id,
                parent_agent_id=run.snapshot.parent_agent_id,
                status=run.snapshot.status,
                subagent_type=run.snapshot.subagent_type,
                title=run.snapshot.title,
                message=message,
                usage=run.snapshot.usage,
            ),
            run=run,
        )
        if not self._is_current_run(run):
            return
        self._persist()

    async def _safe_event_sink(
        self,
        event: CoreEvent,
        *,
        run: _AgentRun | None = None,
    ) -> None:
        """Forward an event without letting a broken client sink kill a run."""
        if run is not None and not self._is_current_run(run):
            return
        if run is not None:
            # CoreSession uses these private routing hints to reject an event
            # that was already inside an async sink when the session switched.
            # They are intentionally not part of the public event schema.
            try:
                setattr(event, "_crabcode_session_id", run.snapshot.session_id)
                setattr(event, "_crabcode_session_generation", run.run_generation)
                setattr(event, "_crabcode_event_stream_token", run.event_stream_token)
            except Exception:
                pass
        try:
            await self._event_sink(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "Managed-agent event sink failed for %s",
                type(event).__name__,
                exc_info=True,
            )

    async def _emit_completion(self, run: _AgentRun) -> None:
        if (
            not self._is_current_run(run)
            or self._completion_sink is None
            or not run.snapshot.callback_enabled
            or run.snapshot.callback_state != "pending"
        ):
            return
        try:
            await self._completion_sink(
                AgentCompletion.from_snapshot(
                    run.snapshot,
                    run_generation=run.run_generation,
                )
            )
        except Exception:
            logger.exception(
                "Failed to enqueue completion for managed agent %s",
                run.snapshot.agent_id,
            )

    async def _run_agent_guarded(
        self,
        *,
        run: _AgentRun,
        model_profile: str | None,
        profile_cfg: AgentTypeConfig,
    ) -> None:
        """Finalize cancellation even if it happens while waiting for a slot."""
        try:
            await run.start_event.wait()
            if run.cancelled or run.done_event.is_set():
                return
            await self._run_agent(
                run=run,
                model_profile=model_profile,
                profile_cfg=profile_cfg,
            )
        except asyncio.CancelledError:
            # _run_agent handles cancellation after entering the semaphore. A
            # queued task can be cancelled before that inner try/finally starts.
            await self._finalize_cancelled_run(run)

    async def _finalize_cancelled_run(self, run: _AgentRun) -> None:
        """Settle a cancelled run exactly once, including never-started tasks."""
        if run.done_event.is_set():
            return
        if not run.detached:
            run.snapshot.error = "stopped"
        run.snapshot.final_result = run.final_text.strip()
        self._persist_transcript(run)
        try:
            await self._emit_state(run, "stopped", "Agent stopped")
        finally:
            run.done_event.set()
            await self._emit_completion(run)

    async def _run_agent(
        self,
        *,
        run: _AgentRun,
        model_profile: str | None,
        profile_cfg: AgentTypeConfig,
    ) -> None:
        async with self._semaphore:
            params: QueryParams | None = None
            baseline_message_ids = {message.uuid for message in run.messages}
            try:
                await self._emit_state(run, "running", "Agent started")
                settings = (
                    run.run_settings
                    if run.run_settings is not None
                    else self._settings
                )
                agent_settings = (
                    run.run_agent_settings
                    if run.run_agent_settings is not None
                    else self._agent_settings
                )
                tools = self._resolve_tools(
                    run.snapshot.subagent_type,
                    profile_cfg,
                    run.run_tools,
                )
                adapter = (
                    run.run_adapter
                    if run.run_adapter is not None
                    else self._adapter_provider(model_profile)
                )

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
                if model_profile and hasattr(settings, "get_api_config"):
                    agent_api_config = settings.get_api_config(model_profile)
                elif hasattr(settings, "get_api_config"):
                    agent_api_config = settings.get_api_config(None)

                agent_prompt = (
                    profile_cfg.prompt
                    if profile_cfg.prompt is not None
                    else resolve_agent_prompt(
                        run.run_prompt_profile
                        if run.run_prompt_profile is not None
                        else self._prompt_profile
                    )
                )
                tool_context = ToolContext(
                    cwd=run.run_cwd,
                    messages=run.messages,
                    session_id=run.snapshot.session_id,
                    env=run.run_env,
                    choice_queue=run.choice_queue,
                    tool_event_queue=asyncio.Queue(),
                    agent_id=run.snapshot.agent_id,
                    agent_depth=run.snapshot.depth,
                    agent_manager=self,
                    lsp_manager=(
                        run.run_lsp_manager if profile_cfg.enable_lsp else None
                    ),
                    team_manager=(
                        run.run_team_manager
                        if run.run_team_manager is not None
                        else self._team_manager
                    ),
                    schedule_manager=self._schedule_manager,
                    snapshot_enabled=settings.snapshot.enabled,
                    snapshot_max_size_mb=settings.snapshot.max_size_mb,
                )
                params = QueryParams(
                    messages=list(run.messages),
                    system_prompt=[agent_prompt],
                    user_context={},
                    system_context={},
                    tools=tools,
                    tool_context=tool_context,
                    api_adapter=adapter,
                    max_turns=agent_settings.max_turns,
                    permission_manager=(
                        run.run_permission_manager
                        if run.run_permission_manager is not None
                        else self._permission_manager
                    ),
                    permission_queue=run.permission_queue,
                    hook_manager=(
                        run.run_hook_manager
                        if run.run_hook_manager is not None
                        else self._hook_manager
                    ),
                    agent_mode="agent",
                    api_config=agent_api_config,
                    context_window=resolved_cw,
                    ai_reviewer=(
                        run.run_ai_reviewer
                        if run.run_ai_reviewer is not None
                        else self._ai_reviewer
                    ),
                    tool_call_timeout=settings.tool_call_timeout,
                    auto_compact_enabled=settings.auto_compact_enabled,
                    compact_threshold=settings.max_context_length,
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
                if run.snapshot.error:
                    await self._emit_state(
                        run,
                        "failed",
                        f"Agent failed: {run.snapshot.error}",
                    )
                else:
                    await self._emit_state(run, "completed", "Agent completed")
            except asyncio.CancelledError:
                # ``query_loop`` mutates its projection as it reaches safe
                # boundaries.  Preserve that projection even when the stream
                # is interrupted before a TurnComplete event is emitted.
                self._capture_partial_output(run, params, baseline_message_ids)
                if not run.detached:
                    run.snapshot.error = "stopped"
                run.snapshot.final_result = run.final_text.strip()
                self._persist_transcript(run)
                await self._emit_state(run, "stopped", "Agent stopped")
            except Exception as exc:
                self._capture_partial_output(run, params, baseline_message_ids)
                run.snapshot.error = str(exc)
                run.snapshot.final_result = run.final_text.strip()
                self._persist_transcript(run)
                await self._emit_state(run, "failed", f"Agent failed: {exc}")
                if not run.detached and run.snapshot.session_id == self._session_id:
                    await self._safe_event_sink(
                        ErrorEvent(
                            message=str(exc),
                            recoverable=True,
                            error_type="agent",
                            agent_id=run.snapshot.agent_id,
                        ),
                        run=run,
                    )
            finally:
                run.done_event.set()
                await self._emit_completion(run)

    async def _handle_agent_event(self, run: _AgentRun, event: CoreEvent) -> None:
        # Cancellation and session switches can race with a provider that is
        # slow to observe task cancellation. Never leak stale run events into
        # the replacement session's foreground/background stream.
        if not self._is_current_run(run):
            return
        agent_id = run.snapshot.agent_id
        if isinstance(event, StreamModeEvent):
            if event.mode == "thinking":
                await self._safe_event_sink(
                    AgentOutputEvent(agent_id=agent_id, stream="thinking", text="thinking"),
                    run=run,
                )
            return
        if isinstance(event, StreamTextEvent):
            run.output_chunks.append(event.text)
            run.final_text += event.text
            await self._safe_event_sink(
                AgentOutputEvent(agent_id=agent_id, stream="text", text=event.text),
                run=run,
            )
            return
        if isinstance(event, ToolUseEvent):
            await self._safe_event_sink(
                ToolUseEvent(
                    tool_name=event.tool_name,
                    tool_input=event.tool_input,
                    tool_use_id=event.tool_use_id,
                    agent_id=agent_id,
                ),
                run=run,
            )
            await self._safe_event_sink(
                AgentOutputEvent(
                    agent_id=agent_id,
                    stream="tool_use",
                    text=event.tool_name,
                    tool_name=event.tool_name,
                ),
                run=run,
            )
            return
        if isinstance(event, ToolResultEvent):
            await self._safe_event_sink(
                ToolResultEvent(
                    tool_use_id=event.tool_use_id,
                    tool_name=event.tool_name,
                    result=event.result,
                    is_error=event.is_error,
                    result_for_display=event.result_for_display,
                    tool_input=event.tool_input,
                    agent_id=agent_id,
                    images=event.images,
                ),
                run=run,
            )
            return
        if isinstance(event, PermissionRequestEvent):
            await self._safe_event_sink(
                PermissionRequestEvent(
                    tool_name=event.tool_name,
                    tool_input=event.tool_input,
                    tool_use_id=event.tool_use_id,
                    reason=event.reason,
                    permission_key=event.permission_key,
                    agent_id=agent_id,
                ),
                run=run,
            )
            return
        if isinstance(event, ChoiceRequestEvent):
            await self._safe_event_sink(
                ChoiceRequestEvent(
                    tool_use_id=event.tool_use_id,
                    question=event.question,
                    options=event.options,
                    multiple=event.multiple,
                    agent_id=agent_id,
                ),
                run=run,
            )
            return
        if isinstance(event, ErrorEvent):
            run.snapshot.error = event.message
            await self._safe_event_sink(
                ErrorEvent(
                    message=event.message,
                    recoverable=event.recoverable,
                    error_type=event.error_type,
                    agent_id=agent_id,
                ),
                run=run,
            )
            return
        if isinstance(event, CompactEvent):
            await self._safe_event_sink(
                CompactEvent(
                    summary=event.summary,
                    messages_before=event.messages_before,
                    messages_after=event.messages_after,
                    trigger=event.trigger,
                    agent_id=agent_id,
                ),
                run=run,
            )
            return
        if isinstance(event, TurnCompleteEvent):
            # AgentStateEvent carries sub-agent completion. Forwarding this raw
            # event would be indistinguishable from the parent turn completing.
            return
        await self._safe_event_sink(event, run=run)
