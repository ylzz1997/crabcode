"""Tests for plan mode: mode switching, tool filtering, permissions, DAG execution, and system prompt."""

from __future__ import annotations

import asyncio
from typing import Any, AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest

from crabcode_core.api.base import APIAdapter, ModelConfig, StreamChunk
from crabcode_core.events import CoreSession
from crabcode_core.permissions.manager import PermissionManager, PermissionMode
from crabcode_core.plan.types import ExecutionPlan, PlanStep
from crabcode_core.plan.executor import PlanExecutor
from crabcode_core.prompts.system import get_system_prompt
from crabcode_core.query.loop import QueryParams, query_loop
from crabcode_core.tools.switch_mode import SwitchModeTool
from crabcode_core.types.config import ApiConfig, CrabCodeSettings
from crabcode_core.types.event import (
    CompactEvent,
    CoreEvent,
    ErrorEvent,
    ModeChangeEvent,
    PlanReadyEvent,
    StreamTextEvent,
    ToolResultEvent,
    TurnCompleteEvent,
)
from crabcode_core.types.message import Message, create_user_message
from crabcode_core.types.tool import (
    PermissionBehavior,
    Tool,
    ToolContext,
    ToolResult,
)


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


class ReadOnlyTool(Tool):
    name = "Read"
    description = "Read a file."
    is_read_only = True
    is_concurrency_safe = True
    input_schema = {"type": "object", "properties": {}}

    async def call(self, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        return ToolResult(result_for_model="contents")


class WriteTool(Tool):
    name = "Write"
    description = "Write a file."
    is_read_only = False
    is_concurrency_safe = False
    input_schema = {"type": "object", "properties": {}}

    async def call(self, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        return ToolResult(result_for_model="written")


class FakeAdapter(APIAdapter):
    def __init__(self, chunks: list[StreamChunk] | None = None):
        self.config = ApiConfig(model="test-model")
        self._chunks = chunks or [
            StreamChunk(type="text", text="Hello"),
            StreamChunk(type="message_stop"),
        ]

    async def stream_message(
        self,
        messages: list[Any],
        system: list[str] | None = None,
        tools: list[dict[str, Any]] | None = None,
        config: ModelConfig | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        for chunk in self._chunks:
            yield chunk

    async def count_tokens(
        self,
        messages: list[Any],
        system: list[str] | None = None,
    ) -> int:
        return 0


# ---------------------------------------------------------------------------
# PlanStep / ExecutionPlan tests
# ---------------------------------------------------------------------------


class TestPlanTypes:
    def test_plan_step_roundtrip(self):
        step = PlanStep(
            id="s1",
            title="Create module",
            description="Create the new module",
            files=["src/module.py"],
            depends_on=[],
        )
        d = step.to_dict()
        restored = PlanStep.from_dict(d)
        assert restored.id == "s1"
        assert restored.files == ["src/module.py"]

    def test_execution_plan_roundtrip(self):
        plan = ExecutionPlan(
            title="Test Plan",
            summary="A test plan",
            steps=[
                PlanStep(id="s1", title="Step 1", description="Do step 1"),
                PlanStep(id="s2", title="Step 2", description="Do step 2", depends_on=["s1"]),
            ],
        )
        d = plan.to_dict()
        restored = ExecutionPlan.from_dict(d)
        assert restored.title == "Test Plan"
        assert len(restored.steps) == 2
        assert restored.steps[1].depends_on == ["s1"]

    def test_validate_dag_valid(self):
        plan = ExecutionPlan(
            title="Valid DAG",
            steps=[
                PlanStep(id="a", title="A", description=""),
                PlanStep(id="b", title="B", description="", depends_on=["a"]),
                PlanStep(id="c", title="C", description="", depends_on=["a"]),
                PlanStep(id="d", title="D", description="", depends_on=["b", "c"]),
            ],
        )
        errors = plan.validate_dag()
        assert errors == []

    def test_validate_dag_cycle(self):
        plan = ExecutionPlan(
            title="Cycle",
            steps=[
                PlanStep(id="a", title="A", description="", depends_on=["b"]),
                PlanStep(id="b", title="B", description="", depends_on=["a"]),
            ],
        )
        errors = plan.validate_dag()
        assert any("cycle" in e.lower() for e in errors)

    def test_validate_dag_missing_dep(self):
        plan = ExecutionPlan(
            title="Missing",
            steps=[
                PlanStep(id="a", title="A", description="", depends_on=["nonexistent"]),
            ],
        )
        errors = plan.validate_dag()
        assert any("unknown" in e.lower() for e in errors)

    def test_get_ready_steps(self):
        plan = ExecutionPlan(
            title="Ready test",
            steps=[
                PlanStep(id="a", title="A", description=""),
                PlanStep(id="b", title="B", description="", depends_on=["a"]),
                PlanStep(id="c", title="C", description=""),
            ],
        )
        ready = plan.get_ready_steps()
        assert {s.id for s in ready} == {"a", "c"}

        plan.steps[0].status = "completed"
        ready = plan.get_ready_steps()
        assert {s.id for s in ready} == {"b", "c"}

    def test_render(self):
        plan = ExecutionPlan(
            title="Test",
            summary="A test",
            steps=[
                PlanStep(id="s1", title="Step 1", description=""),
                PlanStep(id="s2", title="Step 2", description="", depends_on=["s1"]),
            ],
        )
        rendered = plan.render()
        assert "Test" in rendered
        assert "s1" in rendered
        assert "s2" in rendered
        assert "0/2" in rendered


# ---------------------------------------------------------------------------
# PermissionManager plan mode tests
# ---------------------------------------------------------------------------


class TestPermissionManagerPlanMode:
    def test_plan_mode_denies_write_tools(self):
        pm = PermissionManager(mode=PermissionMode.PLAN)
        write_tool = WriteTool()
        result = pm.check(write_tool, {})
        assert result.behavior == PermissionBehavior.DENY
        assert "plan mode" in (result.reason or "").lower()

    def test_plan_mode_allows_read_tools(self):
        pm = PermissionManager(mode=PermissionMode.PLAN)
        read_tool = ReadOnlyTool()
        result = pm.check(read_tool, {})
        assert result.behavior == PermissionBehavior.ALLOW


# ---------------------------------------------------------------------------
# CoreSession.switch_mode tests
# ---------------------------------------------------------------------------


class TestCoreSessionSwitchMode:
    def test_switch_to_plan(self):
        session = CoreSession(settings=CrabCodeSettings(api=ApiConfig(model="test")))
        session._permission_manager = PermissionManager()
        session._initialized = True

        assert session.agent_mode == "agent"
        assert session.switch_mode("plan")
        assert session.agent_mode == "plan"
        assert session._permission_manager.mode == PermissionMode.PLAN

    def test_switch_back_to_agent(self):
        session = CoreSession(settings=CrabCodeSettings(api=ApiConfig(model="test")))
        session._permission_manager = PermissionManager()
        session._initialized = True

        session.switch_mode("plan")
        session.switch_mode("agent")
        assert session.agent_mode == "agent"
        assert session._permission_manager.mode == PermissionMode.DEFAULT

    def test_switch_preserves_original_mode(self):
        pm = PermissionManager(mode=PermissionMode.ACCEPT_EDITS)
        session = CoreSession(settings=CrabCodeSettings(api=ApiConfig(model="test")))
        session._permission_manager = pm
        session._initialized = True

        session.switch_mode("plan")
        assert pm.mode == PermissionMode.PLAN
        session.switch_mode("agent")
        assert pm.mode == PermissionMode.ACCEPT_EDITS

    def test_invalid_mode(self):
        session = CoreSession(settings=CrabCodeSettings(api=ApiConfig(model="test")))
        assert not session.switch_mode("invalid")

    def test_noop_same_mode(self):
        session = CoreSession(settings=CrabCodeSettings(api=ApiConfig(model="test")))
        assert session.switch_mode("agent")
        assert session.agent_mode == "agent"

    def test_set_and_get_plan(self):
        session = CoreSession(settings=CrabCodeSettings(api=ApiConfig(model="test")))
        assert session.current_plan is None
        plan_data = {"title": "test", "steps": []}
        session.set_plan(plan_data)
        assert session.current_plan == plan_data


# ---------------------------------------------------------------------------
# System prompt plan mode tests
# ---------------------------------------------------------------------------


class TestSystemPromptPlanMode:
    def test_plan_mode_includes_plan_section(self):
        prompt = get_system_prompt(
            enabled_tools=["Read", "Grep"],
            model_id="test-model",
            agent_mode="plan",
        )
        full_text = "\n".join(prompt)
        assert "Plan mode is active" in full_text

    def test_agent_mode_excludes_plan_section(self):
        prompt = get_system_prompt(
            enabled_tools=["Read", "Write"],
            model_id="test-model",
            agent_mode="agent",
        )
        full_text = "\n".join(prompt)
        assert "Plan mode is active" not in full_text

    def test_plan_mode_skips_execution_sections(self):
        plan_prompt = get_system_prompt(
            enabled_tools=["Read"],
            model_id="test-model",
            agent_mode="plan",
        )
        agent_prompt = get_system_prompt(
            enabled_tools=["Read"],
            model_id="test-model",
            agent_mode="agent",
        )
        plan_text = "\n".join(plan_prompt)
        agent_text = "\n".join(agent_prompt)
        assert "# Doing tasks" not in plan_text
        assert "# Doing tasks" in agent_text
        assert "# Executing actions with care" not in plan_text
        assert "# Git safety protocol" not in plan_text

    def test_plan_mode_requires_stopping_after_plan_submission(self):
        prompt = get_system_prompt(
            enabled_tools=["Read", "SwitchMode"],
            model_id="test-model",
            agent_mode="plan",
        )
        plan_text = "\n".join(prompt).lower()
        assert "do not call any other tools" in plan_text
        assert "it does not mean execution has started yet" in plan_text


# ---------------------------------------------------------------------------
# QueryParams tool filtering tests
# ---------------------------------------------------------------------------


class TestToolFiltering:
    def test_plan_mode_filters_write_tools(self):
        read_tool = ReadOnlyTool()
        write_tool = WriteTool()
        tools = [read_tool, write_tool]

        schemas = [
            t.to_api_schema()
            for t in tools
            if t.is_enabled and ("plan" != "plan" or t.is_read_only)
        ]
        assert len(schemas) == 1
        assert schemas[0]["name"] == "Read"

    def test_agent_mode_includes_all_tools(self):
        read_tool = ReadOnlyTool()
        write_tool = WriteTool()
        tools = [read_tool, write_tool]

        schemas = [
            t.to_api_schema()
            for t in tools
            if t.is_enabled and ("agent" != "plan" or t.is_read_only)
        ]
        assert len(schemas) == 2


# ---------------------------------------------------------------------------
# SwitchModeTool tests
# ---------------------------------------------------------------------------


class TestSwitchModeTool:
    def test_switch_emits_mode_change_event(self):
        async def _run():
            tool = SwitchModeTool()
            queue: asyncio.Queue[CoreEvent] = asyncio.Queue()
            context = ToolContext(tool_event_queue=queue)

            result = await tool.call({"target_mode": "plan"}, context)
            assert not result.is_error

            event = queue.get_nowait()
            assert isinstance(event, ModeChangeEvent)
            assert event.mode == "plan"

        asyncio.run(_run())

    def test_switch_with_plan_emits_plan_ready(self):
        async def _run():
            tool = SwitchModeTool()
            queue: asyncio.Queue[CoreEvent] = asyncio.Queue()
            context = ToolContext(tool_event_queue=queue)

            plan_data = {
                "title": "Test Plan",
                "summary": "Test",
                "steps": [
                    {"id": "s1", "title": "Step 1", "description": "Do 1"},
                    {"id": "s2", "title": "Step 2", "description": "Do 2", "depends_on": ["s1"]},
                ],
            }
            result = await tool.call({"target_mode": "agent", "plan": plan_data}, context)
            assert not result.is_error
            assert "user can choose whether to execute, revise, or cancel it" in result.result_for_model

            events = []
            while not queue.empty():
                events.append(queue.get_nowait())
            types = [type(e).__name__ for e in events]
            assert "PlanReadyEvent" in types
            assert "ModeChangeEvent" in types

        asyncio.run(_run())


class TestQueryLoopPlanSubmission:
    def test_emergency_compact_appends_resume_prompt_and_continues(self, monkeypatch: pytest.MonkeyPatch):
        async def _run():
            class ResumeAwareAdapter(APIAdapter):
                def __init__(self):
                    self.config = ApiConfig(model="test-model", max_tokens=128)
                    self.seen_messages: list[Message] = []

                async def stream_message(
                    self,
                    messages: list[Any],
                    system: list[str] | None = None,
                    tools: list[dict[str, Any]] | None = None,
                    config: ModelConfig | None = None,
                ) -> AsyncGenerator[StreamChunk, None]:
                    self.seen_messages = list(messages)
                    assert isinstance(messages[-1].content, str)
                    assert "Conversation was compacted to fit the context window" in messages[-1].content
                    yield StreamChunk(type="text", text="continued")
                    yield StreamChunk(type="message_stop")

                async def count_tokens(
                    self,
                    messages: list[Any],
                    system: list[str] | None = None,
                ) -> int:
                    return 0

            async def fake_compact(messages, api_adapter=None):
                return [create_user_message(content="[Conversation summary: compacted]")]

            token_calls = {"count": 0}

            def fake_estimate(messages, system=None):
                token_calls["count"] += 1
                if token_calls["count"] == 1:
                    return 10_000
                return 10

            monkeypatch.setattr("crabcode_core.compact.compact.compact_conversation", fake_compact)
            monkeypatch.setattr("crabcode_core.compact.compact.estimate_token_count", fake_estimate)

            adapter = ResumeAwareAdapter()
            params = QueryParams(
                messages=[create_user_message(content="do something")],
                system_prompt=["test"],
                user_context={},
                system_context={},
                tools=[],
                tool_context=ToolContext(tool_event_queue=asyncio.Queue()),
                api_adapter=adapter,
                agent_mode="agent",
                api_config=adapter.config,
                context_window=256,
            )

            events = [event async for event in query_loop(params)]
            assert any(isinstance(e, CompactEvent) for e in events)
            assert any(isinstance(e, StreamTextEvent) and e.text == "continued" for e in events)

        asyncio.run(_run())

    def test_empty_response_after_compact_retries_once(self, monkeypatch: pytest.MonkeyPatch):
        async def _run():
            class EmptyThenContinueAdapter(APIAdapter):
                def __init__(self):
                    self.config = ApiConfig(model="test-model", max_tokens=128)
                    self.calls = 0

                async def stream_message(
                    self,
                    messages: list[Any],
                    system: list[str] | None = None,
                    tools: list[dict[str, Any]] | None = None,
                    config: ModelConfig | None = None,
                ) -> AsyncGenerator[StreamChunk, None]:
                    self.calls += 1
                    if self.calls == 1:
                        assert isinstance(messages[-1].content, str)
                        assert "Conversation was compacted to fit the context window" in messages[-1].content
                        yield StreamChunk(type="message_stop")
                        return
                    assert isinstance(messages[-1].content, str)
                    assert "previous attempt returned no content after compaction" in messages[-1].content
                    yield StreamChunk(type="text", text="resumed")
                    yield StreamChunk(type="message_stop")

                async def count_tokens(
                    self,
                    messages: list[Any],
                    system: list[str] | None = None,
                ) -> int:
                    return 0

            async def fake_compact(messages, api_adapter=None):
                return [create_user_message(content="[Conversation summary: compacted]")]

            token_calls = {"count": 0}

            def fake_estimate(messages, system=None):
                token_calls["count"] += 1
                if token_calls["count"] == 1:
                    return 10_000
                return 10

            monkeypatch.setattr("crabcode_core.compact.compact.compact_conversation", fake_compact)
            monkeypatch.setattr("crabcode_core.compact.compact.estimate_token_count", fake_estimate)

            adapter = EmptyThenContinueAdapter()
            params = QueryParams(
                messages=[create_user_message(content="do something")],
                system_prompt=["test"],
                user_context={},
                system_context={},
                tools=[],
                tool_context=ToolContext(tool_event_queue=asyncio.Queue()),
                api_adapter=adapter,
                agent_mode="agent",
                api_config=adapter.config,
                context_window=256,
            )

            events = [event async for event in query_loop(params)]
            assert adapter.calls == 2
            assert any(isinstance(e, StreamTextEvent) and e.text == "resumed" for e in events)

        asyncio.run(_run())

    def test_switch_mode_submission_ends_turn_immediately(self):
        async def _run():
            chunks = [
                StreamChunk(type="tool_use_start", tool_use_id="tool-1", tool_name="SwitchMode"),
                StreamChunk(
                    type="tool_use_end",
                    tool_use_id="tool-1",
                    tool_name="SwitchMode",
                    tool_input_json=(
                        '{"target_mode":"agent","plan":{"title":"Test Plan","summary":"Test",'
                        '"steps":[{"id":"s1","title":"Step 1","description":"Do 1"}]}}'
                    ),
                ),
                StreamChunk(type="message_stop"),
            ]
            adapter = FakeAdapter(chunks=chunks)
            queue: asyncio.Queue[CoreEvent] = asyncio.Queue()
            params = QueryParams(
                messages=[],
                system_prompt=["test"],
                user_context={},
                system_context={},
                tools=[SwitchModeTool()],
                tool_context=ToolContext(tool_event_queue=queue),
                api_adapter=adapter,
                agent_mode="plan",
            )

            events = [event async for event in query_loop(params)]
            tool_results = [e for e in events if isinstance(e, ToolResultEvent)]
            turn_complete = [e for e in events if isinstance(e, TurnCompleteEvent)]

            assert len(tool_results) == 1
            assert tool_results[0].tool_name == "SwitchMode"
            assert len(turn_complete) == 1
            assert turn_complete[0].reason == "mode_switch_requested"

        asyncio.run(_run())

    def test_switch_rejects_invalid_plan_dag(self):
        async def _run():
            tool = SwitchModeTool()
            queue: asyncio.Queue[CoreEvent] = asyncio.Queue()
            context = ToolContext(tool_event_queue=queue)

            plan_data = {
                "title": "Bad Plan",
                "steps": [
                    {"id": "s1", "title": "Step 1", "description": "Do 1", "depends_on": ["s2"]},
                    {"id": "s2", "title": "Step 2", "description": "Do 2", "depends_on": ["s1"]},
                ],
            }
            result = await tool.call({"target_mode": "agent", "plan": plan_data}, context)
            assert result.is_error
            assert "cycle" in result.result_for_model.lower()

        asyncio.run(_run())

    def test_validate_input(self):
        async def _run():
            tool = SwitchModeTool()
            assert await tool.validate_input({"target_mode": "agent"}) is None
            assert await tool.validate_input({"target_mode": "plan"}) is None
            err = await tool.validate_input({"target_mode": "invalid"})
            assert err is not None

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# PlanExecutor DAG scheduling tests
# ---------------------------------------------------------------------------


class TestPlanExecutor:
    def test_linear_plan_executes_in_order(self):
        async def _run():
            plan = ExecutionPlan(
                title="Linear",
                steps=[
                    PlanStep(id="s1", title="Step 1", description="Do 1"),
                    PlanStep(id="s2", title="Step 2", description="Do 2", depends_on=["s1"]),
                    PlanStep(id="s3", title="Step 3", description="Do 3", depends_on=["s2"]),
                ],
            )

            async def mock_spawn(prompt: str, subagent_type: str = "generalPurpose", name: str | None = None) -> str:
                step_id = name.split("]")[0].strip("[") if name else "unknown"
                return f"agent-{step_id}"

            fake_snapshot = MagicMock()
            fake_snapshot.status = "completed"
            fake_snapshot.final_result = "done"
            fake_snapshot.error = ""

            async def mock_wait(agent_id: str, timeout_ms: int | None = None) -> Any:
                return fake_snapshot

            executor = PlanExecutor(
                plan=plan,
                spawn_fn=mock_spawn,
                wait_fn=mock_wait,
            )

            async for _ in executor.execute():
                pass

            assert plan.status == "completed"
            assert all(s.status == "completed" for s in plan.steps)

        asyncio.run(_run())

    def test_parallel_steps_launch_together(self):
        async def _run():
            plan = ExecutionPlan(
                title="Parallel",
                steps=[
                    PlanStep(id="s1", title="Step 1", description="Do 1"),
                    PlanStep(id="s2", title="Step 2", description="Do 2"),
                    PlanStep(id="s3", title="Step 3", description="Do 3", depends_on=["s1", "s2"]),
                ],
            )

            spawned: list[str] = []

            async def mock_spawn(prompt: str, subagent_type: str = "generalPurpose", name: str | None = None) -> str:
                step_id = name.split("]")[0].strip("[") if name else "unknown"
                spawned.append(step_id)
                return f"agent-{step_id}"

            fake_snapshot = MagicMock()
            fake_snapshot.status = "completed"
            fake_snapshot.final_result = "done"
            fake_snapshot.error = ""

            async def mock_wait(agent_id: str, timeout_ms: int | None = None) -> Any:
                return fake_snapshot

            executor = PlanExecutor(
                plan=plan,
                spawn_fn=mock_spawn,
                wait_fn=mock_wait,
            )

            async for _ in executor.execute():
                pass

            assert plan.status == "completed"
            assert set(spawned) == {"s1", "s2", "s3"}

        asyncio.run(_run())

    def test_failed_step_cancels_dependents(self):
        async def _run():
            plan = ExecutionPlan(
                title="Fail cascade",
                steps=[
                    PlanStep(id="s1", title="Step 1", description="Do 1"),
                    PlanStep(id="s2", title="Step 2", description="Do 2", depends_on=["s1"]),
                ],
            )

            async def mock_spawn(prompt: str, subagent_type: str = "generalPurpose", name: str | None = None) -> str:
                return "agent-fail"

            fail_snapshot = MagicMock()
            fail_snapshot.status = "failed"
            fail_snapshot.final_result = ""
            fail_snapshot.error = "something broke"

            async def mock_wait(agent_id: str, timeout_ms: int | None = None) -> Any:
                return fail_snapshot

            executor = PlanExecutor(
                plan=plan,
                spawn_fn=mock_spawn,
                wait_fn=mock_wait,
            )

            async for _ in executor.execute():
                pass

            assert plan.steps[0].status == "failed"
            assert plan.steps[1].status == "cancelled"
            assert plan.status == "failed"

        asyncio.run(_run())

    def test_invalid_dag_errors(self):
        async def _run():
            plan = ExecutionPlan(
                title="Invalid",
                steps=[
                    PlanStep(id="a", title="A", description="", depends_on=["b"]),
                    PlanStep(id="b", title="B", description="", depends_on=["a"]),
                ],
            )

            executor = PlanExecutor(
                plan=plan,
                spawn_fn=AsyncMock(),
                wait_fn=AsyncMock(),
            )

            events = []
            async for event in executor.execute():
                events.append(event)

            assert any(isinstance(e, ErrorEvent) for e in events)

        asyncio.run(_run())
