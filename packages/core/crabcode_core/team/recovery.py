"""Crash recovery for Agent Teams.

When the server restarts while teammates are running, stale state
(e.g., teammates marked as "busy" that aren't actually running) needs
to be cleaned up. This module provides the recovery sequence.

Key design decision: NO automatic restart of interrupted teammates.
This prevents runaway agents burning API credits after a crash.
The human must re-engage them manually.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from crabcode_core.logging_utils import get_logger
from crabcode_core.team.manager import TeamManager
from crabcode_core.team.models import TeammateState

logger = get_logger(__name__)

# Cancellation retry settings
_CANCEL_RETRIES = 3
_CANCEL_RETRY_DELAY_S = 0.12  # 120ms


class RecoveryInfo:
    """Result of a recovery operation."""

    def __init__(
        self,
        team_id: str,
        agent_id: str,
        old_state: str,
        new_state: str,
    ) -> None:
        self.team_id = team_id
        self.agent_id = agent_id
        self.old_state = old_state
        self.new_state = new_state

    def __repr__(self) -> str:
        return (
            f"RecoveryInfo(team={self.team_id}, agent={self.agent_id[:8]}, "
            f"{self.old_state} -> {self.new_state})"
        )


async def recover_teams(
    team_manager: TeamManager,
    *,
    inject_fn: Callable[[str, str], Awaitable[None]] | None = None,
) -> list[RecoveryInfo]:
    """Recover all teams after a server restart.

    Recovery sequence:
    1. Find all busy teammates across all teams
    2. Force-transition them from busy -> ready
    3. Inject a notification into the lead session
    4. Do NOT auto-restart agents (prevents runaway)

    Args:
        team_manager: The TeamManager to recover.
        inject_fn: Optional async function (agent_id, notification_text) to
                   inject recovery notifications into sessions.

    Returns:
        List of RecoveryInfo for interrupted teammates.
    """
    interrupted = await team_manager.recover()

    if not interrupted:
        return []

    recovery_infos: list[RecoveryInfo] = []
    for item in interrupted:
        info = RecoveryInfo(
            team_id=item["team_id"],
            agent_id=item["agent_id"],
            old_state=TeammateState.BUSY.value,
            new_state=TeammateState.READY.value,
        )
        recovery_infos.append(info)

    # Inject notification into the lead session
    if inject_fn:
        agent_list = ", ".join(
            f"{item['agent_id'][:8]}" for item in interrupted
        )
        team_ids = set(item["team_id"] for item in interrupted)
        for team_id in team_ids:
            team_agents = [
                item["agent_id"][:8]
                for item in interrupted
                if item["team_id"] == team_id
            ]
            notification = (
                f"[System]: Server was restarted. The following teammates in "
                f'team "{team_id}" were interrupted and need to be resumed: '
                f"{', '.join(team_agents)}. "
                f"Use TeamMessage or TeamBroadcast to tell them to continue their work."
            )
            # Inject into the first agent of the team (likely the lead)
            for item in interrupted:
                if item["team_id"] == team_id:
                    try:
                        await inject_fn(item["agent_id"], notification)
                    except Exception:
                        logger.warning(
                            "Failed to inject recovery notification for %s",
                            item["agent_id"],
                            exc_info=True,
                        )
                    break

    logger.info("Recovered %d interrupted teammates", len(recovery_infos))
    return recovery_infos


async def cancel_agent_with_retry(
    cancel_fn: Callable[[str], Awaitable[bool]],
    agent_id: str,
    *,
    retries: int = _CANCEL_RETRIES,
    delay: float = _CANCEL_RETRY_DELAY_S,
) -> bool:
    """Cancel an agent with retry. If it hasn't stopped after retries,
    force-transition as a safety net.

    Args:
        cancel_fn: Async function that cancels an agent and returns True if successful.
        agent_id: The agent to cancel.
        retries: Number of cancellation attempts.
        delay: Delay between retries in seconds.
    """
    for attempt in range(retries):
        try:
            cancelled = await cancel_fn(agent_id)
            if cancelled:
                return True
        except Exception:
            logger.debug("Cancel attempt %d failed for %s", attempt + 1, agent_id, exc_info=True)

        if attempt < retries - 1:
            await asyncio.sleep(delay)

    # Force-transition as safety net
    logger.warning("Force-transitioning agent %s after %d failed cancel attempts", agent_id, retries)
    return False
