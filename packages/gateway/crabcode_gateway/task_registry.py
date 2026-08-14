"""Lifecycle tracking for gateway background tasks.

HTTP and WebSocket handlers deliberately return before a query/plan finishes.
This module gives those detached tasks an owner (session and, optionally, a
WebSocket connection) so lifecycle operations can cancel and await them.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from contextlib import asynccontextmanager
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from crabcode_gateway.session_registry import get_session_lock

_TASKS_ATTR = "background_tasks"
_CLOSING_ATTR = "closing_sessions"
_GATEWAY_CLOSING_ATTR = "gateway_closing"
_MAX_CANCEL_ROUNDS = 100
_LEASES_ATTR = "session_leases"
_LEASE_EVENTS_ATTR = "session_lease_events"
_LEASE_TASKS_ATTR = "session_lease_tasks"
_OPERATION_TASKS_ATTR = "operation_tasks"

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Async generators/stream adapters are sometimes wrapped in ``wait_for`` or
# another child task.  Preserve the logical lease owner across those task
# boundaries so cleanup does not cancel the parent task that owns the lease.
_LEASE_OWNER: contextvars.ContextVar[
    tuple[str, asyncio.Task[Any]] | None
] = contextvars.ContextVar("crabcode_lease_owner", default=None)


class SessionOperationRejected(RuntimeError):
    """Raised when a request races a session or gateway shutdown."""


class OperationAlreadyRegistered(RuntimeError):
    """Raised when an active operation already owns a session/id pair."""


@dataclass(frozen=True)
class CancelledOperation:
    """A cancelled task whose operation id remains reserved for its terminal."""

    task: asyncio.Task[Any]
    claim: object


def ensure_task_state(app_state: Any) -> None:
    if not hasattr(app_state, _TASKS_ATTR):
        setattr(app_state, _TASKS_ATTR, {})
    if not hasattr(app_state, _CLOSING_ATTR):
        setattr(app_state, _CLOSING_ATTR, set())
    if not hasattr(app_state, _GATEWAY_CLOSING_ATTR):
        setattr(app_state, _GATEWAY_CLOSING_ATTR, False)
    if not hasattr(app_state, _LEASES_ATTR):
        setattr(app_state, _LEASES_ATTR, {})
    if not hasattr(app_state, _LEASE_EVENTS_ATTR):
        setattr(app_state, _LEASE_EVENTS_ATTR, {})
    if not hasattr(app_state, _LEASE_TASKS_ATTR):
        setattr(app_state, _LEASE_TASKS_ATTR, {})
    if not hasattr(app_state, _OPERATION_TASKS_ATTR):
        setattr(app_state, _OPERATION_TASKS_ATTR, {})


def operation_is_registered(
    app_state: Any,
    session_id: str,
    operation_id: str,
) -> bool:
    """Return whether a live task/claim owns an operation id.

    Callers that use this for admission must hold the session registry lock
    until they register the replacement owner.
    """
    ensure_task_state(app_state)
    operations: dict[tuple[str, str], Any] = getattr(
        app_state,
        _OPERATION_TASKS_ATTR,
    )
    owner = operations.get((session_id, operation_id))
    if isinstance(owner, asyncio.Task) and owner.done():
        if operations.get((session_id, operation_id)) is owner:
            operations.pop((session_id, operation_id), None)
        return False
    return owner is not None


def claim_operation(
    app_state: Any,
    session_id: str,
    operation_id: str,
    owner: object,
) -> None:
    """Reserve an operation id across an awaited admission sequence."""
    if operation_is_registered(app_state, session_id, operation_id):
        raise OperationAlreadyRegistered(
            f"operation already active: {operation_id}"
        )
    operations: dict[tuple[str, str], Any] = getattr(
        app_state,
        _OPERATION_TASKS_ATTR,
    )
    operations[(session_id, operation_id)] = owner


def release_operation_claim(
    app_state: Any,
    session_id: str,
    operation_id: str,
    owner: object,
) -> None:
    """Release a reservation only when it is still owned by *owner*."""
    operations: dict[tuple[str, str], Any] = getattr(
        app_state,
        _OPERATION_TASKS_ATTR,
        {},
    )
    key = (session_id, operation_id)
    if operations.get(key) is owner:
        operations.pop(key, None)


def mark_session_closing(app_state: Any, session_id: str) -> None:
    """Fence a session against new background work.

    Callers should set this while holding the gateway session-registry lock,
    before removing the session from the registry.  Keeping the marker until
    the CoreSession has actually closed prevents a concurrent resume/send
    operation from installing or using the same id during cleanup.
    """
    ensure_task_state(app_state)
    getattr(app_state, _CLOSING_ATTR).add(session_id)


def unmark_session_closing(app_state: Any, session_id: str) -> None:
    ensure_task_state(app_state)
    getattr(app_state, _CLOSING_ATTR).discard(session_id)


def track_task(
    app_state: Any,
    session_id: str,
    task: asyncio.Task[Any],
    *,
    owner_tasks: set[asyncio.Task[Any]] | None = None,
    owner_sessions: dict[asyncio.Task[Any], str] | None = None,
    operation_id: str | None = None,
    operation_scope: str | None = None,
    operation_claim: object | None = None,
) -> None:
    """Register a detached task and consume its terminal exception."""
    ensure_task_state(app_state)
    if operation_id is not None:
        operations: dict[tuple[str, str], Any] = getattr(
            app_state,
            _OPERATION_TASKS_ATTR,
        )
        key = (session_id, operation_id)
        previous = operations.get(key)
        if previous is not None and previous is not task:
            if operation_claim is None or previous is not operation_claim:
                raise OperationAlreadyRegistered(
                    f"operation already active: {operation_id}"
                )
        operations[key] = task
        setattr(task, "_crabcode_operation_id", operation_id)
        setattr(task, "_crabcode_operation_scope", operation_scope)

    if (
        getattr(app_state, _GATEWAY_CLOSING_ATTR, False)
        or session_id in getattr(app_state, _CLOSING_ATTR)
    ):
        task.cancel()
    else:
        tasks: dict[str, set[asyncio.Task[Any]]] = getattr(app_state, _TASKS_ATTR)
        tasks.setdefault(session_id, set()).add(task)

    if owner_tasks is not None:
        owner_tasks.add(task)
    if owner_sessions is not None:
        owner_sessions[task] = session_id

    def _done(done: asyncio.Task[Any]) -> None:
        tasks = getattr(app_state, _TASKS_ATTR, {})
        session_tasks = tasks.get(session_id)
        if session_tasks is not None:
            session_tasks.discard(done)
            if not session_tasks:
                tasks.pop(session_id, None)
        if owner_tasks is not None:
            owner_tasks.discard(done)
        if owner_sessions is not None:
            owner_sessions.pop(done, None)
        if operation_id is not None:
            operations = getattr(app_state, _OPERATION_TASKS_ATTR, {})
            key = (session_id, operation_id)
            if operations.get(key) is done:
                operations.pop(key, None)
        try:
            done.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            # The task's owner is responsible for publishing a user-facing
            # error.  Drain the exception here to avoid "Task exception was
            # never retrieved" warnings during shutdown.
            pass

    task.add_done_callback(_done)


async def cancel_operation_task(
    app_state: Any,
    session_id: str,
    operation_id: str,
    *,
    expected_session: object | None = None,
) -> CancelledOperation | None:
    """Cancel one operation and reserve its id until the caller emits terminal.

    Replacing the task with an opaque claim under the registry lock also makes
    concurrent interrupts single-owner: only the first caller can cancel the
    task and publish its terminal event.  Callers must release ``claim`` after
    terminal publication with :func:`release_operation_claim`.
    """
    async with get_session_lock(app_state):
        ensure_task_state(app_state)
        if expected_session is not None and (
            getattr(app_state, "sessions", {}).get(session_id) is not expected_session
            or getattr(app_state, _GATEWAY_CLOSING_ATTR, False)
            or session_id in getattr(app_state, _CLOSING_ATTR, set())
        ):
            return None
        operations: dict[tuple[str, str], Any] = getattr(
            app_state,
            _OPERATION_TASKS_ATTR,
        )
        owner = operations.get((session_id, operation_id))
        if not isinstance(owner, asyncio.Task) or owner.done():
            return None
        claim = object()
        operations[(session_id, operation_id)] = claim
        owner.cancel()
        task = owner

    # Shield the operation task from cancellation of the interrupt request.
    # The cancelled operation may have asynchronous cleanup of its own, and a
    # second cancellation must not abandon that cleanup or strand the claim.
    caller = asyncio.current_task()
    caller_cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if caller is not None and caller.cancelling():
                caller_cancelled = True
        except Exception:
            # The operation's detached-task callback is responsible for
            # reporting/draining its error. Cancellation still owns the claim.
            break
    try:
        task.result()
    except (asyncio.CancelledError, Exception):
        pass
    if caller_cancelled:
        release_operation_claim(
            app_state,
            session_id,
            operation_id,
            claim,
        )
        raise asyncio.CancelledError
    return CancelledOperation(task=task, claim=claim)


async def cancel_tasks(
    tasks: list[asyncio.Task[Any]] | set[asyncio.Task[Any]],
    *,
    exclude_tasks: set[asyncio.Task[Any]] | None = None,
) -> None:
    excluded = set(exclude_tasks or ())
    current = asyncio.current_task()
    if current is not None:
        excluded.add(current)
    pending = [task for task in tasks if task not in excluded and not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


async def cancel_session_tasks(
    app_state: Any,
    session_id: str,
    *,
    keep_closing: bool = False,
    protected_tasks: set[asyncio.Task[Any]] | None = None,
) -> None:
    """Mark a session closing and await all currently registered tasks."""
    ensure_task_state(app_state)
    closing: set[str] = getattr(app_state, _CLOSING_ATTR)
    was_closing = session_id in closing
    closing.add(session_id)
    tasks_by_session: dict[str, set[asyncio.Task[Any]]] = getattr(
        app_state, _TASKS_ATTR
    )
    protected = set(protected_tasks or ())
    current = asyncio.current_task()
    if current is not None:
        protected.add(current)
    try:
        # A task may create a child in a cancellation/finally callback.  Keep
        # draining until the registry is empty; the closing marker makes every
        # such late registration immediately cancellable.  The bound is only a
        # guard against a broken task that continuously manufactures children.
        for _ in range(_MAX_CANCEL_ROUNDS):
            tasks = list(tasks_by_session.get(session_id, set()))
            if not tasks:
                break
            if tasks and all(task in protected for task in tasks):
                # A session task may defensively invoke its own cleanup.  It
                # cannot cancel itself; leave removal to its done callback and
                # avoid spinning through the cancellation-round guard.
                break
            await cancel_tasks(tasks, exclude_tasks=protected)
            # Give done callbacks a scheduling turn before taking the next
            # snapshot.  This is important when a task was already complete
            # when it entered ``cancel_tasks``.
            await asyncio.sleep(0)
            if not tasks_by_session.get(session_id):
                break
        else:
            remaining = len(tasks_by_session.get(session_id, set()))
            if remaining:
                logger.warning(
                    "Session %s still has %d background task(s) after %d cancellation rounds",
                    session_id,
                    remaining,
                    _MAX_CANCEL_ROUNDS,
                )
    finally:
        operations: dict[tuple[str, str], Any] = getattr(
            app_state,
            _OPERATION_TASKS_ATTR,
            {},
        )
        for key in [key for key in operations if key[0] == session_id]:
            operations.pop(key, None)
        if not keep_closing and not was_closing:
            closing.discard(session_id)


async def cleanup_session(
    app_state: Any,
    session_id: str,
    session: Any,
    *,
    protected_task: asyncio.Task[Any] | None = None,
    owns_registry: bool = True,
) -> None:
    """Cancel owned work, close a CoreSession, then release its fence.

    The marker intentionally spans both task cancellation and ``session.close``
    so a resume request cannot recreate the same id while the old object's
    resources are still being released.  This helper is idempotent with
    respect to CoreSession.close().
    """
    # A loaded resume candidate can lose an install race to an already
    # registered CoreSession with the same id.  Such a candidate must be
    # closed without fencing/cancelling the winner's tasks.
    if not owns_registry:
        await session.close()
        return

    mark_session_closing(app_state, session_id)
    try:
        initiator = protected_task or asyncio.current_task()
        protected = {initiator} if initiator is not None else None
        await cancel_session_tasks(
            app_state,
            session_id,
            keep_closing=True,
            protected_tasks=protected,
        )
        # ``cancel_session_tasks`` deliberately leaves the current task
        # alive.  When cleanup is initiated from inside a streaming lease,
        # waiting for all leases here would wait for this task's own finally
        # block, which cannot run until cleanup returns.  Every *other* lease
        # is registered as a session task and has already been cancelled and
        # drained above, so only wait when the current task is not the owner.
        if not await _task_owns_session_lease(app_state, session_id, initiator):
            await wait_session_leases(app_state, session_id)
        await session.close()
    finally:
        unmark_session_closing(app_state, session_id)


async def shielded_cleanup_session(
    app_state: Any,
    session_id: str,
    session: Any,
    *,
    owns_registry: bool = True,
) -> None:
    """Run session cleanup to completion even if its caller is cancelled."""
    logical_owner = _LEASE_OWNER.get()
    initiator = (
        logical_owner[1]
        if logical_owner is not None and logical_owner[0] == session_id
        else asyncio.current_task()
    )
    cleanup_task = asyncio.create_task(
        cleanup_session(
            app_state,
            session_id,
            session,
            protected_task=initiator,
            owns_registry=owns_registry,
        )
    )
    cancelled = False
    try:
        # ``shield`` keeps the child alive but immediately raises in the
        # caller.  A shutdown task can itself receive more than one cancel
        # request while cleanup is draining; keep waiting through every such
        # interruption so the CoreSession and task registry are not abandoned.
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                cancelled = True

        # Retrieve the child's result/exception after it has reached a
        # terminal state.  This also distinguishes an externally cancelled
        # child from cancellation delivered to this waiter.
        await cleanup_task
    except asyncio.CancelledError:
        # If the cleanup child itself was cancelled, preserve that terminal
        # state.  Otherwise this is the caller's cancellation, which is
        # re-raised only after the child has been fully drained.
        cancelled = True
    if cancelled:
        raise asyncio.CancelledError


async def cancel_owner_tasks(
    owner_tasks: set[asyncio.Task[Any]],
) -> None:
    # Owner tasks can schedule another task from a cancellation callback.  A
    # bounded drain keeps WebSocket disconnect cleanup deterministic without
    # allowing an ill-behaved callback to hang shutdown forever.
    for _ in range(_MAX_CANCEL_ROUNDS):
        tasks = list(owner_tasks)
        if not tasks:
            return
        await cancel_tasks(tasks)
        await asyncio.sleep(0)
    if owner_tasks:
        logger.warning(
            "WebSocket owner still has %d background task(s) after cancellation",
            len(owner_tasks),
        )


async def wait_session_leases(app_state: Any, session_id: str) -> None:
    """Wait for in-flight streaming handlers admitted for a session."""
    ensure_task_state(app_state)
    events: dict[str, asyncio.Event] = getattr(app_state, _LEASE_EVENTS_ATTR)
    event = events.get(session_id)
    if event is not None:
        await event.wait()


async def _task_owns_session_lease(
    app_state: Any,
    session_id: str,
    task: asyncio.Task[Any] | None = None,
) -> bool:
    """Return whether *task* owns this session's streaming lease."""
    owner = task if task is not None else asyncio.current_task()
    if owner is None:
        return False
    async with get_session_lock(app_state):
        ensure_task_state(app_state)
        owners: dict[str, set[asyncio.Task[Any]]] = getattr(
            app_state,
            _LEASE_TASKS_ATTR,
            {},
        )
        return owner in owners.get(session_id, set())


@asynccontextmanager
async def session_lease(app_state: Any, session: Any):
    """Admit a long-lived operation until it leaves the session.

    Unlike ``run_session_operation`` this does not create a child task, so an
    async generator can yield incrementally.  Cleanup marks the session as
    closing before waiting on this lease, which closes the resolve/close race.
    """
    session_id = getattr(session, "session_id", None)
    if not session_id:
        raise SessionOperationRejected("session has no id")
    async with get_session_lock(app_state):
        ensure_task_state(app_state)
        if (
            getattr(app_state, _GATEWAY_CLOSING_ATTR, False)
            or session_id in getattr(app_state, _CLOSING_ATTR)
            or getattr(app_state, "sessions", {}).get(session_id) is not session
        ):
            raise SessionOperationRejected("session is closing")
        leases: dict[str, int] = getattr(app_state, _LEASES_ATTR)
        events: dict[str, asyncio.Event] = getattr(app_state, _LEASE_EVENTS_ATTR)
        lease_tasks: dict[str, set[asyncio.Task[Any]]] = getattr(
            app_state,
            _LEASE_TASKS_ATTR,
        )
        leases[session_id] = leases.get(session_id, 0) + 1
        events.setdefault(session_id, asyncio.Event()).clear()
        # Keep the owning request visible to shutdown.  A stalled stream must
        # be cancelled before cleanup waits for its lease to drain.
        current = asyncio.current_task()
        owner_token = None
        if current is not None:
            getattr(app_state, _TASKS_ATTR).setdefault(session_id, set()).add(current)
            lease_tasks.setdefault(session_id, set()).add(current)
            owner_token = _LEASE_OWNER.set((session_id, current))
    try:
        yield
    finally:
        if owner_token is not None:
            _LEASE_OWNER.reset(owner_token)
        async with get_session_lock(app_state):
            leases = getattr(app_state, _LEASES_ATTR, {})
            events = getattr(app_state, _LEASE_EVENTS_ATTR, {})
            lease_tasks = getattr(app_state, _LEASE_TASKS_ATTR, {})
            tasks_by_session = getattr(app_state, _TASKS_ATTR, {})
            current = asyncio.current_task()
            if current is not None:
                session_tasks = tasks_by_session.get(session_id)
                if session_tasks is not None:
                    session_tasks.discard(current)
                    if not session_tasks:
                        tasks_by_session.pop(session_id, None)
            if current is not None:
                owners = lease_tasks.get(session_id)
                if owners is not None:
                    owners.discard(current)
                    if not owners:
                        lease_tasks.pop(session_id, None)
            remaining = leases.get(session_id, 0) - 1
            if remaining <= 0:
                leases.pop(session_id, None)
                event = events.pop(session_id, None)
                if event is not None:
                    event.set()
            else:
                leases[session_id] = remaining


async def run_session_operation(
    app_state: Any,
    session: Any,
    operation: Callable[[], Awaitable[T]],
    *,
    owner_tasks: set[asyncio.Task[Any]] | None = None,
    owner_sessions: dict[asyncio.Task[Any], str] | None = None,
) -> T:
    """Run one session operation with lifecycle admission fencing.

    Route handlers often resolve a ``CoreSession`` and then await into it.  A
    concurrent archive/stop can remove and close that object in the gap.  The
    operation is therefore admitted and registered as a session-owned task
    while holding the registry lock.  Cleanup marks the session as closing,
    cancels all registered work, and waits for it before closing resources.

    The caller's cancellation is propagated after the owned operation is
    settled.  If cleanup cancelled the operation, expose a stable rejection
    error instead of leaking ``CancelledError`` through an unrelated HTTP/WS
    handler.
    """
    session_id = getattr(session, "session_id", None)
    if not session_id:
        raise SessionOperationRejected("session has no id")

    async with get_session_lock(app_state):
        ensure_task_state(app_state)
        if (
            getattr(app_state, _GATEWAY_CLOSING_ATTR, False)
            or session_id in getattr(app_state, _CLOSING_ATTR)
            or getattr(app_state, "sessions", {}).get(session_id) is not session
        ):
            raise SessionOperationRejected("session is closing")
        task: asyncio.Task[T] = asyncio.create_task(operation())
        track_task(
            app_state,
            session_id,
            task,
            owner_tasks=owner_tasks,
            owner_sessions=owner_sessions,
        )

    try:
        # Shield lets us distinguish a shutdown cancellation (the owned task
        # itself is cancelled) from a client disconnect (the waiter is
        # cancelled while the operation remains alive).
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        if task.done() and task.cancelled():
            if (
                getattr(app_state, _GATEWAY_CLOSING_ATTR, False)
                or session_id in getattr(app_state, _CLOSING_ATTR, set())
                or getattr(app_state, "sessions", {}).get(session_id) is not session
            ):
                raise SessionOperationRejected("session is closing")
            raise

        # The caller was cancelled (for example a disconnected HTTP client).
        # Do not leave an operation running against a session whose owner has
        # gone away; cancel and drain it before propagating cancellation.
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise
