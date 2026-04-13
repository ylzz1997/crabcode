from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path
import tempfile
from typing import Any, AsyncGenerator
from unittest.mock import patch

from crabcode_core.api.base import APIAdapter, ModelConfig, StreamChunk
from crabcode_core.events import CoreSession
from crabcode_core.session.storage import SessionStorage
from crabcode_core.tools.ask_user import AskUserTool
from crabcode_core.tools.file_read import FileReadTool
from crabcode_core.types.config import ApiConfig, CrabCodeSettings
from crabcode_core.types.event import (
    AgentStateEvent,
    ChoiceRequestEvent,
    ChoiceResponseEvent,
    PermissionRequestEvent,
    PermissionResponseEvent,
    ToolResultEvent,
)
from crabcode_core.types.message import Message
from crabcode_core.types.tool import Tool, ToolContext, ToolResult


class SleepTool(Tool):
    name = "Sleep"
    description = "Sleep for a short period."
    is_read_only = True
    is_concurrency_safe = True
    input_schema = {
        "type": "object",
        "properties": {"seconds": {"type": "number"}},
        "required": ["seconds"],
    }

    async def call(self, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        await asyncio.sleep(float(tool_input["seconds"]))
        return ToolResult(result_for_model=f"slept {tool_input['seconds']}")


class DangerousTool(Tool):
    name = "Danger"
    description = "A write-like test tool."
    is_read_only = False
    is_concurrency_safe = True
    input_schema = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
    }

    async def call(self, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        return ToolResult(result_for_model=f"danger {tool_input['value']}")


class FakeAdapter(APIAdapter):
    def __init__(self, config: ApiConfig):
        self.config = config

    async def stream_message(
        self,
        messages: list[Message],
        system: list[str],
        tools: list[dict[str, Any]],
        config: ModelConfig,
    ) -> AsyncGenerator[StreamChunk, None]:
        prompt = messages[-1].text_content
        yield StreamChunk(type="message_start", usage={"input_tokens": 1})
        if prompt.startswith("sleep"):
            seconds = prompt.split(" ", 1)[1]
            yield StreamChunk(type="tool_use_start", tool_use_id="tool-1", tool_name="Sleep")
            yield StreamChunk(type="tool_use_delta", tool_use_id="tool-1", tool_input_json=f'{{"seconds": {seconds}}}')
            yield StreamChunk(
                type="tool_use_end",
                tool_use_id="tool-1",
                tool_name="Sleep",
                tool_input_json=f'{{"seconds": {seconds}}}',
            )
        elif prompt == "danger":
            yield StreamChunk(type="tool_use_start", tool_use_id="tool-1", tool_name="Danger")
            yield StreamChunk(type="tool_use_delta", tool_use_id="tool-1", tool_input_json='{"value":"x"}')
            yield StreamChunk(
                type="tool_use_end",
                tool_use_id="tool-1",
                tool_name="Danger",
                tool_input_json='{"value":"x"}',
            )
        elif prompt == "ask":
            yield StreamChunk(type="tool_use_start", tool_use_id="tool-1", tool_name="AskUser")
            yield StreamChunk(
                type="tool_use_delta",
                tool_use_id="tool-1",
                tool_input_json='{"question":"Pick one","options":["a","b"]}',
            )
            yield StreamChunk(
                type="tool_use_end",
                tool_use_id="tool-1",
                tool_name="AskUser",
                tool_input_json='{"question":"Pick one","options":["a","b"]}',
            )
        else:
            yield StreamChunk(type="text", text=f"done:{prompt}")
        yield StreamChunk(type="message_stop", usage={"output_tokens": 1})

    async def count_tokens(self, messages: list[Message], system: list[str]) -> int:
        return 1


async def _drain_until(predicate, agen):
    items = []
    async for item in agen:
        items.append(item)
        if predicate(item):
            break
    return items


async def _wait_for_queue_event(queue: asyncio.Queue, predicate, timeout: float = 2.0):
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        item = await asyncio.wait_for(queue.get(), timeout=remaining)
        if predicate(item):
            return item


def _make_settings() -> CrabCodeSettings:
    return CrabCodeSettings(
        api=ApiConfig(provider="openai", model="fake"),
        agent={
            "max_turns": 3,
            "timeout": 5,
            "max_output_chars": 2000,
            "max_concurrency": 4,
            "max_depth": 2,
            "max_active_agents_per_run": 16,
        },
    )


def _make_session() -> CoreSession:
    session = CoreSession(
        cwd=".",
        settings=_make_settings(),
        tools=[SleepTool(), DangerousTool(), AskUserTool(), FileReadTool()],
    )
    return session


async def _initialize_fake_session(session: CoreSession) -> None:
    with patch("crabcode_core.api.create_adapter", side_effect=lambda config: FakeAdapter(config)), patch(
        "crabcode_core.api.registry.create_adapter", side_effect=lambda config: FakeAdapter(config)
    ):
        await session.initialize()
    session._api_adapter = FakeAdapter(session.settings.api)  # type: ignore[attr-defined]
    if session._agent_manager is not None:  # type: ignore[attr-defined]
        session._agent_manager._adapter_provider = lambda model_name=None: FakeAdapter(  # type: ignore[attr-defined]
            session.settings.get_api_config(model_name)
        )


@contextlib.contextmanager
def _patched_storage_home():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        with patch("crabcode_core.session.storage.Path.home", return_value=home), patch(
            "crabcode_core.session.meta_db.Path.home", return_value=home
        ):
            yield


def test_agent_spawn_runs_in_parallel():
    async def _run():
        with _patched_storage_home():
            session = _make_session()
            await _initialize_fake_session(session)
            start = time.perf_counter()
            a = await session.spawn_agent(prompt="sleep 0.2")
            b = await session.spawn_agent(prompt="sleep 0.2")
            await session.wait_agent(a, timeout_ms=2000)
            await session.wait_agent(b, timeout_ms=2000)
            elapsed = time.perf_counter() - start
            assert elapsed < 0.35

    asyncio.run(_run())


def test_agent_permission_routes_by_agent_id():
    async def _run():
        with _patched_storage_home():
            session = _make_session()
            await _initialize_fake_session(session)
            spawned = await session.spawn_agent(prompt="danger")
            event = None
            for _ in range(10):
                item = await session._agent_event_queue.get()  # type: ignore[attr-defined]
                if isinstance(item, PermissionRequestEvent):
                    event = item
                    break
            assert event is not None
            assert event.agent_id == spawned
            await session.respond_permission(
                PermissionResponseEvent(
                    tool_use_id=event.tool_use_id,
                    allowed=False,
                    agent_id=event.agent_id,
                )
            )
            snapshot = await session.wait_agent(spawned, timeout_ms=2000)
            assert snapshot is not None
            assert snapshot.status == "completed"
            assert "Permission denied" in snapshot.final_result

    asyncio.run(_run())


def test_agent_choice_routes_by_agent_id():
    async def _run():
        with _patched_storage_home():
            session = _make_session()
            await _initialize_fake_session(session)
            agent_id = await session.spawn_agent(prompt="ask")
            event = None
            for _ in range(10):
                item = await session._agent_event_queue.get()  # type: ignore[attr-defined]
                if isinstance(item, ChoiceRequestEvent):
                    event = item
                    break
            assert event is not None
            assert event.agent_id == agent_id
            await session.respond_choice(
                ChoiceResponseEvent(
                    tool_use_id=event.tool_use_id,
                    selected=["b"],
                    agent_id=event.agent_id,
                )
            )
            snapshot = await session.wait_agent(agent_id, timeout_ms=2000)
            assert snapshot is not None
            assert "User selected: b" in snapshot.final_result

    asyncio.run(_run())


def test_explore_agents_only_get_read_only_tools():
    async def _run():
        with _patched_storage_home():
            session = _make_session()
            await _initialize_fake_session(session)
            agent_id = await session.spawn_agent(prompt="danger", subagent_type="explore")
            snapshot = await session.wait_agent(agent_id, timeout_ms=2000)
            assert snapshot is not None
            assert "unknown tool 'Danger'" in snapshot.final_result

    asyncio.run(_run())


def test_cancel_agent_marks_snapshot_cancelled():
    async def _run():
        with _patched_storage_home():
            session = _make_session()
            await _initialize_fake_session(session)
            agent_id = await session.spawn_agent(prompt="sleep 1")
            await asyncio.sleep(0.1)
            ok = await session.cancel_agent(agent_id)
            assert ok is True
            snapshot = await session.wait_agent(agent_id, timeout_ms=2000)
            assert snapshot is not None
            assert snapshot.status == "cancelled"

    asyncio.run(_run())


def test_legacy_agent_tool_still_waits_for_completion():
    async def _run():
        with _patched_storage_home():
            session = _make_session()
            await _initialize_fake_session(session)
            agent_tool = next(tool for tool in session.tools if tool.name == "Agent")
            context = ToolContext(
                cwd=".",
                session_id=session.session_id,
                env=session.settings.env,
                choice_queue=asyncio.Queue(),
                tool_event_queue=asyncio.Queue(),
                agent_id=None,
                agent_depth=0,
                agent_manager=session._agent_manager,  # type: ignore[attr-defined]
            )
            result = await agent_tool.call({"prompt": "sleep 0"}, context)
            assert result.is_error is False
            assert "status: completed" in result.result_for_model
            assert "slept 0" in result.result_for_model

    asyncio.run(_run())


def test_switch_model_affects_new_agents():
    async def _run():
        with _patched_storage_home():
            settings = _make_settings()
            settings.models["fast"] = ApiConfig(provider="openai", model="fast-model")
            settings.models["smart"] = ApiConfig(provider="openai", model="smart-model")
            settings.default_model = "fast"
            session = CoreSession(
                cwd=".",
                settings=settings,
                tools=[SleepTool(), DangerousTool(), AskUserTool(), FileReadTool()],
            )
            with patch("crabcode_core.api.create_adapter", side_effect=lambda config: FakeAdapter(config)), patch(
                "crabcode_core.api.registry.create_adapter", side_effect=lambda config: FakeAdapter(config)
            ):
                await session.initialize()
                assert session.switch_model("smart") is True
                agent_id = await session.spawn_agent(prompt="sleep 0")
                snapshot = await session.wait_agent(agent_id, timeout_ms=2000)
            assert snapshot is not None
            assert snapshot.model == "smart-model"

    asyncio.run(_run())


def test_agent_send_input_continues_completed_agent():
    async def _run():
        with _patched_storage_home():
            session = _make_session()
            await _initialize_fake_session(session)
            agent_id = await session.spawn_agent(prompt="sleep 0")
            first = await session.wait_agent(agent_id, timeout_ms=2000)
            assert first is not None
            ok = await session.send_agent_input(agent_id, "ask")
            assert ok is True
            event = await _wait_for_queue_event(
                session._agent_event_queue,  # type: ignore[attr-defined]
                lambda item: isinstance(item, ChoiceRequestEvent) and item.agent_id == agent_id,
            )
            assert event is not None
            await session.respond_choice(
                ChoiceResponseEvent(
                    tool_use_id=event.tool_use_id,
                    selected=["a"],
                    agent_id=agent_id,
                )
            )
            second = await session.wait_agent(agent_id, timeout_ms=2000)
            assert second is not None
            assert "User selected: a" in second.final_result

    asyncio.run(_run())


def test_wait_any_returns_first_completed_agent():
    async def _run():
        with _patched_storage_home():
            session = _make_session()
            await _initialize_fake_session(session)
            slow = await session.spawn_agent(prompt="sleep 0.3")
            fast = await session.spawn_agent(prompt="sleep 0")
            snapshot = await session.wait_agent([slow, fast], timeout_ms=2000)
            assert snapshot is not None
            assert snapshot.agent_id == fast

    asyncio.run(_run())


def test_agent_transcript_persists_and_resumes():
    async def _run():
        with _patched_storage_home():
            session = _make_session()
            await _initialize_fake_session(session)
            agent_id = await session.spawn_agent(prompt="sleep 0")
            snapshot = await session.wait_agent(agent_id, timeout_ms=2000)
            assert snapshot is not None
            assert snapshot.transcript_path is not None
            assert Path(snapshot.transcript_path).exists()

            resumed = _make_session()
            await _initialize_fake_session(resumed)
            ok = await resumed.resume(session.session_id)
            assert ok is True
            restored = resumed.get_agent(agent_id)
            assert restored is not None
            assert restored.transcript_path == snapshot.transcript_path
            storage = SessionStorage(resumed.cwd, resumed.session_id)
            raw_messages = storage.load_agent_messages(agent_id)
            assert raw_messages

    asyncio.run(_run())
