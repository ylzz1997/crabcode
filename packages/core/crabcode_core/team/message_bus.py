"""Team message bus — event-driven message passing with backpressure.

Provides O(1) JSONL append for message writes, session injection for
delivery, and auto-wake for idle recipients. No polling required.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from crabcode_core.logging_utils import get_logger
from crabcode_core.team.models import (
    TeamConfig,
    TeamMessage,
)

logger = get_logger(__name__)

_MAX_MESSAGE_SIZE_BYTES = 10_000  # 10KB default

try:  # POSIX file locking; the fallback keeps Windows imports usable.
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover - exercised only on Windows
    fcntl = None  # type: ignore[assignment]

_PROCESS_FILE_LOCK_GUARD = threading.Lock()
_PROCESS_FILE_LOCKS: dict[str, threading.Lock] = {}


def _process_file_lock(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _PROCESS_FILE_LOCK_GUARD:
        return _PROCESS_FILE_LOCKS.setdefault(key, threading.Lock())


@contextmanager
def _team_file_lock(
    storage_root: Path,
    team_name: str,
    *,
    shared: bool = False,
) -> Iterator[None]:
    """Coordinate all disk operations for one team.

    The sidecar lives beside the team directory, so deleting the directory
    cannot remove the lock inode while another writer is waiting.  The naming
    matches ``InboxStorage`` so the two storage APIs also serialize when they
    point at the same root.
    """
    storage_root.mkdir(parents=True, exist_ok=True)
    lock_path = storage_root / f".{team_name}.team.lock"
    process_lock = _process_file_lock(lock_path)
    with process_lock:
        with open(lock_path, "a", encoding="utf-8") as lock_file:
            if fcntl is not None:
                fcntl.flock(
                    lock_file.fileno(),
                    fcntl.LOCK_SH if shared else fcntl.LOCK_EX,
                )
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


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
        self._validate_component(team_name, "team name")
        self._team_name = team_name
        self._config = config
        self._max_queue_size = int(config.backpressure_queue_size)
        self._max_msg_size = int(config.max_message_size_bytes or _MAX_MESSAGE_SIZE_BYTES)
        if self._max_queue_size <= 0:
            raise ValueError("backpressure_queue_size must be greater than zero")
        if self._max_msg_size <= 0:
            raise ValueError("max_message_size_bytes must be greater than zero")
        self._inject_fn = inject_fn  # async (agent_id, from_agent, text) -> None
        self._wake_fn = wake_fn  # async (agent_id, from_agent) -> None

        # Per-agent queues: agent_id -> asyncio.Queue[TeamMessage]
        self._queues: dict[str, asyncio.Queue[TeamMessage]] = {}
        # Per-agent inbox for persistence (agent_id -> list of all messages)
        self._inboxes: dict[str, list[TeamMessage]] = {}
        # Track which agents are registered
        self._registered: set[str] = set()
        # Registration-scoped wake events let receive() abandon a queue that
        # was invalidated by unregister_agent() instead of waiting forever on
        # an orphaned queue object.
        self._registration_events: dict[str, asyncio.Event] = {}
        # Inbox storage root on disk
        self._storage_root = storage_root
        # A per-recipient lock serializes append and read-state rewrites.  Without
        # this, an async mark_read rewrite can race a send append and lose data.
        self._inbox_locks: dict[str, asyncio.Lock] = {}
        # Disk operations run in an executor and may outlive a registration
        # change.  Epochs prevent a stale operation from repopulating the
        # in-memory state of a later registration for the same agent id.
        self._registration_epochs: dict[str, int] = {}

    @staticmethod
    def _validate_component(value: str, label: str) -> None:
        """Reject path separators and traversal components used in inbox paths."""
        if (
            not isinstance(value, str)
            or not value
            or "\x00" in value
            or value in {".", ".."}
        ):
            raise ValueError(f"Invalid {label}")
        if Path(value).is_absolute() or "/" in value or "\\" in value:
            raise ValueError(f"Invalid {label}: path separators are not allowed")

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register_agent(self, agent_id: str) -> None:
        """Register an agent to receive messages."""
        self._validate_component(agent_id, "agent id")
        if agent_id in self._registered:
            return
        self._registered.add(agent_id)
        self._registration_epochs[agent_id] = (
            self._registration_epochs.get(agent_id, 0) + 1
        )
        self._registration_events[agent_id] = asyncio.Event()
        self._queues[agent_id] = asyncio.Queue(maxsize=self._max_queue_size)
        # Preserve the lock across unregister/re-register cycles.  An
        # in-flight send or mark_read may still own the previous lock object.
        self._inbox_locks.setdefault(agent_id, asyncio.Lock())
        # Recover durable messages before exposing the recipient.  Previously a
        # newly-created bus silently reset the in-memory inbox and made JSONL
        # persistence useless after a restart.
        messages = self.load_inbox_from_disk(agent_id)
        self._inboxes[agent_id] = messages
        # Queue only unread messages, retaining the full inbox as the source of
        # truth.  If more messages exist than the bounded queue can hold, keep
        # the newest entries in the live queue; older entries remain readable.
        unread = [message for message in messages if not message.read]
        for message in unread[-self._max_queue_size :]:
            try:
                self._queues[agent_id].put_nowait(message.model_copy(deep=True))
            except asyncio.QueueFull:
                break

    def unregister_agent(self, agent_id: str) -> None:
        """Unregister an agent. Drops its queue and inbox."""
        self._registration_epochs[agent_id] = (
            self._registration_epochs.get(agent_id, 0) + 1
        )
        event = self._registration_events.pop(agent_id, None)
        if event is not None:
            event.set()
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
        expected_epoch: int | None = None,
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
        if not isinstance(text, str):
            logger.warning("Non-text message rejected for %s in team %s", to_agent, self._team_name)
            return None
        if to_agent not in self._registered:
            logger.warning("Message to unregistered agent %s in team %s", to_agent, self._team_name)
            return None
        # Capture the registration generation before waiting on the recipient
        # lock.  An unregister/re-register cycle must fence this operation;
        # otherwise a send that started for the old runtime can be delivered to
        # a replacement using the same agent id after it acquires the lock.
        initial_epoch = self._registration_epochs.get(to_agent)
        if initial_epoch is None:
            return None
        if expected_epoch is not None and initial_epoch != expected_epoch:
            logger.warning(
                "Recipient %s registration changed before delivery in team %s",
                to_agent,
                self._team_name,
            )
            return None

        if len(text.encode("utf-8")) > self._max_msg_size:
            logger.warning(
                "Message from %s to %s exceeds %d bytes, truncating",
                from_agent, to_agent, self._max_msg_size,
            )
            # Ignore a partial UTF-8 sequence at the byte boundary so the
            # resulting payload never exceeds the configured byte limit.
            text = text.encode("utf-8")[:self._max_msg_size].decode("utf-8", errors="ignore")
            if not text:
                return None

        msg = TeamMessage(
            from_agent=from_agent,
            to_agent=to_agent,
            text=text,
            msg_type=msg_type,
        )

        # 1. Append to inbox and persist under one recipient lock.  Re-check
        # registration after waiting: shutdown/removal may have happened while
        # another send was in progress.
        lock = self._inbox_locks.setdefault(to_agent, asyncio.Lock())
        async with lock:
            if (
                to_agent not in self._registered
                or self._registration_epochs.get(to_agent) != initial_epoch
            ):
                logger.warning("Recipient %s was unregistered before delivery", to_agent)
                return None
            registration_epoch = initial_epoch
            if not await self._persist_message(to_agent, msg):
                # Persistence is the commit point for a send.  Do not expose a
                # message through memory, injection, or wake-up when its durable
                # append failed.
                return None
            if (
                to_agent not in self._registered
                or self._registration_epochs.get(to_agent, 0) != registration_epoch
            ):
                # The recipient can be removed while the executor is writing
                # the durable line.  Keep it for a future registration without
                # recreating the old in-memory inbox or injecting into a stale
                # session callback.
                if to_agent in self._registered:
                    self._refresh_registered_inbox(to_agent)
                return None
            self._inboxes.setdefault(to_agent, []).append(msg)

        # 3. Push into queue (with backpressure)
        queue = self._queues.get(to_agent)
        if queue is not None and (
            to_agent in self._registered
            and self._registration_epochs.get(to_agent, 0) == registration_epoch
        ):
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
            queue.put_nowait(msg.model_copy(deep=True))

        # 4. Inject into session (delivery)
        if self._inject_fn and (
            to_agent in self._registered
            and self._registration_epochs.get(to_agent, 0) == registration_epoch
        ):
            try:
                await self._inject_fn(to_agent, from_agent, text)
            except Exception:
                logger.warning("Failed to inject message into session for %s", to_agent, exc_info=True)

        # 5. Auto-wake idle recipient
        if self._wake_fn and (
            to_agent in self._registered
            and self._registration_epochs.get(to_agent, 0) == registration_epoch
        ):
            try:
                await self._wake_fn(to_agent, from_agent)
            except Exception:
                logger.warning("Failed to auto-wake %s", to_agent, exc_info=True)

        return msg

    def registration_epoch(self, agent_id: str) -> int | None:
        """Return the current registration generation for *agent_id*.

        The generation is an internal lifecycle fence used by TeamManager to
        prevent an authorization made for one runtime from being delivered to
        a replacement that reuses the same agent id.  ``None`` means the agent
        is not currently registered.
        """
        if agent_id not in self._registered:
            return None
        return self._registration_epochs.get(agent_id)

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
        if agent_id in self._registered and self._storage_root is not None:
            # Another bus/process may have appended since this instance was
            # registered. Keep query APIs from serving a permanently stale
            # in-memory snapshot; real-time receive() remains queue-based.
            self._refresh_registered_inbox(agent_id)
        inbox = self._inboxes.get(agent_id, [])
        # Do not expose mutable in-memory records: callers changing ``read``
        # directly would bypass receipt generation and durable persistence.
        return [msg.model_copy(deep=True) for msg in inbox if not msg.read]

    def get_all(self, agent_id: str) -> list[TeamMessage]:
        """Get all messages for an agent (read and unread)."""
        if agent_id in self._registered and self._storage_root is not None:
            self._refresh_registered_inbox(agent_id)
        return [msg.model_copy(deep=True) for msg in self._inboxes.get(agent_id, [])]

    async def mark_read(self, agent_id: str, message_ids: list[str] | None = None) -> int:
        """Mark messages as read. If message_ids is None, mark all as read.

        Returns the number of messages marked read.
        Also sends delivery receipts back to the senders.
        """
        if agent_id not in self._registered:
            return 0
        # As with send(), fence a read acknowledgement that was started by a
        # previous registration if it waits behind an in-flight rewrite.
        initial_epoch = self._registration_epochs.get(agent_id)
        if initial_epoch is None:
            return 0
        count = 0
        # Group by sender for batched delivery receipts
        # Keep the sender registration epoch with each receipt batch.  A
        # sender id can be reused while this method is awaiting the inbox
        # rewrite; an acknowledgement for the old runtime must not be sent to
        # the replacement runtime.
        receipt_by_sender: dict[tuple[str, int], list[str]] = {}

        # Mutate and persist under the same recipient lock used by send().
        lock = self._inbox_locks.setdefault(agent_id, asyncio.Lock())
        async with lock:
            # Re-check after waiting on the lock.  unregister_agent() can run
            # synchronously while another coroutine is persisting a message.
            if (
                agent_id not in self._registered
                or self._registration_epochs.get(agent_id) != initial_epoch
            ):
                return 0
            registration_epoch = initial_epoch
            live_refs_by_id = {
                message.id: message
                for message in self._inboxes.get(agent_id, [])
            }
            if self._storage_root is not None:
                # Another bus/process may have committed messages after this
                # registration loaded its in-memory snapshot.  Refresh at the
                # acknowledgement's linearization point so ``mark all`` (and
                # explicit ids) cannot silently leave already-durable messages
                # unread.  A later concurrent append remains correctly outside
                # this acknowledgement and is merged by _write_content().
                self._refresh_registered_inbox(agent_id)
            staged_inbox = [
                message.model_copy(deep=True)
                for message in self._inboxes.get(agent_id, [])
            ]
            for msg in staged_inbox:
                if msg.read:
                    continue
                if message_ids is not None and msg.id not in message_ids:
                    continue
                msg.read = True
                count += 1
                # A receipt is itself a message.  Generating a receipt for it
                # would make two agents acknowledge acknowledgements forever.
                sender_epoch = self._registration_epochs.get(msg.from_agent)
                if (
                    msg.from_agent
                    and sender_epoch is not None
                    and msg.msg_type != "delivery_receipt"
                ):
                    receipt_by_sender.setdefault(
                        (msg.from_agent, sender_epoch),
                        [],
                    ).append(msg.id)
            persisted_inbox = await self._persist_inbox(
                agent_id,
                inbox=staged_inbox,
            )
            if persisted_inbox is None:
                # Keep the original in-memory read state and suppress receipts
                # when the durable acknowledgement could not be committed.
                return 0
            if (
                agent_id in self._registered
                and self._registration_epochs.get(agent_id) == registration_epoch
            ):
                # Preserve references returned by send(): historically callers
                # observe their TeamMessage instance transition to read after a
                # successful acknowledgement.  Apply that mutation only after
                # persistence succeeds, while also adopting externally merged
                # messages from the committed snapshot.
                existing_by_id = {
                    message.id: message
                    for message in self._inboxes.get(agent_id, [])
                }
                # Keep objects returned by send() live across the disk refresh
                # above; callers historically observe their ``read`` field
                # transition after a successful acknowledgement.
                existing_by_id.update(live_refs_by_id)
                committed_inbox: list[TeamMessage] = []
                for persisted in persisted_inbox:
                    existing = existing_by_id.get(persisted.id)
                    if existing is None:
                        committed_inbox.append(persisted)
                        continue
                    existing.read = persisted.read
                    committed_inbox.append(existing)
                self._inboxes[agent_id] = committed_inbox

        # The recipient may have been unregistered while the executor was
        # rewriting its file.  Do not emit receipts from a stale registration.
        if (
            agent_id not in self._registered
            or self._registration_epochs.get(agent_id, 0) != registration_epoch
        ):
            if agent_id in self._registered:
                self._refresh_registered_inbox(agent_id)
            return count

        # Send delivery receipts
        for (sender_id, sender_epoch), read_ids in receipt_by_sender.items():
            if (
                sender_id not in self._registered
                or self._registration_epochs.get(sender_id) != sender_epoch
            ):
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
                    expected_epoch=sender_epoch,
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
        registration_event = self._registration_events.get(agent_id)
        if queue is None or registration_event is None:
            return None
        queue_task = asyncio.create_task(queue.get())
        invalidated_task = asyncio.create_task(registration_event.wait())
        try:
            done, pending = await asyncio.wait(
                {queue_task, invalidated_task},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            if not done or invalidated_task in done:
                return None
            if queue_task not in done:
                return None
            return queue_task.result().model_copy(deep=True)
        except asyncio.CancelledError:
            for task in (queue_task, invalidated_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(queue_task, invalidated_task, return_exceptions=True)
            raise

    def try_receive(self, agent_id: str) -> TeamMessage | None:
        """Non-blocking receive. Returns None if no message is available."""
        queue = self._queues.get(agent_id)
        if queue is None:
            return None
        try:
            return queue.get_nowait().model_copy(deep=True)
        except asyncio.QueueEmpty:
            return None

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _inbox_path(self, agent_id: str) -> Path | None:
        """Return the JSONL inbox path for an agent, or None if no storage root."""
        if self._storage_root is None:
            return None
        self._validate_component(agent_id, "agent id")
        return self._storage_root / self._team_name / f"{agent_id}.jsonl"

    async def _persist_message(self, agent_id: str, msg: TeamMessage) -> bool:
        """Append a single message to the agent's JSONL inbox file (O(1))."""
        path = self._inbox_path(agent_id)
        if path is None:
            return True
        try:
            line = msg.model_dump_json() + "\n"
            # Use append mode for O(1) writes
            loop = asyncio.get_event_loop()
            operation = loop.run_in_executor(None, self._append_line, path, line)
            try:
                await asyncio.shield(operation)
            except asyncio.CancelledError:
                # Executor threads cannot be cancelled safely.  Drain the
                # write before propagating cancellation so shutdown cannot
                # delete an inbox while an append is still in flight.
                await asyncio.shield(operation)
                raise
        except Exception:
            logger.warning("Failed to persist message for %s", agent_id, exc_info=True)
            return False
        return True

    @staticmethod
    def _append_line(path: Path, line: str) -> None:
        # ``path`` is <storage_root>/<team>/<agent>.jsonl.  Acquire the team
        # lock before creating the directory; otherwise delete_team_inboxes()
        # could remove it and this late append would recreate stale state.
        storage_root = path.parent.parent
        team_name = path.parent.name
        with _team_file_lock(storage_root, team_name):
            path.parent.mkdir(parents=True, exist_ok=True)
            lock_path = path.with_name(f".{path.name}.lock")
            process_lock = _process_file_lock(lock_path)
            with process_lock:
                with open(lock_path, "a", encoding="utf-8") as lock_file:
                    if fcntl is not None:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                    try:
                        with open(path, "a", encoding="utf-8") as f:
                            f.write(line)
                            f.flush()
                            os.fsync(f.fileno())
                    finally:
                        if fcntl is not None:
                            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    async def _persist_inbox(
        self,
        agent_id: str,
        *,
        inbox: list[TeamMessage] | None = None,
    ) -> list[TeamMessage] | None:
        """Persist and return the committed inbox, or ``None`` on failure."""
        desired = inbox if inbox is not None else self._inboxes.get(agent_id, [])
        path = self._inbox_path(agent_id)
        if path is None:
            return [message.model_copy(deep=True) for message in desired]
        try:
            lines = [msg.model_dump_json() + "\n" for msg in desired]
            content = "".join(lines)
            loop = asyncio.get_event_loop()
            operation = loop.run_in_executor(None, self._write_content, path, content)
            try:
                committed = await asyncio.shield(operation)
            except asyncio.CancelledError:
                committed = await asyncio.shield(operation)
                raise
            return self._parse_inbox_content(agent_id, committed)
        except Exception:
            logger.warning("Failed to persist inbox for %s", agent_id, exc_info=True)
            return None

    @staticmethod
    def _write_content(path: Path, content: str) -> str:
        storage_root = path.parent.parent
        team_name = path.parent.name
        with _team_file_lock(storage_root, team_name):
            path.parent.mkdir(parents=True, exist_ok=True)
            lock_path = path.with_name(f".{path.name}.lock")
            process_lock = _process_file_lock(lock_path)
            with process_lock:
                with open(lock_path, "a", encoding="utf-8") as lock_file:
                    if fcntl is not None:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                    try:
                        # Merge lines appended by another bus/process since this
                        # instance loaded its inbox.  A plain rewrite would
                        # otherwise erase those messages even though append and
                        # rewrite each held a file lock.
                        current = (
                            path.read_text(encoding="utf-8")
                            if path.exists()
                            else ""
                        )
                        merged = TeamMessageBus._merge_jsonl(current, content)

                        # Replace atomically so a crash or concurrent reader never
                        # observes a half-written JSONL file.
                        tmp_path: str | None = None
                        try:
                            with tempfile.NamedTemporaryFile(
                                mode="w",
                                encoding="utf-8",
                                dir=path.parent,
                                prefix=f".{path.name}.",
                                suffix=".tmp",
                                delete=False,
                            ) as f:
                                tmp_path = f.name
                                f.write(merged)
                                f.flush()
                                os.fsync(f.fileno())
                            os.replace(tmp_path, path)
                            tmp_path = None
                        finally:
                            if tmp_path is not None:
                                try:
                                    os.unlink(tmp_path)
                                except OSError:
                                    pass
                    finally:
                        if fcntl is not None:
                            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        return merged

    @staticmethod
    def _merge_jsonl(current: str, desired: str) -> str:
        """Merge two inbox snapshots by message id while preserving order."""
        desired_by_id: dict[str, str] = {}
        desired_unkeyed: list[str] = []
        for raw in desired.splitlines():
            if not raw.strip():
                continue
            try:
                message_id = str(json.loads(raw).get("id", ""))
            except Exception:
                message_id = ""
            if message_id:
                desired_by_id[message_id] = raw
            else:
                desired_unkeyed.append(raw)

        merged: list[str] = []
        for raw in current.splitlines():
            if not raw.strip():
                continue
            try:
                message_id = str(json.loads(raw).get("id", ""))
            except Exception:
                message_id = ""
            if message_id and message_id in desired_by_id:
                merged.append(
                    TeamMessageBus._merge_json_line(
                        raw,
                        desired_by_id.pop(message_id),
                    )
                )
            else:
                merged.append(raw)
        merged.extend(desired_by_id.values())
        merged.extend(desired_unkeyed)
        return "".join(f"{line}\n" for line in merged)

    @staticmethod
    def _merge_json_line(current: str, desired: str) -> str:
        """Merge a line while keeping the monotonic read acknowledgement."""
        try:
            current_obj = json.loads(current)
            desired_obj = json.loads(desired)
            if current_obj.get("read"):
                desired_obj["read"] = True
            return json.dumps(desired_obj, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            return desired

    def load_inbox_from_disk(self, agent_id: str) -> list[TeamMessage]:
        """Load messages from an agent's JSONL inbox file on disk."""
        self._validate_component(agent_id, "agent id")
        path = self._inbox_path(agent_id)
        if path is None or not path.exists():
            self._inboxes[agent_id] = []
            return []
        messages: list[TeamMessage] = []
        try:
            # Read under the same sidecar lock used by append/rewrite.  Without
            # this, a second process can observe a partially appended JSON line,
            # skip it as invalid, and later rewrite it away permanently.
            content = self._read_content(path)
            messages = self._parse_inbox_content(agent_id, content)
        except OSError:
            logger.warning("Failed to read inbox for %s", agent_id, exc_info=True)
        self._inboxes[agent_id] = messages
        return messages

    @staticmethod
    def _parse_inbox_content(agent_id: str, content: str) -> list[TeamMessage]:
        """Parse a persisted inbox while tolerating isolated corrupt lines."""
        messages: list[TeamMessage] = []
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                messages.append(TeamMessage.model_validate_json(line))
            except Exception:
                logger.debug(
                    "Skipping invalid inbox line for %s",
                    agent_id,
                    exc_info=True,
                )
        return messages

    def _refresh_registered_inbox(self, agent_id: str) -> None:
        """Merge durable messages into a replacement registration.

        A stale append/rewrite may finish after ``register_agent`` has loaded
        the file. Refresh synchronously at that boundary and enqueue any new
        unread records, while preserving read acknowledgements already held in
        memory. This never invokes the old session's injection callback.
        """
        if agent_id not in self._registered:
            return
        queue = self._queues.get(agent_id)
        queued_before_refresh = {
            getattr(message, "id", "")
            for message in list(getattr(queue, "_queue", ()))
        } if queue is not None else set()
        existing = [
            message.model_copy(deep=True)
            for message in self._inboxes.get(agent_id, [])
        ]
        disk = self.load_inbox_from_disk(agent_id)
        merged_by_id: dict[str, TeamMessage] = {}
        order: list[str] = []
        for message in [*existing, *disk]:
            if message.id not in merged_by_id:
                merged_by_id[message.id] = message.model_copy(deep=True)
                order.append(message.id)
                continue
            current = merged_by_id[message.id]
            merged_by_id[message.id] = message.model_copy(deep=True)
            merged_by_id[message.id].read = current.read or message.read
        merged = [merged_by_id[message_id] for message_id in order]
        self._inboxes[agent_id] = merged

        if queue is None:
            return
        queued_ids = {
            getattr(message, "id", "")
            for message in list(getattr(queue, "_queue", ()))
        } | queued_before_refresh
        for message in merged:
            if message.read or message.id in queued_ids:
                continue
            if queue.full():
                break
            queue.put_nowait(message.model_copy(deep=True))
            queued_ids.add(message.id)

    @staticmethod
    def _read_content(path: Path) -> str:
        """Read a JSONL file while sharing its process/POSIX lock."""
        storage_root = path.parent.parent
        team_name = path.parent.name
        with _team_file_lock(storage_root, team_name, shared=True):
            lock_path = path.with_name(f".{path.name}.lock")
            process_lock = _process_file_lock(lock_path)
            with process_lock:
                with open(lock_path, "a", encoding="utf-8") as lock_file:
                    if fcntl is not None:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_SH)
                    try:
                        return path.read_text(encoding="utf-8")
                    finally:
                        if fcntl is not None:
                            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def delete_team_inboxes(self) -> None:
        """Delete all inbox files for this team."""
        if self._storage_root is None:
            return
        team_dir = self._storage_root / self._team_name
        loop = asyncio.get_event_loop()
        operation = loop.run_in_executor(None, self._remove_dir, team_dir)
        try:
            await asyncio.shield(operation)
        except asyncio.CancelledError:
            # rmtree runs in a thread and cannot be cancelled safely.  Drain
            # it before propagating cancellation so a caller cannot reuse the
            # team directory while deletion is still in progress.
            await asyncio.shield(operation)
            raise

    def _remove_dir(self, path: Path) -> None:
        import shutil
        with _team_file_lock(self._storage_root or path.parent, self._team_name):
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
