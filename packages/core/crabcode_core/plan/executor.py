"""Plan executor — DAG-based scheduler that runs plan steps via sub-agents."""

from __future__ import annotations

import asyncio
from typing import Any, AsyncGenerator, Callable, Awaitable

from crabcode_core.logging_utils import get_logger
from crabcode_core.plan.types import ExecutionPlan, PlanStep
from crabcode_core.types.event import (
    CoreEvent,
    ErrorEvent,
    StreamTextEvent,
)

logger = get_logger(__name__)


class PlanExecutionEvent:
    """Wrapper for plan execution progress events."""

    def __init__(self, step_id: str, status: str, message: str = "") -> None:
        self.step_id = step_id
        self.status = status
        self.message = message


class PlanExecutor:
    """Execute an ExecutionPlan using sub-agents with DAG-based scheduling.

    Uses CoreSession.spawn_agent/wait_agent to run each step as a sub-agent,
    respecting dependency ordering and maximizing parallelism.
    """

    def __init__(
        self,
        plan: ExecutionPlan,
        spawn_fn: Callable[..., Awaitable[str]],
        wait_fn: Callable[[str, int | None], Awaitable[Any]],
        max_concurrency: int = 4,
    ) -> None:
        self._plan = plan
        self._spawn_fn = spawn_fn
        self._wait_fn = wait_fn
        self._max_concurrency = max_concurrency
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._step_map = {s.id: s for s in plan.steps}

    @property
    def plan(self) -> ExecutionPlan:
        return self._plan

    async def execute(self) -> AsyncGenerator[CoreEvent, None]:
        """Execute the plan, yielding progress events.

        Schedules steps in topological order, running independent steps
        in parallel up to max_concurrency.
        """
        errors = self._plan.validate_dag()
        if errors:
            yield ErrorEvent(
                message=f"Plan DAG validation failed: {'; '.join(errors)}",
                recoverable=False,
            )
            return

        self._plan.status = "running"
        yield StreamTextEvent(
            text=f"\n**Executing plan: {self._plan.title}** ({len(self._plan.steps)} steps)\n\n"
        )

        pending_tasks: dict[str, asyncio.Task[None]] = {}
        step_results: dict[str, bool] = {}
        event_queue: asyncio.Queue[CoreEvent] = asyncio.Queue()

        async def _run_step(step: PlanStep) -> None:
            """Run a single step as a sub-agent."""
            async with self._semaphore:
                step.status = "running"
                await event_queue.put(
                    StreamTextEvent(text=f"  ◉ Starting step [{step.id}]: {step.title}\n")
                )

                try:
                    agent_id = await self._spawn_fn(
                        prompt=step.description,
                        subagent_type=step.subagent_type,
                        name=f"[{step.id}] {step.title}",
                    )
                    step.agent_id = agent_id

                    snapshot = await self._wait_fn(agent_id, None)
                    if snapshot is None:
                        step.status = "failed"
                        step.error = "Agent returned no result"
                        step_results[step.id] = False
                        await event_queue.put(
                            StreamTextEvent(text=f"  ✗ Step [{step.id}] failed: no result\n")
                        )
                        return

                    step.result = snapshot.final_result or ""
                    if snapshot.status == "completed":
                        step.status = "completed"
                        step_results[step.id] = True
                        await event_queue.put(
                            StreamTextEvent(text=f"  ● Step [{step.id}] completed: {step.title}\n")
                        )
                    else:
                        step.status = "failed"
                        step.error = snapshot.error or f"Agent status: {snapshot.status}"
                        step_results[step.id] = False
                        await event_queue.put(
                            StreamTextEvent(
                                text=f"  ✗ Step [{step.id}] failed: {step.error}\n"
                            )
                        )
                except Exception as e:
                    step.status = "failed"
                    step.error = str(e)
                    step_results[step.id] = False
                    await event_queue.put(
                        StreamTextEvent(text=f"  ✗ Step [{step.id}] error: {e}\n")
                    )

        async def _schedule_ready_steps() -> int:
            """Launch all steps whose dependencies are satisfied. Returns count launched."""
            launched = 0
            for step in self._plan.get_ready_steps():
                failed_deps = [
                    dep for dep in step.depends_on
                    if dep in step_results and not step_results[dep]
                ]
                if failed_deps:
                    step.status = "cancelled"
                    step.error = f"Cancelled: dependency failed ({', '.join(failed_deps)})"
                    step_results[step.id] = False
                    await event_queue.put(
                        StreamTextEvent(
                            text=f"  ⊘ Step [{step.id}] cancelled (dependency failed)\n"
                        )
                    )
                    continue

                task = asyncio.create_task(_run_step(step))
                pending_tasks[step.id] = task
                launched += 1
            return launched

        # Main scheduling loop
        done_sentinel = object()

        async def _producer() -> None:
            """Schedule steps and wait for completion, then signal done."""
            try:
                while True:
                    launched = await _schedule_ready_steps()

                    active = {
                        sid: t for sid, t in pending_tasks.items()
                        if not t.done()
                    }

                    if not active:
                        remaining = [
                            s for s in self._plan.steps
                            if s.status == "pending"
                        ]
                        if not remaining:
                            break
                        if not launched:
                            for s in remaining:
                                s.status = "cancelled"
                                s.error = "No runnable dependencies"
                                step_results[s.id] = False
                            break
                        continue

                    done, _ = await asyncio.wait(
                        set(active.values()),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for t in done:
                        if t.exception():
                            logger.warning("Step task raised: %s", t.exception())
            finally:
                await event_queue.put(done_sentinel)

        producer_task = asyncio.create_task(_producer())

        try:
            while True:
                item = await event_queue.get()
                if item is done_sentinel:
                    break
                yield item
        finally:
            producer_task.cancel()
            try:
                await producer_task
            except asyncio.CancelledError:
                pass

        # Summary
        completed = sum(1 for s in self._plan.steps if s.status == "completed")
        failed = sum(1 for s in self._plan.steps if s.status == "failed")
        cancelled = sum(1 for s in self._plan.steps if s.status == "cancelled")
        total = len(self._plan.steps)

        if failed == 0 and cancelled == 0:
            self._plan.status = "completed"
        else:
            self._plan.status = "failed"

        summary = (
            f"\n**Plan execution {'completed' if self._plan.status == 'completed' else 'finished with errors'}**: "
            f"{completed}/{total} steps completed"
        )
        if failed:
            summary += f", {failed} failed"
        if cancelled:
            summary += f", {cancelled} cancelled"
        summary += "\n"

        yield StreamTextEvent(text=summary)
