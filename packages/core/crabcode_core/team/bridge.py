"""TeamBridge — controlled cross-team communication.

Allows teams to send messages to each other through a bridge policy.
Policies: allow_all, allow_tagged, deny (default).
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from crabcode_core.logging_utils import get_logger
from crabcode_core.team.manager import TeamManager
from crabcode_core.team.models import BridgePolicy, CrossTeamMessage

logger = get_logger(__name__)


class TeamBridge:
    """Manages cross-team communication with policy-based access control.

    Usage:
        bridge = TeamBridge(team_manager)
        bridge.register("team-a", "team-b", BridgePolicy.ALLOW_TAGGED)
        await bridge.send("team-a", "agent-1", "team-b", "agent-2", "Hello!")
    """

    def __init__(self, team_manager: TeamManager) -> None:
        self._manager = team_manager

    def register(
        self,
        team_a: str,
        team_b: str,
        policy: BridgePolicy = BridgePolicy.ALLOW_TAGGED,
    ) -> None:
        """Register a bridge policy between two teams."""
        self._manager.register_bridge(team_a, team_b, policy)

    def get_policy(self, team_a: str, team_b: str) -> BridgePolicy:
        """Get the bridge policy between two teams."""
        return self._manager._bridges.get((team_a, team_b), BridgePolicy.DENY)

    async def send(
        self,
        from_team: str,
        from_agent: str,
        to_team: str,
        to_agent: str,
        text: str,
    ) -> CrossTeamMessage | None:
        """Send a cross-team message. Returns None if the bridge policy denies it."""
        return await self._manager.send_cross_team(
            from_team, from_agent, to_team, to_agent, text,
        )

    async def broadcast_to_team(
        self,
        from_team: str,
        from_agent: str,
        to_team: str,
        text: str,
    ) -> CrossTeamMessage | None:
        """Broadcast a message to all members of another team."""
        return await self._manager.send_cross_team(
            from_team, from_agent, to_team, "", text,
        )
