"""TeamManager — core runtime for managing agent teams.

Owns team lifecycle, teammate state machines, task boards, and message routing.
Delegates agent spawn/wait/cancel to the existing AgentManager.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from crabcode_core.logging_utils import get_logger
from crabcode_core.team.message_bus import TeamMessageBus
from crabcode_core.team.models import (
    BridgePolicy,
    CrossTeamMessage,
    TaskItem,
    TaskStatus,
    TeamConfig,
    TeamMessage,
    TeamState,
    TeammateInfo,
    TeammateRole,
    TeammateState,
)

logger = get_logger(__name__)


@dataclass
class TeamRuntime:
    """Runtime state for a single team."""

    team_id: str
    config: TeamConfig
    message_bus: TeamMessageBus
    task_board: list[TaskItem] = field(default_factory=list)
    teammates: dict[str, TeammateInfo] = field(default_factory=dict)
    task_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Serializes membership changes (especially spawn + capacity checks).
    membership_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Spawning an AgentManager run emits lifecycle events that re-enter this
    # manager. Keep the potentially-awaiting spawn outside membership_lock,
    # while this lock still serializes capacity reservations.
    spawn_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending_spawns: int = 0
    # Serializes manager-owned bus operations with unregister/cleanup without
    # blocking lifecycle callbacks that only need membership_lock.
    bus_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    state: TeamState = TeamState.FORMING


class TeamManager:
    """Owns all teams for a CoreSession.

    Uses the existing AgentManager for spawn/wait/cancel operations.
    Manages teammate state machines, message routing, and task boards.
    """

    def __init__(
        self,
        *,
        agent_manager: Any,
        settings: Any,
        event_sink: Callable[[Any], Awaitable[None]],
        cwd: str = ".",
        session_id: str = "",
    ) -> None:
        self._agent_manager = agent_manager
        self._settings = settings
        self._event_sink = event_sink
        self._cwd = cwd
        self._session_id = session_id
        self._teams: dict[str, TeamRuntime] = {}
        # Serializes team creation/removal so a replacement cannot delete the
        # new team's inbox while the old team's cleanup is still running.
        self._teams_lock = asyncio.Lock()
        self._shutting_down: set[str] = set()
        # A manager is reusable while close() is draining cancellation
        # callbacks (some integrations intentionally create a replacement team
        # from such a callback), but becomes terminal once close has completed.
        # This prevents stale ToolContext instances from resurrecting a closed
        # session's team runtime.
        self._closed = False
        # Keep shutdown work independent from the caller's task.  Cancelling a
        # tool invocation must not abandon agent cancellation or inbox cleanup.
        self._shutdown_tasks: dict[str, asyncio.Task[None]] = {}
        self._bridges: dict[tuple[str, str], BridgePolicy] = {}
        self._cross_team_messages: list[CrossTeamMessage] = []
        # Messages received while a teammate is running are held here until
        # its current turn settles.  AgentManager.send_input(..., interrupt=False)
        # intentionally rejects busy runs, so without this buffer messages were
        # persisted but never shown to the model.
        self._pending_messages: dict[str, list[tuple[str, str]]] = {}
        self._delivery_locks: dict[str, asyncio.Lock] = {}
        self._pending_delivery_tasks: dict[str, asyncio.Task[None]] = {}
        self._teammate_watch_tasks: dict[str, asyncio.Task[None]] = {}
        # Preserve recovery details without widening recover()'s historical
        # public return shape ({team_id, agent_id}).  recover_teams() uses this
        # to report whether an interrupted member was busy or cancelling.
        self._last_recovered_states: dict[tuple[str, str], str] = {}

    # ------------------------------------------------------------------
    # Team lifecycle
    # ------------------------------------------------------------------

    async def create_team(
        self,
        name: str,
        *,
        max_teammates: int | None = None,
        config_override: TeamConfig | None = None,
    ) -> str:
        async with self._teams_lock:
            if self._closed:
                raise RuntimeError("Team manager is closed")
            return await self._create_team(
                name,
                max_teammates=max_teammates,
                config_override=config_override,
            )

    async def _create_team(
        self,
        name: str,
        *,
        max_teammates: int | None = None,
        config_override: TeamConfig | None = None,
    ) -> str:
        """Create a new team and return its team_id."""
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Team name must not be empty")
        # Team IDs are used as directory names by the inbox store.  Reject
        # separators here so callers cannot escape the configured inbox root.
        if "\x00" in name or "/" in name or "\\" in name or name in {".", ".."}:
            raise ValueError("Team name cannot contain path separators")
        team_id = name  # Use name as ID for simplicity

        if team_id in self._teams:
            raise ValueError(f"Team '{team_id}' already exists")
        if team_id in self._shutting_down:
            raise ValueError(f"Team '{team_id}' is shutting down")

        team_settings = getattr(self._settings, "team", None)
        if max_teammates is not None and max_teammates <= 0:
            raise ValueError("max_teammates must be greater than zero")
        if config_override is not None:
            # Keep the public `name` argument authoritative while preserving
            # compatibility with callers that reuse a config object whose
            # display name differs.
            config = (
                config_override
                if config_override.name == name
                else config_override.model_copy(update={"name": name})
            )
        else:
            configured_policy = getattr(team_settings, "bridge_policy", BridgePolicy.DENY)
            try:
                configured_policy = BridgePolicy(configured_policy)
            except (TypeError, ValueError):
                logger.warning("Unknown team bridge policy %r; defaulting to deny", configured_policy)
                configured_policy = BridgePolicy.DENY
            config = TeamConfig(
                name=name,
                max_teammates=(
                    max_teammates
                    if max_teammates is not None
                    else (team_settings.max_teammates if team_settings else 8)
                ),
                backpressure_queue_size=team_settings.backpressure_queue_size if team_settings else 100,
                max_message_size_bytes=team_settings.max_message_size_bytes if team_settings else 10_000,
                bridge_policy=configured_policy,
            )

        storage_root = (
            self._scope_inbox_root(Path(config.inbox_dir), custom=True)
            if config.inbox_dir
            else self._resolve_inbox_root()
        )
        inject_fn = self._make_inject_fn()
        wake_fn = self._make_wake_fn()

        bus = TeamMessageBus(
            team_name=team_id,
            config=config,
            inject_fn=inject_fn,
            wake_fn=wake_fn,
            storage_root=storage_root,
        )

        runtime = TeamRuntime(
            team_id=team_id,
            config=config,
            message_bus=bus,
            state=TeamState.ACTIVE,
        )
        self._teams[team_id] = runtime
        return team_id

    async def shutdown_team(self, team_id: str) -> bool:
        """Shutdown a team: cancel all teammates, delete inboxes."""
        # Reserve the name while holding only the map lock.  The expensive
        # cleanup happens in a separate task, so agent cancellation callbacks
        # can safely call back into TeamManager (including create_team).
        async with self._teams_lock:
            runtime = self._teams.get(team_id)
            if runtime is None or team_id in self._shutting_down:
                return False
            self._shutting_down.add(team_id)
            cleanup_task = asyncio.create_task(self._finish_shutdown(team_id, runtime))
            self._shutdown_tasks[team_id] = cleanup_task

        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            # shield() leaves the cleanup task running.  Wait for it before
            # propagating cancellation so callers never observe a half-closed
            # runtime or a prematurely reusable team name.
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                # A second cancellation request should not resurrect the team;
                # the task remains tracked and close() will await it.
                pass
            raise
        return True

    async def _finish_shutdown(self, team_id: str, runtime: TeamRuntime) -> None:
        """Perform the non-cancellable portion of team shutdown."""
        agent_infos: list[tuple[str, TeammateInfo]] = []
        shutdown_events: list[tuple[str, str, str]] = []
        try:
            # Mark the runtime and members before cancellation.  Keep the
            # membership entries until the final phase so send/remove calls
            # fail cleanly and state watchers cannot turn members READY again.
            async with runtime.membership_lock:
                runtime.state = TeamState.SHUTDOWN
                agent_infos = list(runtime.teammates.items())
                for agent_id, info in agent_infos:
                    old_state = info.state.value
                    info.transition_to(TeammateState.SHUTDOWN, force=True)
                    shutdown_events.append((agent_id, old_state, info.role.value))

            # An add_teammate call may have reserved capacity and be waiting
            # for AgentManager.spawn_agent outside membership_lock.  Wait for
            # that transaction to either commit or roll back before deleting
            # the runtime/inboxes, so close() cannot return with an orphan run.
            async with runtime.spawn_lock:
                pass

            # Stop delivery/watch tasks before signalling agent completion.  A
            # waiter can otherwise observe the completion event and start a new
            # send_input run after cancel_agent has returned.
            await asyncio.gather(
                *(self._cancel_agent_tasks(agent_id) for agent_id, _info in agent_infos),
                return_exceptions=True,
            )

            # Agent cancellation can run tool cleanup that calls back into this
            # manager, so it must remain outside membership_lock.
            for agent_id, _info in agent_infos:
                try:
                    await self._agent_manager.cancel_agent(agent_id)
                except Exception:
                    logger.warning(
                        "Failed to cancel agent %s during team shutdown",
                        agent_id,
                        exc_info=True,
                    )

            # Unregister only after manager-owned bus operations have finished.
            # bus_lock is deliberately distinct from membership_lock so a
            # delivery callback can update teammate state while a send is in
            # flight.
            async with runtime.bus_lock:
                # A delivery callback can have queued work after the first
                # cancellation sweep but before the bus operation committed.
                # Drain those tasks once more while the runtime is fenced,
                # before cancelling the underlying agents.
                await asyncio.gather(
                    *(self._cancel_agent_tasks(agent_id) for agent_id, _info in agent_infos),
                    return_exceptions=True,
                )
                async with runtime.membership_lock:
                    for agent_id in list(runtime.teammates):
                        runtime.message_bus.unregister_agent(agent_id)
                    runtime.teammates.clear()

            for agent_id, old_state, role in shutdown_events:
                await self._emit_team_state(
                    team_id,
                    agent_id,
                    old_state,
                    TeammateState.SHUTDOWN.value,
                    role,
                )

            # Delete inbox files while the name remains reserved, preventing a
            # replacement team from sharing this directory during cleanup.
            await runtime.message_bus.delete_team_inboxes()
        finally:
            async with self._teams_lock:
                if self._teams.get(team_id) is runtime:
                    del self._teams[team_id]
                self._shutting_down.discard(team_id)
                current = asyncio.current_task()
                if self._shutdown_tasks.get(team_id) is current:
                    self._shutdown_tasks.pop(team_id, None)

    # ------------------------------------------------------------------
    # Teammate management
    # ------------------------------------------------------------------

    async def add_teammate(
        self,
        team_id: str,
        *,
        role: TeammateRole = TeammateRole.WORKER,
        prompt: str,
        name: str | None = None,
        model_profile: str | None = None,
    ) -> str:
        """Add a teammate to the team. Spawns a sub-agent via AgentManager."""
        runtime = self._teams.get(team_id)
        if runtime is None:
            raise ValueError(f"Team '{team_id}' not found")

        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("Teammate prompt must not be empty")

        # Reserve capacity under the membership lock, then spawn outside it.
        # AgentManager emits queued lifecycle events while spawning; those
        # events re-enter handle_agent_event(), which also needs
        # membership_lock. Holding it across the await would deadlock a real
        # session. spawn_lock serializes reservations so concurrent calls still
        # cannot exceed max_teammates.
        agent_id: str | None = None
        reservation_held = False
        try:
            async with runtime.spawn_lock:
                async with runtime.membership_lock:
                    if runtime.state != TeamState.ACTIVE or team_id in self._shutting_down:
                        raise ValueError(f"Team '{team_id}' is not active")
                    if (
                        len(runtime.teammates) + runtime.pending_spawns
                        >= runtime.config.max_teammates
                    ):
                        raise ValueError(
                            f"Team '{team_id}' is full "
                            f"({len(runtime.teammates) + runtime.pending_spawns}/"
                            f"{runtime.config.max_teammates})"
                        )
                    runtime.pending_spawns += 1
                    reservation_held = True
                    spawn_name = name or f"{role.value}-{len(runtime.teammates)}"

                try:
                    # Spawn outside membership_lock so lifecycle callbacks can
                    # update the team state without waiting on this operation.
                    agent_id = await self._agent_manager.spawn_agent(
                        prompt=prompt,
                        subagent_type="generalPurpose",
                        name=spawn_name,
                        model_profile=model_profile,
                        depth=1,
                    )

                    async with runtime.membership_lock:
                        runtime.pending_spawns -= 1
                        reservation_held = False
                        if (
                            runtime.state != TeamState.ACTIVE
                            or team_id in self._shutting_down
                            or self._teams.get(team_id) is not runtime
                        ):
                            raise ValueError(f"Team '{team_id}' is no longer active")

                        # Register with the message bus and create teammate
                        # info. If either step fails, the spawned run is
                        # cancelled below so it cannot become an orphan.
                        runtime.message_bus.register_agent(agent_id)
                        info = TeammateInfo(
                            agent_id=agent_id,
                            role=role,
                            state=TeammateState.BUSY,
                            model_profile=model_profile,
                            name=name,
                        )
                        runtime.teammates[agent_id] = info
                        self._delivery_locks.setdefault(agent_id, asyncio.Lock())
                except BaseException:
                    if reservation_held:
                        async with runtime.membership_lock:
                            runtime.pending_spawns = max(0, runtime.pending_spawns - 1)
                        reservation_held = False
                    raise

            # Emit state event and arm the watcher inside the same rollback
            # boundary.  Both operations can be cancelled by a caller; a
            # spawned AgentManager run must never be left behind in that case.
            old_state = TeammateState.IDLE.value
            new_state = TeammateState.BUSY.value
            await self._emit_team_state(team_id, agent_id, old_state, new_state, role.value)

            # Keep TeamStatus in sync with the underlying AgentManager run.
            # This watcher is also the hand-off point for queued messages.
            self._ensure_teammate_watcher(team_id, agent_id)

            return agent_id
        except BaseException:
            # Once spawn_agent returned, always attempt rollback.  The
            # cancellation may have landed between spawn and bus registration,
            # leaving no teammate entry to key off.
            if agent_id is not None:
                await self._shielded_rollback_teammate(runtime, team_id, agent_id)
            raise

    async def remove_teammate(self, team_id: str, agent_id: str) -> bool:
        """Remove a teammate from the team."""
        runtime = self._teams.get(team_id)
        if runtime is None:
            return False

        async with runtime.bus_lock:
            async with runtime.membership_lock:
                if runtime.state != TeamState.ACTIVE or team_id in self._shutting_down:
                    return False
                info = runtime.teammates.pop(agent_id, None)
                if info is None:
                    return False

                old_state = info.state.value
                info.transition_to(TeammateState.SHUTDOWN, force=True)
                runtime.message_bus.unregister_agent(agent_id)

        # Stop delivery/state waiters before cancel_agent sets its completion
        # event.  Otherwise a pending waiter can wake and restart this removed
        # agent in the gap before task cleanup.
        await self._cancel_agent_tasks(agent_id)

        # Cancellation can invoke tool cleanup that calls back into this
        # manager, so do it after releasing membership_lock.
        try:
            await self._agent_manager.cancel_agent(agent_id)
        except Exception:
            logger.warning("Failed to cancel agent %s during removal", agent_id, exc_info=True)

        await self._emit_team_state(team_id, agent_id, old_state, TeammateState.SHUTDOWN.value, info.role.value)
        return True

    # ------------------------------------------------------------------
    # Messaging
    # ------------------------------------------------------------------

    async def send_message(
        self,
        team_id: str,
        from_agent: str,
        to_agent: str,
        text: str,
    ) -> TeamMessage | None:
        """Send a message from one teammate to another."""
        runtime = self._teams.get(team_id)
        if runtime is None:
            return None
        if not isinstance(text, str) or not text.strip():
            return None
        # Authorize under membership_lock, but do not hold it across the bus
        # operation. Delivery injection calls back into _mark_teammate_busy,
        # which needs the same lock; holding it here deadlocks idle recipients.
        recipient_epoch: int | None = None
        async with runtime.membership_lock:
            if (
                self._teams.get(team_id) is not runtime
                or runtime.state != TeamState.ACTIVE
                or team_id in self._shutting_down
            ):
                return None
            if from_agent and from_agent not in runtime.teammates:
                logger.warning("Message from non-member %s in team %s", from_agent, team_id)
                return None
            if not to_agent or to_agent not in runtime.teammates:
                return None
            # Capture the registration generation.  If this member is removed
            # and replaced with the same id while the bus lock is contended,
            # the replacement must not inherit this authorization.
            recipient_epoch = runtime.message_bus.registration_epoch(to_agent)
            if recipient_epoch is None:
                return None

        # Keep the recipient registered until this already-authorized send
        # reaches its commit point.  Shutdown acquires the same lock before
        # unregistering agents, so an in-flight persistence/injection cannot be
        # turned into a spurious failure by teardown.
        async with runtime.bus_lock:
            # Removal takes bus_lock before unregistering.  A synchronous
            # re-check here therefore closes the auth-to-commit race without
            # taking membership_lock while bus_lock is held (shutdown uses the
            # opposite nested order in its final cleanup phase).
            if (
                self._teams.get(team_id) is not runtime
                or runtime.state != TeamState.ACTIVE
                or team_id in self._shutting_down
                or to_agent not in runtime.teammates
                or runtime.message_bus.registration_epoch(to_agent) != recipient_epoch
            ):
                return None
            msg = await runtime.message_bus.send(
                from_agent=from_agent,
                to_agent=to_agent,
                text=text,
                expected_epoch=recipient_epoch,
            )

        emit_allowed = False
        if msg:
            async with runtime.membership_lock:
                emit_allowed = not (
                    self._teams.get(team_id) is not runtime
                    or runtime.state != TeamState.ACTIVE
                    or team_id in self._shutting_down
                    or to_agent not in runtime.teammates
                )
            if emit_allowed:
                await self._emit_team_message(team_id, msg)

        return msg

    async def broadcast(
        self,
        team_id: str,
        from_agent: str,
        text: str,
    ) -> list[TeamMessage]:
        """Broadcast a message to all teammates except the sender."""
        runtime = self._teams.get(team_id)
        if runtime is None:
            return []
        if not isinstance(text, str) or not text.strip():
            return []
        recipient_epochs: dict[str, int] = {}
        async with runtime.membership_lock:
            if (
                self._teams.get(team_id) is not runtime
                or runtime.state != TeamState.ACTIVE
                or team_id in self._shutting_down
            ):
                return []
            if from_agent and from_agent not in runtime.teammates:
                logger.warning("Broadcast from non-member %s in team %s", from_agent, team_id)
                return []
            for agent_id in runtime.teammates:
                if agent_id == from_agent:
                    continue
                epoch = runtime.message_bus.registration_epoch(agent_id)
                if epoch is not None:
                    recipient_epochs[agent_id] = epoch

        async with runtime.bus_lock:
            # Freeze the authorized roster and registration generations before
            # writing. Calling the bus broadcast helper directly could include
            # a teammate added after authorization.
            if (
                self._teams.get(team_id) is not runtime
                or runtime.state != TeamState.ACTIVE
                or team_id in self._shutting_down
                or any(
                    agent_id not in runtime.teammates
                    or runtime.message_bus.registration_epoch(agent_id) != epoch
                    for agent_id, epoch in recipient_epochs.items()
                )
            ):
                return []
            messages: list[TeamMessage] = []
            for agent_id, epoch in recipient_epochs.items():
                message = await runtime.message_bus.send(
                    from_agent=from_agent,
                    to_agent=agent_id,
                    text=text,
                    expected_epoch=epoch,
                )
                if message is not None:
                    messages.append(message)

        emit_allowed = True
        async with runtime.membership_lock:
            if (
                self._teams.get(team_id) is not runtime
                or runtime.state != TeamState.ACTIVE
                or team_id in self._shutting_down
            ):
                emit_allowed = False

        for msg in messages:
            if emit_allowed:
                await self._emit_team_message(team_id, msg)

        return messages

    def get_unread_messages(self, team_id: str, agent_id: str) -> list[TeamMessage]:
        """Get unread messages for a teammate."""
        runtime = self._teams.get(team_id)
        if runtime is None:
            return []
        return runtime.message_bus.get_unread(agent_id)

    def get_all_messages(self, team_id: str, agent_id: str) -> list[TeamMessage]:
        """Get all messages for a teammate."""
        runtime = self._teams.get(team_id)
        if runtime is None:
            return []
        return runtime.message_bus.get_all(agent_id)

    async def mark_read(self, team_id: str, agent_id: str, message_ids: list[str] | None = None) -> int:
        """Mark messages as read for a teammate."""
        runtime = self._teams.get(team_id)
        if runtime is None:
            return 0
        # Authorize first, then release the lock while mark_read rewrites the
        # inbox and sends receipts. Receipt injection can update teammate state
        # and therefore re-enter membership_lock.
        async with runtime.membership_lock:
            if (
                self._teams.get(team_id) is not runtime
                or runtime.state != TeamState.ACTIVE
                or team_id in self._shutting_down
                or agent_id not in runtime.teammates
            ):
                return 0
        async with runtime.bus_lock:
            count = await runtime.message_bus.mark_read(agent_id, message_ids)
        async with runtime.membership_lock:
            if (
                self._teams.get(team_id) is not runtime
                or runtime.state != TeamState.ACTIVE
                or team_id in self._shutting_down
                or agent_id not in runtime.teammates
            ):
                return 0
        return count

    # ------------------------------------------------------------------
    # Task board
    # ------------------------------------------------------------------

    async def add_task(self, team_id: str, description: str) -> str:
        """Add a task to the team's task board."""
        runtime = self._teams.get(team_id)
        if runtime is None:
            raise ValueError(f"Team '{team_id}' not found")
        if not isinstance(description, str) or not description.strip():
            raise ValueError("Task description must not be empty")

        task = TaskItem(description=description)
        async with runtime.membership_lock:
            if runtime.state != TeamState.ACTIVE or team_id in self._shutting_down:
                raise ValueError(f"Team '{team_id}' is not active")
            async with runtime.task_lock:
                runtime.task_board.append(task)

        await self._emit_task_update(team_id, task)
        return task.id

    async def claim_task(self, team_id: str, task_id: str, agent_id: str) -> bool:
        """Atomically claim a task. Protected by asyncio.Lock."""
        runtime = self._teams.get(team_id)
        if runtime is None:
            return False
        async with runtime.membership_lock:
            if runtime.state != TeamState.ACTIVE or team_id in self._shutting_down:
                return False
            if agent_id and agent_id not in runtime.teammates:
                return False
            async with runtime.task_lock:
                task = next((t for t in runtime.task_board if t.id == task_id), None)
                if task is None:
                    return False
                if not task.claim(agent_id):
                    return False

        await self._emit_task_update(team_id, task)
        return True

    async def complete_task(
        self,
        team_id: str,
        task_id: str,
        result: str = "",
        agent_id: str | None = None,
    ) -> bool:
        """Mark a task as completed, optionally enforcing its assignee."""
        runtime = self._teams.get(team_id)
        if runtime is None:
            return False

        async with runtime.membership_lock:
            if runtime.state != TeamState.ACTIVE or team_id in self._shutting_down:
                return False
            if agent_id and agent_id not in runtime.teammates:
                return False
            async with runtime.task_lock:
                task = next((t for t in runtime.task_board if t.id == task_id), None)
                if task is None:
                    return False
                if agent_id and task.assignee != agent_id:
                    return False
                if not task.complete(result):
                    return False

        await self._emit_task_update(team_id, task)
        return True

    async def fail_task(
        self,
        team_id: str,
        task_id: str,
        reason: str = "",
        agent_id: str | None = None,
    ) -> bool:
        """Mark a task as failed, optionally enforcing its assignee."""
        runtime = self._teams.get(team_id)
        if runtime is None:
            return False

        async with runtime.membership_lock:
            if runtime.state != TeamState.ACTIVE or team_id in self._shutting_down:
                return False
            if agent_id and agent_id not in runtime.teammates:
                return False
            async with runtime.task_lock:
                task = next((t for t in runtime.task_board if t.id == task_id), None)
                if task is None:
                    return False
                if agent_id and task.assignee != agent_id:
                    return False
                if not task.fail(reason):
                    return False

        await self._emit_task_update(team_id, task)
        return True

    def list_tasks(self, team_id: str) -> list[TaskItem]:
        """List all tasks on the team's task board."""
        runtime = self._teams.get(team_id)
        if runtime is None:
            return []
        return [task.model_copy(deep=True) for task in runtime.task_board]

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_team_status(self, team_id: str) -> dict[str, Any]:
        """Get a status overview of the team."""
        runtime = self._teams.get(team_id)
        if runtime is None:
            return {}

        teammates = []
        for agent_id, info in runtime.teammates.items():
            teammates.append({
                "agent_id": agent_id,
                "name": info.name,
                "role": info.role.value,
                "state": info.state.value,
                "model_profile": info.model_profile,
            })

        tasks_summary = {
            "total": len(runtime.task_board),
            "pending": sum(1 for t in runtime.task_board if t.status == TaskStatus.PENDING),
            "claimed": sum(1 for t in runtime.task_board if t.status == TaskStatus.CLAIMED),
            "completed": sum(1 for t in runtime.task_board if t.status == TaskStatus.COMPLETED),
            "failed": sum(1 for t in runtime.task_board if t.status == TaskStatus.FAILED),
        }

        return {
            "team_id": team_id,
            "state": runtime.state.value,
            "teammates": teammates,
            "teammate_count": len(runtime.teammates),
            "max_teammates": runtime.config.max_teammates,
            "tasks": tasks_summary,
        }

    def list_teams(self) -> list[str]:
        """List all active team IDs."""
        return list(self._teams.keys())

    def get_teammate(self, team_id: str, agent_id: str) -> TeammateInfo | None:
        """Get info about a specific teammate."""
        runtime = self._teams.get(team_id)
        if runtime is None:
            return None
        info = runtime.teammates.get(agent_id)
        return info.model_copy(deep=True) if info is not None else None

    def get_team_for_agent(self, agent_id: str) -> str | None:
        """Find which team an agent belongs to."""
        for team_id, runtime in self._teams.items():
            if agent_id in runtime.teammates:
                return team_id
        return None

    def get_lead_agent(self, team_id: str) -> str | None:
        """Return an explicitly registered lead teammate, if one exists."""
        runtime = self._teams.get(team_id)
        if runtime is None:
            return None
        for agent_id, info in runtime.teammates.items():
            if info.role == TeammateRole.LEAD:
                return agent_id
        return None

    # ------------------------------------------------------------------
    # Cross-team communication
    # ------------------------------------------------------------------

    def register_bridge(
        self,
        team_a: str,
        team_b: str,
        policy: BridgePolicy = BridgePolicy.ALLOW_TAGGED,
    ) -> None:
        """Register a bridge policy between two teams."""
        if team_a == team_b:
            raise ValueError("A bridge requires two distinct teams")
        try:
            policy = BridgePolicy(policy)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Unknown bridge policy: {policy!r}") from exc
        self._bridges[(team_a, team_b)] = policy
        self._bridges[(team_b, team_a)] = policy

    def get_bridge_policy(self, team_a: str, team_b: str) -> BridgePolicy:
        """Return the effective bridge policy, defaulting to deny."""
        explicit = self._bridges.get((team_a, team_b))
        if explicit is not None:
            return explicit
        source = self._teams.get(team_a)
        target = self._teams.get(team_b)
        if source is not None and target is not None:
            source_policy = source.config.bridge_policy
            target_policy = target.config.bridge_policy
            if source_policy == target_policy:
                return source_policy
        return BridgePolicy.DENY

    async def send_cross_team(
        self,
        from_team: str,
        from_agent: str,
        to_team: str,
        to_agent: str,
        text: str,
    ) -> CrossTeamMessage | None:
        """Send a message between teams. Requires an allowed bridge policy."""
        if from_team == to_team:
            logger.warning("Cross-team message requires distinct teams: %s", from_team)
            return None
        source_runtime = self._teams.get(from_team)
        target_runtime = self._teams.get(to_team)
        if source_runtime is None or target_runtime is None:
            logger.warning("Cross-team message references an unknown team: %s -> %s", from_team, to_team)
            return None
        if not isinstance(text, str) or not text.strip():
            return None

        # Acquire both team locks in stable order for authorization only.  The
        # actual bus operation must run outside them: target injection calls
        # back into _mark_teammate_busy(), which needs the target lock.
        ordered = sorted(
            ((from_team, source_runtime), (to_team, target_runtime)),
            key=lambda item: item[0],
        )
        target_epochs: dict[str, int] = {}
        async with ordered[0][1].membership_lock:
            async with ordered[1][1].membership_lock:
                if (
                    self._teams.get(from_team) is not source_runtime
                    or self._teams.get(to_team) is not target_runtime
                    or source_runtime.state != TeamState.ACTIVE
                    or target_runtime.state != TeamState.ACTIVE
                    or from_team in self._shutting_down
                    or to_team in self._shutting_down
                ):
                    return None
                if from_agent and from_agent not in source_runtime.teammates:
                    logger.warning(
                        "Cross-team message from non-member %s in team %s",
                        from_agent,
                        from_team,
                    )
                    return None
                if to_agent and to_agent not in target_runtime.teammates:
                    logger.warning(
                        "Cross-team message targets non-member %s in team %s",
                        to_agent,
                        to_team,
                    )
                    return None
                if to_agent:
                    epoch = target_runtime.message_bus.registration_epoch(to_agent)
                    if epoch is None:
                        return None
                    target_epochs[to_agent] = epoch
                else:
                    for target_id in target_runtime.teammates:
                        if target_id == from_agent:
                            continue
                        epoch = target_runtime.message_bus.registration_epoch(target_id)
                        if epoch is not None:
                            target_epochs[target_id] = epoch

                policy = self._bridges.get((from_team, to_team))
                if policy is None:
                    # A non-deny team-level policy can opt into a bridge without
                    # explicit pair registration.  Require both sides to opt in.
                    source_policy = source_runtime.config.bridge_policy
                    target_policy = target_runtime.config.bridge_policy
                    policy = (
                        source_policy
                        if source_policy == target_policy
                        else BridgePolicy.DENY
                    )
                if policy == BridgePolicy.DENY:
                    logger.warning(
                        "Cross-team message blocked: no bridge between %s and %s",
                        from_team,
                        to_team,
                    )
                    return None

                # Apply the target team's byte limit before recording the audit
                # message so retained and delivered payloads agree.
                max_bytes = target_runtime.config.max_message_size_bytes
                tag_prefix = (
                    f"[cross-team:{from_team}] "
                    if policy == BridgePolicy.ALLOW_TAGGED
                    else ""
                )
                available_bytes = max(0, max_bytes - len(tag_prefix.encode("utf-8")))
                if len(text.encode("utf-8")) > available_bytes:
                    text = (
                        text.encode("utf-8")[:available_bytes]
                        .decode("utf-8", errors="ignore")
                    )
                if not text:
                    return None

                cross_msg = CrossTeamMessage(
                    from_team=from_team,
                    from_agent=from_agent,
                    to_team=to_team,
                    to_agent=to_agent,
                    text=text,
                    bridge_policy=policy,
                )
                # ALLOW_TAGGED makes the source explicit to the recipient;
                # ALLOW_ALL preserves the original payload.
                tagged_text = tag_prefix + text

        # Serialize bus I/O with both teams' teardown paths.  Acquire in the
        # same stable team-id order used for membership authorization so two
        # simultaneous cross-team sends cannot deadlock each other.
        async with ordered[0][1].bus_lock:
            async with ordered[1][1].bus_lock:
                # Removal acquires bus_lock before unregistering.  Recheck the
                # frozen roster/generations synchronously while both locks are
                # held, then use expected_epoch on every write.
                if (
                    self._teams.get(from_team) is not source_runtime
                    or self._teams.get(to_team) is not target_runtime
                    or source_runtime.state != TeamState.ACTIVE
                    or target_runtime.state != TeamState.ACTIVE
                    or from_team in self._shutting_down
                    or to_team in self._shutting_down
                    or any(
                        target_id not in target_runtime.teammates
                        or target_runtime.message_bus.registration_epoch(target_id) != epoch
                        for target_id, epoch in target_epochs.items()
                    )
                ):
                    return None
                delivered_messages = []
                for target_id, epoch in target_epochs.items():
                    delivered = await target_runtime.message_bus.send(
                        from_agent=from_agent,
                        to_agent=target_id,
                        text=tagged_text,
                        msg_type="cross_team",
                        expected_epoch=epoch,
                    )
                    if delivered is not None:
                        delivered_messages.append(delivered)
                        if len(delivered_messages) == 1:
                            # The durable inbox append is the delivery commit
                            # point.  Record the audit exactly once while the bus
                            # transaction is still owned so teardown cannot make
                            # an already-delivered message look unsuccessful.
                            self._cross_team_messages.append(cross_msg)
                if not delivered_messages:
                    return None

        for delivered in delivered_messages:
            await self._emit_team_message(to_team, delivered)
        return cross_msg

    # ------------------------------------------------------------------
    # Recovery
    # ------------------------------------------------------------------

    async def recover(self) -> list[dict[str, str]]:
        """Recover stale teammate state after an interrupted runtime.

        Both ``busy`` and ``cancelling`` are in-flight states.  Either can be
        left behind when a process exits before the AgentManager writes its
        terminal state, so both are normalized to ``ready``.  This method does
        NOT auto-restart agents and returns the affected team/agent pairs for
        manual re-engagement.
        """
        interrupted: list[dict[str, str]] = []
        recovered_states: dict[tuple[str, str], str] = {}

        # Take a stable runtime snapshot under the map lock.  A concurrent
        # shutdown may remove and later recreate the same team id; identity
        # checks below prevent recovery from mutating or reporting the
        # replacement runtime.
        async with self._teams_lock:
            runtimes = list(self._teams.items())

        for team_id, runtime in runtimes:
            changes: list[tuple[str, str, str]] = []
            # A shutdown reserves the team name before waiting on its
            # membership lock.  Check that inexpensive fence first so recovery
            # does not block behind a lock that shutdown intentionally holds
            # across cancellation callbacks.
            async with self._teams_lock:
                if (
                    self._teams.get(team_id) is not runtime
                    or team_id in self._shutting_down
                ):
                    continue
            # Acquire the per-runtime lock first.  add_teammate() and the
            # shutdown worker can hold membership_lock across an AgentManager
            # callback that re-enters create_team() (and therefore needs
            # _teams_lock); taking the locks in the opposite order here would
            # deadlock that callback.  The map lock is only held for the
            # synchronous identity/state check and transition.
            async with runtime.membership_lock:
                async with self._teams_lock:
                    if (
                        runtime.state != TeamState.ACTIVE
                        or team_id in self._shutting_down
                        or self._teams.get(team_id) is not runtime
                    ):
                        continue
                    for agent_id, info in list(runtime.teammates.items()):
                        if info.state in {
                            TeammateState.BUSY,
                            TeammateState.CANCELLING,
                        }:
                            old_state = info.state.value
                            info.transition_to(TeammateState.READY, force=True)
                            changes.append((agent_id, old_state, info.role.value))
                            interrupted.append({"team_id": team_id, "agent_id": agent_id})
                            recovered_states[(team_id, agent_id)] = old_state
            for agent_id, old_state, role in changes:
                # Shutdown/removal may begin after the recovery transition but
                # before its notification is dispatched.  Re-check ownership
                # synchronously so a READY event cannot be emitted for a team
                # that has already entered its terminal state.
                async with runtime.membership_lock:
                    info = runtime.teammates.get(agent_id)
                    if (
                        self._teams.get(team_id) is not runtime
                        or runtime.state != TeamState.ACTIVE
                        or team_id in self._shutting_down
                        or info is None
                        or info.state != TeammateState.READY
                    ):
                        continue
                await self._emit_team_state(
                    team_id,
                    agent_id,
                    old_state,
                    TeammateState.READY.value,
                    role,
                )

        if recovered_states:
            self._last_recovered_states = recovered_states
        return interrupted

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Shutdown all teams."""
        async with self._teams_lock:
            if self._closed:
                return
        # Cancellation callbacks can re-enter the manager and create another
        # team.  Drain until no active team or detached cleanup remains instead
        # of relying on one snapshot of _teams.
        current = asyncio.current_task()
        while True:
            for team_id in list(self._teams.keys()):
                if team_id in self._shutting_down:
                    continue
                await self.shutdown_team(team_id)

            # A caller may have cancelled shutdown_team after its cleanup task
            # was scheduled.  Do not return while that detached cleanup is
            # still mutating inboxes or cancelling agents.  Exclude the current
            # cleanup task for re-entrant close() calls from cancellation hooks.
            pending_shutdowns = [
                task
                for task in self._shutdown_tasks.values()
                if task is not current and not task.done()
            ]
            if pending_shutdowns:
                await asyncio.gather(*pending_shutdowns, return_exceptions=True)

            # Also clean up orphaned waiters for agents removed by an external
            # AgentManager operation.  This await is intentionally inside the
            # drain loop: a cancellation callback can create a team while the
            # orphan tasks are being cancelled, and that team must be drained
            # before the manager becomes terminal.
            await asyncio.gather(
                *(self._cancel_agent_tasks(agent_id) for agent_id in list(self._delivery_locks)),
                return_exceptions=True,
            )

            async with self._teams_lock:
                remaining = [
                    task
                    for task in self._shutdown_tasks.values()
                    if task is not current and not task.done()
                ]
                active_teams = any(
                    team_id not in self._shutting_down for team_id in self._teams
                )
                if not active_teams and not remaining:
                    # The lock closes the small race between the final drain
                    # check and a stale ToolContext attempting create_team().
                    # A creator that acquired the lock first made the team
                    # visible above and forces another drain iteration; one
                    # that arrives afterwards observes _closed and is rejected.
                    self._closed = True
                    return

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_inbox_root(self) -> Path:
        """Resolve the inbox storage root directory."""
        team_settings = getattr(self._settings, "team", None)
        if team_settings and team_settings.inbox_dir:
            # A configured directory is a parent, rather than a shared inbox:
            # session-bound managers get stable project/session children so an
            # old session cannot read or delete a same-named team's files.
            return self._scope_inbox_root(Path(team_settings.inbox_dir), custom=True)
        # Default: ~/.crabcode/team_inbox/<project_hash>/<session_hash>/.
        # Keep the project-only path for low-level callers that have not bound a
        # session yet.
        return self._scope_inbox_root(
            Path.home() / ".crabcode" / "team_inbox",
            custom=False,
        )

    def _scope_inbox_root(self, base: Path, *, custom: bool) -> Path:
        """Return a session-isolated inbox root under *base*.

        ``custom`` roots retain their historical unscoped location until a
        session id is available.  This keeps low-level TeamManager callers
        compatible while ensuring every CoreSession-backed manager is isolated.
        """
        project_hash = hashlib.md5(self._cwd.encode()).hexdigest()[:12]
        if not self._session_id:
            return base if custom else base / project_hash
        session_hash = hashlib.md5(self._session_id.encode()).hexdigest()[:12]
        return base / project_hash / session_hash

    def _make_inject_fn(self) -> Callable:
        """Create a message injection function for the bus.

        Injects a message into the recipient's session as a synthetic
        user message so the LLM actually sees it.
        """
        async def _inject(agent_id: str, from_agent: str, text: str) -> None:
            await self._deliver_or_queue(agent_id, from_agent, text)
        return _inject

    async def _deliver_or_queue(self, agent_id: str, from_agent: str, text: str) -> None:
        """Inject immediately when idle, otherwise retain ordered pending input."""
        lock = self._delivery_locks.setdefault(agent_id, asyncio.Lock())
        async with lock:
            # A pending batch must be delivered before a new direct message;
            # otherwise a message arriving just after the previous turn ends
            # could overtake older queued messages.
            team_id = self.get_team_for_agent(agent_id)
            runtime = self._teams.get(team_id) if team_id is not None else None
            if (
                runtime is None
                or self._teams.get(team_id) is not runtime
                or runtime.state != TeamState.ACTIVE
                or team_id in self._shutting_down
            ):
                # A bus write may finish after teardown has started.  It is
                # durable, but it must not start/restart work in a terminal
                # teammate runtime.
                self._pending_messages.pop(agent_id, None)
                return
            if self._pending_messages.get(agent_id):
                self._pending_messages.setdefault(agent_id, []).append((from_agent, text))
                self._ensure_pending_delivery(agent_id)
                return

            prompt = f"[Message from teammate {from_agent}]: {text}"
            delivered = False
            try:
                delivered = bool(
                    await self._agent_manager.send_input(
                        agent_id,
                        prompt,
                        interrupt=False,
                    )
                )
            except Exception:
                logger.debug("Failed to inject message to agent %s", agent_id, exc_info=True)

            if delivered:
                await self._mark_teammate_busy(agent_id)
                self._ensure_teammate_watcher_for_agent(agent_id)
                return

            # Busy (or a transient race with completion): wait for the current
            # run and then deliver all buffered messages in one ordered prompt.
            self._pending_messages.setdefault(agent_id, []).append((from_agent, text))
            self._ensure_pending_delivery(agent_id)

    def _ensure_pending_delivery(self, agent_id: str) -> None:
        task = self._pending_delivery_tasks.get(agent_id)
        if task is not None and not task.done():
            return
        self._pending_delivery_tasks[agent_id] = asyncio.create_task(
            self._wait_and_deliver_pending(agent_id)
        )

    async def _wait_and_deliver_pending(self, agent_id: str) -> None:
        """Wait for a busy run and drain pending messages without reordering."""
        failed_attempts = 0
        try:
            while self._pending_messages.get(agent_id):
                team_id = self.get_team_for_agent(agent_id)
                runtime = self._teams.get(team_id) if team_id is not None else None
                if (
                    runtime is None
                    or self._teams.get(team_id) is not runtime
                    or runtime.state != TeamState.ACTIVE
                    or team_id in self._shutting_down
                ):
                    self._pending_messages.pop(agent_id, None)
                    return
                wait_agent = getattr(self._agent_manager, "wait_agent", None)
                if wait_agent is None:
                    logger.warning("Agent manager cannot wait for pending teammate %s", agent_id)
                    return
                snapshot = await wait_agent(agent_id)
                if snapshot is None:
                    # The recipient may have been removed by a team/session
                    # shutdown while this waiter was asleep.  Do not leave a
                    # permanent in-memory retry task (or repeatedly requeue
                    # the same batch) when AgentManager can no longer observe
                    # that run.  The message bus remains the durable source
                    # of truth and will replay unread messages if the agent is
                    # registered again.
                    self._pending_messages.pop(agent_id, None)
                    return

                # If a new run is already active, wait for it rather than
                # calling send_input and dropping the batch again.
                is_active = getattr(self._agent_manager, "is_agent_active", None)
                if callable(is_active) and is_active(agent_id):
                    await asyncio.sleep(0.05)
                    continue

                lock = self._delivery_locks.setdefault(agent_id, asyncio.Lock())
                async with lock:
                    pending = list(self._pending_messages.get(agent_id, []))
                    if not pending:
                        return
                    # Do not spin forever when callback-enabled terminal runs
                    # intentionally reject follow-up input.
                    if (
                        getattr(snapshot, "callback_enabled", False)
                        and getattr(snapshot, "callback_state", "") in {"pending", "injected"}
                    ):
                        # The completion dispatcher still owns the terminal
                        # callback.  Keep messages that arrived while this
                        # teammate was busy instead of silently dropping the
                        # in-memory delivery record.  The durable inbox remains
                        # the source of truth; a later explicit re-engagement
                        # or lifecycle event will retry this batch.
                        logger.info(
                            "Deferring pending messages until callback delivery for teammate %s",
                            agent_id,
                        )
                        return
                    combined = "\n".join(
                        f"[Message from teammate {sender}]: {message}"
                        for sender, message in pending
                    )
                    try:
                        delivered = bool(
                            await self._agent_manager.send_input(
                                agent_id,
                                combined,
                                interrupt=False,
                            )
                        )
                    except Exception:
                        delivered = False
                        logger.debug(
                            "Failed to drain pending messages for agent %s",
                            agent_id,
                            exc_info=True,
                        )
                    if delivered:
                        # New arrivals block on this lock, so clearing the
                        # exact batch cannot lose or reorder a message.
                        self._pending_messages.pop(agent_id, None)
                        await self._mark_teammate_busy(agent_id)
                        self._ensure_teammate_watcher_for_agent(agent_id)
                        return
                    failed_attempts += 1
                # A terminal AgentManager implementation can reject input
                # forever.  Bound retries so a broken recipient does not leave
                # a hot background task; the durable inbox remains available
                # for a later explicit retry.
                if failed_attempts >= 5:
                    logger.warning("Giving up temporary delivery retries for teammate %s", agent_id)
                    return
                await asyncio.sleep(min(0.05 * (2 ** (failed_attempts - 1)), 1.0))
        except asyncio.CancelledError:
            raise
        finally:
            current = asyncio.current_task()
            if self._pending_delivery_tasks.get(agent_id) is current:
                self._pending_delivery_tasks.pop(agent_id, None)

    def _ensure_teammate_watcher(self, team_id: str, agent_id: str) -> None:
        runtime = self._teams.get(team_id)
        if (
            runtime is None
            or runtime.state != TeamState.ACTIVE
            or team_id in self._shutting_down
            or agent_id not in runtime.teammates
        ):
            return
        task = self._teammate_watch_tasks.get(agent_id)
        if task is not None and not task.done():
            return
        self._teammate_watch_tasks[agent_id] = asyncio.create_task(
            self._watch_teammate(team_id, agent_id)
        )

    def _ensure_teammate_watcher_for_agent(self, agent_id: str) -> None:
        team_id = self.get_team_for_agent(agent_id)
        if team_id is not None:
            self._ensure_teammate_watcher(team_id, agent_id)

    async def _watch_teammate(self, team_id: str, agent_id: str) -> None:
        """Mirror AgentManager terminal states into TeamStatus."""
        try:
            wait_agent = getattr(self._agent_manager, "wait_agent", None)
            if wait_agent is None:
                return
            while True:
                snapshot = await wait_agent(agent_id)
                if snapshot is None:
                    return
                # A new send_input can start a replacement run immediately
                # after done_event is set but before this watcher resumes.  Do
                # not mark that replacement READY; wait for its own terminal
                # state first.
                is_active = getattr(self._agent_manager, "is_agent_active", None)
                if not callable(is_active) or not is_active(agent_id):
                    break
                await asyncio.sleep(0.05)
            runtime = self._teams.get(team_id)
            if runtime is None:
                return
            state_event: tuple[str, str] | None = None
            should_rearm = False
            # A teammate can be removed or a team can enter shutdown after
            # wait_agent() wakes but before this watcher updates the model.
            # Serialize the final transition with membership changes so a
            # stale completion cannot publish READY for a removed member.
            async with runtime.membership_lock:
                if (
                    self._teams.get(team_id) is not runtime
                    or runtime.state != TeamState.ACTIVE
                    or team_id in self._shutting_down
                ):
                    return
                info = runtime.teammates.get(agent_id)
                if info is None:
                    return
                if info.state == TeammateState.BUSY:
                    old_state = info.state.value
                    if info.transition_to(TeammateState.READY, force=True):
                        state_event = (old_state, info.role.value)
                # A pending delivery may have started another run while this
                # watcher was settling. Ensure a fresh watcher observes it.
                should_rearm = bool(
                    getattr(self._agent_manager, "is_agent_active", lambda _id: False)(agent_id)
                )
            if state_event is not None:
                await self._emit_team_state(
                    team_id,
                    agent_id,
                    state_event[0],
                    TeammateState.READY.value,
                    state_event[1],
                )
            if should_rearm:
                self._ensure_teammate_watcher(team_id, agent_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("Failed to update teammate state for %s", agent_id, exc_info=True)
        finally:
            current = asyncio.current_task()
            if self._teammate_watch_tasks.get(agent_id) is current:
                self._teammate_watch_tasks.pop(agent_id, None)
            # A replacement run may have started in the tiny window between
            # the terminal snapshot and watcher cleanup.  Re-arm a watcher so
            # TeamStatus cannot remain READY while that run is active.
            is_active = getattr(self._agent_manager, "is_agent_active", None)
            runtime = self._teams.get(team_id)
            info = runtime.teammates.get(agent_id) if runtime else None
            if (
                callable(is_active)
                and is_active(agent_id)
                and runtime is not None
                and runtime.state == TeamState.ACTIVE
                and team_id not in self._shutting_down
                and info is not None
                and info.state != TeammateState.SHUTDOWN
            ):
                self._ensure_teammate_watcher_for_agent(agent_id)

    async def _mark_teammate_busy(self, agent_id: str) -> None:
        team_id = self.get_team_for_agent(agent_id)
        runtime = self._teams.get(team_id) if team_id else None
        if runtime is None or team_id is None:
            return
        state_event: tuple[str, str] | None = None
        async with runtime.membership_lock:
            if (
                self._teams.get(team_id) is not runtime
                or runtime.state != TeamState.ACTIVE
                or team_id in self._shutting_down
            ):
                return
            info = runtime.teammates.get(agent_id)
            if info is None or info.state == TeammateState.SHUTDOWN:
                return
            old_state = info.state.value
            if info.state != TeammateState.BUSY and info.transition_to(TeammateState.BUSY, force=True):
                state_event = (old_state, info.role.value)
        if state_event is not None:
            await self._emit_team_state(
                team_id,
                agent_id,
                state_event[0],
                TeammateState.BUSY.value,
                state_event[1],
            )

    async def _cancel_agent_tasks(self, agent_id: str) -> None:
        """Cancel pending delivery/state waiters and discard buffered input."""
        self._pending_messages.pop(agent_id, None)
        tasks = []
        for task_map in (self._pending_delivery_tasks, self._teammate_watch_tasks):
            task = task_map.pop(agent_id, None)
            if task is not None and task is not asyncio.current_task() and not task.done():
                task.cancel()
                tasks.append(task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._delivery_locks.pop(agent_id, None)

    async def _rollback_teammate(
        self,
        runtime: TeamRuntime,
        team_id: str,
        agent_id: str,
    ) -> None:
        """Undo a partially-created teammate and stop its AgentManager run."""
        # Remove membership first under the same lock used by send/remove/
        # shutdown.  This fences any late delivery while cancellation runs.
        async with runtime.bus_lock:
            async with runtime.membership_lock:
                info = runtime.teammates.pop(agent_id, None)
                if info is not None:
                    info.transition_to(TeammateState.SHUTDOWN, force=True)
                runtime.message_bus.unregister_agent(agent_id)

        await self._cancel_agent_tasks(agent_id)
        try:
            await self._agent_manager.cancel_agent(agent_id)
        except Exception:
            logger.warning(
                "Failed to clean up spawned teammate %s in team %s",
                agent_id,
                team_id,
                exc_info=True,
            )

    async def _shielded_rollback_teammate(
        self,
        runtime: TeamRuntime,
        team_id: str,
        agent_id: str,
    ) -> None:
        """Run teammate rollback to completion despite caller cancellation."""
        cleanup_task = asyncio.create_task(
            self._rollback_teammate(runtime, team_id, agent_id)
        )
        cancelled = False
        # ``shield`` protects the child, but every cancellation request still
        # interrupts the parent await.  Keep joining until the child settles;
        # otherwise a second cancellation can make the rollback task
        # untracked, leaving an AgentManager run and inbox registration behind.
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                cancelled = True
                continue

        # Retrieve and propagate a cleanup failure (including cancellation of
        # the child itself).  Checking done() above avoids an infinite loop if
        # some external owner explicitly cancels the cleanup task.
        cleanup_task.result()
        if cancelled:
            raise asyncio.CancelledError

    async def handle_agent_event(self, event: Any) -> None:
        """Consume an AgentStateEvent when CoreSession wires one in.

        Kept as a public, optional hook so lifecycle integration can be added
        without coupling TeamManager to CoreSession's event dispatcher.
        """
        # TeamStateEvent/AgentOutputEvent also carry an agent_id but are not
        # lifecycle notifications.  Restrict this hook to AgentStateEvent to
        # avoid flipping a teammate back to READY on its own team event.
        from crabcode_core.types.event import AgentStateEvent
        if not isinstance(event, AgentStateEvent):
            return
        agent_id = getattr(event, "agent_id", None)
        if not agent_id:
            return
        team_id = self.get_team_for_agent(agent_id)
        runtime = self._teams.get(team_id) if team_id else None
        if runtime is None or team_id is None:
            return
        status = getattr(event, "status", "")
        target = TeammateState.BUSY if status in {"queued", "running"} else TeammateState.READY
        state_event: tuple[str, str] | None = None
        async with runtime.membership_lock:
            if (
                self._teams.get(team_id) is not runtime
                or runtime.state != TeamState.ACTIVE
                or team_id in self._shutting_down
            ):
                return
            info = runtime.teammates.get(agent_id)
            if info is None or info.state == TeammateState.SHUTDOWN:
                return
            old_state = info.state.value
            if info.state != target and info.transition_to(target, force=True):
                state_event = (old_state, info.role.value)
        if state_event is not None:
            await self._emit_team_state(
                team_id,
                agent_id,
                state_event[0],
                target.value,
                state_event[1],
            )
        if target == TeammateState.BUSY:
            self._ensure_teammate_watcher(team_id, agent_id)
        elif self._pending_messages.get(agent_id):
            self._ensure_pending_delivery(agent_id)

    def _make_wake_fn(self) -> Callable:
        """Create an auto-wake function for the bus.

        If the recipient is idle (done_event set), re-engages it by
        sending a wake message via send_input.
        """
        async def _wake(agent_id: str, from_agent: str) -> None:
            # The inject_fn already handles re-engagement by calling send_input,
            # which will restart the prompt loop if the agent is idle.
            # This function exists as a separate hook for future use (e.g.,
            # starting a fresh prompt loop without injecting a message).
            pass
        return _wake

    async def _emit_team_message(self, team_id: str, msg: TeamMessage) -> None:
        from crabcode_core.types.event import TeamMessageEvent
        try:
            await self._event_sink(TeamMessageEvent(
                team_id=team_id,
                from_agent=msg.from_agent,
                to_agent=msg.to_agent,
                text=msg.text,
                msg_type=msg.msg_type,
                message_id=msg.id,
            ))
        except Exception:
            logger.warning("Failed to emit team message event for %s", msg.id, exc_info=True)

    async def _emit_team_state(
        self, team_id: str, agent_id: str, old_state: str, new_state: str, role: str
    ) -> None:
        from crabcode_core.types.event import TeamStateEvent
        try:
            await self._event_sink(TeamStateEvent(
                team_id=team_id,
                agent_id=agent_id,
                old_state=old_state,
                new_state=new_state,
                role=role,
            ))
        except Exception:
            logger.warning("Failed to emit team state event for %s", agent_id, exc_info=True)

    async def _emit_task_update(self, team_id: str, task: TaskItem) -> None:
        from crabcode_core.types.event import TaskUpdateEvent
        try:
            await self._event_sink(TaskUpdateEvent(
                team_id=team_id,
                task_id=task.id,
                status=task.status.value,
                assignee=task.assignee,
                description=task.description,
            ))
        except Exception:
            logger.warning("Failed to emit task update event for %s", task.id, exc_info=True)
