"""Round statistics survive transport/reload without counting streaming chunks."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from crabcode_core.events import CoreSession
from crabcode_core.session.storage import SessionStorage
from crabcode_core.turn_details import TurnDetailsTracker
from crabcode_core.types.config import ApiConfig, CrabCodeSettings
from crabcode_core.types.event import (
    CompactEvent, StreamModeEvent, StreamRetryEvent, StreamTextEvent,
    ThinkingEvent, ToolUseEvent, TurnCompleteEvent,
)
from crabcode_core.types.message import create_assistant_message, message_from_entry
from crabcode_gateway.routes.event import _send_session_history
from crabcode_gateway.schemas import core_event_to_payload
from crabcode_gateway.turn_details import with_turn_details


def test_tracker_deduplicates_tools_and_thinking_chunks_and_ignores_agents():
    tracker = TurnDetailsTracker()
    for event in [
        StreamModeEvent(mode="requesting"), ThinkingEvent(text="a"), ThinkingEvent(text="b"),
        ToolUseEvent(tool_name="Read", tool_input={}, tool_use_id="t"),
        ToolUseEvent(tool_name="Read", tool_input={}, tool_use_id="t"),
        ToolUseEvent(tool_name="Read", tool_input={}, tool_use_id="child", agent_id="child"),
        StreamRetryEvent(message="retry", error="e", retry_count=1, max_retries=2, delay_seconds=0),
        StreamModeEvent(mode="requesting"), ThinkingEvent(text="c"),
        CompactEvent(summary="s"), StreamTextEvent(text="done"),
    ]:
        tracker.observe(event)
    details = tracker.finish(TurnCompleteEvent(), session_id="s", model="model", provider="provider")
    assert details["tool_call_count"] == 1
    assert details["thinking_count"] == 2
    assert details["request_count"] == 2
    assert details["retry_count"] == details["compact_count"] == 1
    assert details["ended_at"] >= details["started_at"]


def test_core_saves_each_round_and_replays_details_over_websocket(tmp_path, monkeypatch):
    config = tmp_path / "config"
    monkeypatch.setattr("crabcode_core.session.storage.get_config_home", lambda: config)
    monkeypatch.setattr("crabcode_core.session.meta_db.get_config_home", lambda: config)
    session = CoreSession(cwd=str(tmp_path), settings=CrabCodeSettings(api=ApiConfig(model="example", provider="openai")))
    session._initialized = True
    session._api_adapter = SimpleNamespace()
    monkeypatch.setattr(session, "ensure_peer_runtime", AsyncMock())
    monkeypatch.setattr(session, "reload_compaction_settings", lambda: None)
    monkeypatch.setattr(session, "reload_prompt_settings", lambda: None)
    monkeypatch.setattr(session, "_maybe_generate_title", lambda: None)
    monkeypatch.setattr("crabcode_core.prompts.context.get_system_context", lambda cwd: {})
    monkeypatch.setattr("crabcode_core.prompts.context.get_user_context", lambda cwd: {})

    async def query(params):
        yield StreamModeEvent(mode="requesting")
        yield ThinkingEvent(text="one")
        yield ThinkingEvent(text="two")
        params.messages.append(create_assistant_message("done"))
        yield StreamTextEvent(text="done")
        yield TurnCompleteEvent(usage={"input_tokens": 120, "output_tokens": 10})

    monkeypatch.setattr("crabcode_core.query.loop.query_loop", query)

    async def run():
        first = [event async for event in session.send_message("first")][-1]
        second = [event async for event in session.send_message("second")][-1]
        assert first.assistant_message_uuid != second.assistant_message_uuid
        assert first.turn_details["thinking_count"] == second.turn_details["thinking_count"] == 1
        assert core_event_to_payload(second).turn_details.usage["input_tokens"] == 120
        restored = [message_from_entry(entry) for entry in SessionStorage(str(tmp_path), session.session_id).load_messages()]
        assistants = [message for message in restored if message.role.value == "assistant"]
        assert assistants[0].turn_details == first.turn_details
        assert assistants[1].turn_details == second.turn_details
        ws = SimpleNamespace(send_text=AsyncMock())
        await _send_session_history(ws, SimpleNamespace(messages=restored, session_id=session.session_id))
        import json
        history = json.loads(ws.send_text.call_args_list[0].args[0])
        assert history["messages"][-1]["turn_details"] == second.turn_details

    asyncio.run(run())


def test_legacy_history_uses_whole_round_and_keeps_missing_times_unknown():
    messages = [
        {"role": "user", "timestamp": "2026-10-10T01:00:00Z", "content": "first"},
        {"role": "assistant", "uuid": "a", "content": [
            {"type": "thinking", "thinking": "reason"}, {"type": "tool_use", "id": "t"},
        ], "usage": {"input_tokens": 20}},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t"}]},
        {"role": "user", "origin": "user-steering", "content": "guidance"},
        {"role": "assistant", "uuid": "b", "content": "done", "usage": {"input_tokens": 30}},
        {"role": "user", "timestamp": "2026-10-10T02:00:00Z", "content": "next"},
        {"role": "assistant", "uuid": "c", "content": "done"},
    ]
    result = with_turn_details(messages, "s")
    assert "turn_details" not in messages[4]
    first = result[4]["turn_details"]
    assert first["request_count"] == 2
    assert first["thinking_count"] == first["tool_call_count"] == 1
    assert first["usage"]["input_tokens"] == 50
    assert first.get("ended_at") is None
    assert first["source"] == "history"
    assert result[6]["turn_details"]["tool_call_count"] == 0


def test_fork_rebinds_session_id_without_mutating_recorded_source():
    messages = [{"role": "assistant", "content": "done", "turn_details": {"session_id": "old", "source": "recorded"}}]
    assert with_turn_details(messages, "fork")[0]["turn_details"]["session_id"] == "fork"
    assert messages[0]["turn_details"]["session_id"] == "old"
