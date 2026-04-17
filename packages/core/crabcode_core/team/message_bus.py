"""Team message bus — event-driven message passing with backpressure.

Provides O(1) JSONL append for message writes, session injection for
delivery, and auto-wake for idle recipients. No polling required.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from crabcode_core.logging_utils import get_logger
from crabcode_core.team.models import (
    TeamConfig,
    TeamMessage,
    TeammateState,
)

logger = get_logger(__name__)

_MAX_MESSAGE_SIZE_BYTES = 10_000  # 10KB default


class TeamMessageBus:
    """In-process message bus for a single team.

    Each team gets its own TeamMessageBus instance. Messages are:
    1. Written to the recipient's inbox (JSONL, O(1) append)
    2. Injected into the recipient's asyncio.Queue for immediate delivery
    3. Auto-wake triggers restart the recipient's prompt loop if idle

    Backpressure: each agent has a bounded asyncio.Queue. When full,
    the oldest unread message is dropped and a warning is logged.
    """

    def __init__(
        self,
        team_name: str,
        config: TeamConfig,
        *,
        inject_fn: Any | None = None,
        wake_fn: Any | None = None,
        storage_root: Path | None = None,
    ) -> None:
        self._team_name = team_name
        self._config = config
        self._max_queue_size = config.backpressure_queue_size
        self._max_msg_size = config.max_message_size_bytes or _MAX_MESSAGE_SIZE_BYTES
        self._inject_fn = inject_fn  # async (agent_id, from_agent, text) -> None
        self._wake_fn = wake_fn  # async (agent_id, from_agent) -> None

        # Per-agent queues: agent_id -> asyncio.Queue[TeamMessage]
        self._queues: dict[str, asyncio.Queue[TeamMessage]] = {}
        # Per-agent inbox for persistence (agent_id -> list of all messages)
        self._inboxes: dict[str, list[TeamMessage]] = {}
        # Track which agents are registered
        self._registered: set[str] = set()
        # Inbox storage root on disk
        self._storage_root = storage_root

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register_agent(self, agent_id: str) -> None:
        """Register an agent to receive messages."""
        if agent_id in self._registered:
            return
        self._registered.add(agent_id)
        self._queues[agent_id] = asyncio.Queue(maxsize=self._max_queue_size)
        self._inboxes[agent_id] = []

    def unregister_agent(self, agent_id: str) -> None:
        """Unregister an agent. Drops its queue and inbox."""
        self._registered.discard(agent_id)
        self._queues.pop(agent_id, None)
        self._inboxes.pop(agent_id, None)

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    async def send(
        self,
        *,
        from_agent: str,
        to_agent: str,
        text: str,
        msg_type: str = "text",
    ) -> TeamMessage | None:
        """Send a message from one agent to another.

        Steps:
        1. Validate message size
        2. Create TeamMessage
        3. Append to recipient's inbox (source of truth)
        4. Persist to JSONL on disk
        5. Push into recipient's asyncio.Queue (with backpressure)
        6. Inject into recipient's session (delivery mechanism)
        7. Auto-wake idle recipient
        """
        if to_agent not in self._registered:
            logger.warning("Message to unregistered agent %s in team %s", to_agent, self._team_name)
            return None

        if len(text.encode("utf-8")) > self._max_msg_size:
            logger.warning(
                "Message from %s to %s exceeds %d bytes, truncating",
                from_agent, to_agent, self._max_msg_size,
            )
            text = text.encode("utf-8")[:self._max_msg_size].decode("utf-8", errors="replace")

        msg = TeamMessage(
            from_agent=from_agent,
            to_agent=to_agent,
            text=text,
            msg_type=msg_type,
        )

        # 1. Append to inbox (source of truth)
        self._inboxes.setdefault(to_agent, []).append(msg)

        # 2. Persist to JSONL on disk
        await self._persist_message(to_agent, msg)

        # 3. Push into queue (with backpressure)
        queue = self._queues.get(to_agent)
        if queue is not None:
            if queue.full():
                # Drop oldest unread message
                try:
                    dropped = queue.get_nowait()
                    logger.warning(
                        "Backpressure: dropped message %s from %s to %s in team %s",
                        dropped.id, dropped.from_agent, to_agent, self._team_name,
                    )
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(msg)

        # 4. Inject into session (delivery)
        if self._inject_fn:
            try:
                await self._inject_fn(to_agent, from_agent, text)
            except Exception:
                logger.warning("Failed to inject message into session for %s", to_agent, exc_info=True)

        # 5. Auto-wake idle recipient
        if self._wake_fn:
            try:
                await self._wake_fn(to_agent, from_agent)
            except Exception:
                logger.warning("Failed to auto-wake %s", to_agent, exc_info=True)

        return msg

    async def broadcast(
        self,
        *,
        from_agent: str,
        text: str,
        msg_type: str = "text",
    ) -> list[TeamMessage]:
        """Broadcast a message to all registered agents except the sender."""
        messages: list[TeamMessage] = []
        for agent_id in list(self._registered):
            if agent_id == from_agent:
                continue
            msg = await self.send(
                from_agent=from_agent,
                to_agent=agent_id,
                text=text,
                msg_type=msg_type,
            )
            if msg is not None:
                messages.append(msg)
        return messages

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def get_unread(self, agent_id: str) -> list[TeamMessage]:
        """Get all unread messages for an agent from its inbox."""
        inbox = self._inboxes.get(agent_id, [])
        return [msg for msg in inbox if not msg.read]

    def get_all(self, agent_id: str) -> list[TeamMessage]:
        """Get all messages for an agent (read and unread)."""
        return list(self._inboxes.get(agent_id, []))

    async def mark_read(self, agent_id: str, message_ids: list[str] | None = None) -> int:
        """Mark messages as read. If message_ids is None, mark all as read.

        Returns the number of messages marked read.
        Also sends delivery receipts back to the senders.
        """
        inbox = self._inboxes.get(agent_id, [])
        count = 0

        # Group by sender for batched delivery receipts
        receipt_by_sender: dict[str, list[str]] = {}

        for msg in inbox:
            if msg.read:
                continue
            if message_ids is not None and msg.id not in message_ids:
                continue
            msg.read = True
            count += 1
            if msg.from_agent:
                receipt_by_sender.setdefault(msg.from_agent, []).append(msg.id)

        # Persist the updated read state
        await self._persist_inbox(agent_id)

        # Send delivery receipts
        for sender_id, read_ids in receipt_by_sender.items():
            if sender_id not in self._registered:
                continue
            receipt_text = f"Messages read by {agent_id}: {', '.join(read_ids[:10])}"
            if len(read_ids) > 10:
                receipt_text += f" ... and {len(read_ids) - 10} more"
            try:
                await self.send(
                    from_agent=agent_id,
                    to_agent=sender_id,
                    text=receipt_text,
                    msg_type="delivery_receipt",
                )
            except Exception:
                logger.warning("Failed to send delivery receipt to %s", sender_id, exc_info=True)

        return count

    # ------------------------------------------------------------------
    # Queue-based receive (for async consumers)
    # ------------------------------------------------------------------

    async def receive(self, agent_id: str, timeout: float | None = None) -> TeamMessage | None:
        """Wait for the next message for an agent from its queue."""
        queue = self._queues.get(agent_id)
        if queue is None:
            return None
        try:
            if timeout is not None:
                return await asyncio.wait_for(queue.get(), timeout=timeout)
            return await queue.get()
        except asyncio.TimeoutError:
            return None

    def try_receive(self, agent_id: str) -> TeamMessage | None:
        """Non-blocking receive. Returns None if no message is available."""
        queue = self._queues.get(agent_id)
        if queue is None:
            return None
        try:
            return queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _inbox_path(self, agent_id: str) -> Path | None:
        """Return the JSONL inbox path for an agent, or None if no storage root."""
        if self._storage_root is None:
            return None
        return self._storage_root / self._team_name / f"{agent_id}.jsonl"

    async def _persist_message(self, agent_id: str, msg: TeamMessage) -> None:
        """Append a single message to the agent's JSONL inbox file (O(1))."""
        path = self._inbox_path(agent_id)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            line = msg.model_dump_json() + "\n"
            # Use append mode for O(1) writes
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._append_line, path, line)
        except Exception:
            logger.warning("Failed to persist message for %s", agent_id, exc_info=True)

    @staticmethod
    def _append_line(path: Path, line: str) -> None:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)

    async def _persist_inbox(self, agent_id: str) -> None:
        """Rewrite the full inbox file after mark_read (called once per prompt loop)."""
        path = self._inbox_path(agent_id)
        if path is None:
            return
        inbox = self._inboxes.get(agent_id, [])
        if not inbox:
            return
        try:
            lines = [msg.model_dump_json() + "\n" for msg in inbox]
            content = "".join(lines)
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._write_content, path, content)
        except Exception:
            logger.warning("Failed to persist inbox for %s", agent_id, exc_info=True)

    @staticmethod
    def _write_content(path: Path, content: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

    def load_inbox_from_disk(self, agent_id: str) -> list[TeamMessage]:
        """Load messages from an agent's JSONL inbox file on disk."""
        path = self._inbox_path(agent_id)
        if path is None or not path.exists():
            return []
        messages: list[TeamMessage] = []
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        messages.append(TeamMessage.model_validate_json(line))
                    except Exception:
                        logger.debug("Skipping invalid inbox line for %s", agent_id, exc_info=True)
        except OSError:
            logger.warning("Failed to read inbox for %s", agent_id, exc_info=True)
        self._inboxes[agent_id] = messages
        return messages

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def delete_team_inboxes(self) -> None:
        """Delete all inbox files for this team."""
        if self._storage_root is None:
            return
        team_dir = self._storage_root / self._team_name
        if not team_dir.exists():
            return
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._remove_dir, team_dir)

    @staticmethod
    def _remove_dir(path: Path) -> None:
        import shutil
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)

    @property
    def team_name(self) -> str:
        return self._team_name

    @property
    def config(self) -> TeamConfig:
        return self._config

    @property
    def registered_agents(self) -> set[str]:
        return set(self._registered)
