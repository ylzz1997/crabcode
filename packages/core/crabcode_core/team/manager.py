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
        self._bridges: dict[tuple[str, str], BridgePolicy] = {}
        self._cross_team_messages: list[CrossTeamMessage] = []

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
        """Create a new team and return its team_id."""
        team_id = name  # Use name as ID for simplicity

        if team_id in self._teams:
            raise ValueError(f"Team '{team_id}' already exists")

        team_settings = getattr(self._settings, "team", None)
        config = config_override or TeamConfig(
            name=name,
            max_teammates=max_teammates or (team_settings.max_teammates if team_settings else 8),
            backpressure_queue_size=team_settings.backpressure_queue_size if team_settings else 100,
            max_message_size_bytes=team_settings.max_message_size_bytes if team_settings else 10_000,
        )

        storage_root = self._resolve_inbox_root()
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
        runtime = self._teams.get(team_id)
        if runtime is None:
            return False

        # Cancel all teammate agents
        for agent_id, info in list(runtime.teammates.items()):
            if info.state not in TeammateState.terminal_states():
                try:
                    await self._agent_manager.cancel_agent(agent_id)
                except Exception:
                    logger.warning("Failed to cancel agent %s during team shutdown", agent_id, exc_info=True)
            info.transition_to(TeammateState.SHUTDOWN, force=True)
            runtime.message_bus.unregister_agent(agent_id)

        # Delete inbox files
        await runtime.message_bus.delete_team_inboxes()

        runtime.state = TeamState.SHUTDOWN
        del self._teams[team_id]
        return True

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

        if len(runtime.teammates) >= runtime.config.max_teammates:
            raise ValueError(
                f"Team '{team_id}' is full ({len(runtime.teammates)}/{runtime.config.max_teammates})"
            )

        # Spawn the sub-agent
        agent_id = await self._agent_manager.spawn_agent(
            prompt=prompt,
            subagent_type="generalPurpose",
            name=name or f"{role.value}-{len(runtime.teammates)}",
            model_profile=model_profile,
            depth=1,
        )

        # Register with the message bus
        runtime.message_bus.register_agent(agent_id)

        # Create teammate info
        info = TeammateInfo(
            agent_id=agent_id,
            role=role,
            state=TeammateState.BUSY,  # starts busy since it was just spawned
            model_profile=model_profile,
            name=name,
        )
        runtime.teammates[agent_id] = info

        # Emit state event
        old_state = TeammateState.IDLE.value
        new_state = TeammateState.BUSY.value
        await self._emit_team_state(team_id, agent_id, old_state, new_state, role.value)

        return agent_id

    async def remove_teammate(self, team_id: str, agent_id: str) -> bool:
        """Remove a teammate from the team."""
        runtime = self._teams.get(team_id)
        if runtime is None:
            return False

        info = runtime.teammates.pop(agent_id, None)
        if info is None:
            return False

        old_state = info.state.value
        info.transition_to(TeammateState.SHUTDOWN, force=True)

        # Cancel the sub-agent
        try:
            await self._agent_manager.cancel_agent(agent_id)
        except Exception:
            logger.warning("Failed to cancel agent %s during removal", agent_id, exc_info=True)

        runtime.message_bus.unregister_agent(agent_id)

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

        msg = await runtime.message_bus.send(
            from_agent=from_agent,
            to_agent=to_agent,
            text=text,
        )

        if msg:
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

        messages = await runtime.message_bus.broadcast(
            from_agent=from_agent,
            text=text,
        )

        for msg in messages:
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
        return await runtime.message_bus.mark_read(agent_id, message_ids)

    # ------------------------------------------------------------------
    # Task board
    # ------------------------------------------------------------------

    async def add_task(self, team_id: str, description: str) -> str:
        """Add a task to the team's task board."""
        runtime = self._teams.get(team_id)
        if runtime is None:
            raise ValueError(f"Team '{team_id}' not found")

        task = TaskItem(description=description)
        runtime.task_board.append(task)

        await self._emit_task_update(team_id, task)
        return task.id

    async def claim_task(self, team_id: str, task_id: str, agent_id: str) -> bool:
        """Atomically claim a task. Protected by asyncio.Lock."""
        runtime = self._teams.get(team_id)
        if runtime is None:
            return False

        async with runtime.task_lock:
            task = next((t for t in runtime.task_board if t.id == task_id), None)
            if task is None:
                return False
            if not task.claim(agent_id):
                return False

        await self._emit_task_update(team_id, task)
        return True

    async def complete_task(self, team_id: str, task_id: str, result: str = "") -> bool:
        """Mark a task as completed."""
        runtime = self._teams.get(team_id)
        if runtime is None:
            return False

        task = next((t for t in runtime.task_board if t.id == task_id), None)
        if task is None:
            return False
        if not task.complete(result):
            return False

        await self._emit_task_update(team_id, task)
        return True

    async def fail_task(self, team_id: str, task_id: str, reason: str = "") -> bool:
        """Mark a task as failed."""
        runtime = self._teams.get(team_id)
        if runtime is None:
            return False

        task = next((t for t in runtime.task_board if t.id == task_id), None)
        if task is None:
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
        return list(runtime.task_board)

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
        return runtime.teammates.get(agent_id)

    def get_team_for_agent(self, agent_id: str) -> str | None:
        """Find which team an agent belongs to."""
        for team_id, runtime in self._teams.items():
            if agent_id in runtime.teammates:
                return team_id
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
        self._bridges[(team_a, team_b)] = policy
        self._bridges[(team_b, team_a)] = policy

    async def send_cross_team(
        self,
        from_team: str,
        from_agent: str,
        to_team: str,
        to_agent: str,
        text: str,
    ) -> CrossTeamMessage | None:
        """Send a message between teams. Requires an allowed bridge policy."""
        policy = self._bridges.get((from_team, to_team), BridgePolicy.DENY)
        if policy == BridgePolicy.DENY:
            logger.warning("Cross-team message blocked: no bridge between %s and %s", from_team, to_team)
            return None

        cross_msg = CrossTeamMessage(
            from_team=from_team,
            from_agent=from_agent,
            to_team=to_team,
            to_agent=to_agent,
            text=text,
            bridge_policy=policy,
        )
        self._cross_team_messages.append(cross_msg)

        # Deliver to the target team
        target_runtime = self._teams.get(to_team)
        if target_runtime is None:
            return cross_msg

        tagged_text = f"[cross-team:{from_team}] {text}"

        if to_agent:
            await target_runtime.message_bus.send(
                from_agent=from_agent,
                to_agent=to_agent,
                text=tagged_text,
                msg_type="cross_team",
            )
        else:
            # Broadcast within target team
            await target_runtime.message_bus.broadcast(
                from_agent=from_agent,
                text=tagged_text,
                msg_type="cross_team",
            )

        return cross_msg

    # ------------------------------------------------------------------
    # Recovery
    # ------------------------------------------------------------------

    async def recover(self) -> list[dict[str, str]]:
        """Recover from a crash: find busy teammates and force-transition them to ready.

        Does NOT auto-restart agents — prevents runaway agents after a crash.
        Returns a list of {team_id, agent_id} pairs that need manual re-engagement.
        """
        interrupted: list[dict[str, str]] = []

        for team_id, runtime in self._teams.items():
            for agent_id, info in list(runtime.teammates.items()):
                if info.state == TeammateState.BUSY:
                    old_state = info.state.value
                    info.transition_to(TeammateState.READY, force=True)
                    await self._emit_team_state(team_id, agent_id, old_state, TeammateState.READY.value, info.role.value)
                    interrupted.append({"team_id": team_id, "agent_id": agent_id})

        return interrupted

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Shutdown all teams."""
        for team_id in list(self._teams.keys()):
            await self.shutdown_team(team_id)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_inbox_root(self) -> Path:
        """Resolve the inbox storage root directory."""
        team_settings = getattr(self._settings, "team", None)
        if team_settings and team_settings.inbox_dir:
            return Path(team_settings.inbox_dir)
        # Default: ~/.crabcode/team_inbox/<project_hash>/
        project_hash = hashlib.md5(self._cwd.encode()).hexdigest()[:12]
        return Path.home() / ".crabcode" / "team_inbox" / project_hash

    def _make_inject_fn(self) -> Callable:
        """Create a message injection function for the bus.

        Injects a message into the recipient's session as a synthetic
        user message so the LLM actually sees it.
        """
        async def _inject(agent_id: str, from_agent: str, text: str) -> None:
            try:
                await self._agent_manager.send_input(
                    agent_id,
                    f"[Message from teammate {from_agent}]: {text}",
                    interrupt=False,
                )
            except Exception:
                logger.debug("Failed to inject message to agent %s", agent_id, exc_info=True)
        return _inject

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
        await self._event_sink(TeamMessageEvent(
            team_id=team_id,
            from_agent=msg.from_agent,
            to_agent=msg.to_agent,
            text=msg.text,
            msg_type=msg.msg_type,
            message_id=msg.id,
        ))

    async def _emit_team_state(
        self, team_id: str, agent_id: str, old_state: str, new_state: str, role: str
    ) -> None:
        from crabcode_core.types.event import TeamStateEvent
        await self._event_sink(TeamStateEvent(
            team_id=team_id,
            agent_id=agent_id,
            old_state=old_state,
            new_state=new_state,
            role=role,
        ))

    async def _emit_task_update(self, team_id: str, task: TaskItem) -> None:
        from crabcode_core.types.event import TaskUpdateEvent
        await self._event_sink(TaskUpdateEvent(
            team_id=team_id,
            task_id=task.id,
            status=task.status.value,
            assignee=task.assignee,
            description=task.description,
        ))
