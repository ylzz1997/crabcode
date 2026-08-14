"""Crash recovery for Agent Teams.

When a long-lived process has stale teammate state (for example, a teammate
marked as "busy" that is no longer running), it can be cleaned up explicitly
through this module. Team state is currently in memory and this helper is not
wired into gateway startup, so it does not provide cross-process/server-restart
recovery by itself.

Key design decision: NO automatic restart of interrupted teammates.
This prevents runaway agents burning API credits after a crash.
The human must re-engage them manually.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

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
    """Recover stale teammates in the supplied in-process manager.

    This is not a server-startup hook: a newly-created manager has no persisted
    team manifest to inspect, so callers must reconstruct the runtime first.

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
    recovered_states = getattr(team_manager, "_last_recovered_states", {})
    for item in interrupted:
        state_key = (item.get("team_id", ""), item.get("agent_id", ""))
        info = RecoveryInfo(
            team_id=item["team_id"],
            agent_id=item["agent_id"],
            # ``old_state`` was not part of the original TeamManager return
            # shape.  Keep the BUSY default for third-party managers while
            # preserving the more useful state when the built-in manager
            # reports a stale CANCELLING teammate.
            old_state=item.get(
                "old_state",
                recovered_states.get(state_key, TeammateState.BUSY.value),
            ),
            new_state=TeammateState.READY.value,
        )
        recovery_infos.append(info)

    # Inject notification into the lead session
    if inject_fn:
        team_ids = sorted({item["team_id"] for item in interrupted})
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
            # Prefer an explicitly designated lead.  Older teams may not have
            # one (the main session is not currently represented as a
            # teammate), so retain the historical first-member fallback.
            get_lead_agent = getattr(team_manager, "get_lead_agent", None)
            lead_id = (
                get_lead_agent(team_id)
                if callable(get_lead_agent)
                else None
            )
            if lead_id is None:
                lead_id = next(
                    (item["agent_id"] for item in interrupted if item["team_id"] == team_id),
                    None,
                )
            if lead_id is None:
                continue
            try:
                await inject_fn(lead_id, notification)
            except Exception:
                logger.warning(
                    "Failed to inject recovery notification for %s",
                    lead_id,
                    exc_info=True,
                )

    logger.info("Recovered %d interrupted teammates", len(recovery_infos))
    return recovery_infos


async def cancel_agent_with_retry(
    cancel_fn: Callable[[str], Awaitable[bool]],
    agent_id: str,
    *,
    retries: int = _CANCEL_RETRIES,
    delay: float = _CANCEL_RETRY_DELAY_S,
    force_fn: Callable[[str], Awaitable[None]] | None = None,
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

    # Give the owner a chance to force-transition stale state.  The old
    # implementation only logged and returned False, leaving a teammate stuck
    # in cancelling/busy forever despite its docstring promising a safety net.
    if force_fn is not None:
        try:
            await force_fn(agent_id)
        except Exception:
            logger.debug("Force-transition callback failed for %s", agent_id, exc_info=True)
    if force_fn is not None:
        logger.warning(
            "Force-transitioning agent %s after %d failed cancel attempts",
            agent_id,
            retries,
        )
    else:
        logger.warning(
            "Unable to cancel agent %s after %d attempts; no force callback configured",
            agent_id,
            retries,
        )
    return False
