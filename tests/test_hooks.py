from __future__ import annotations

import asyncio
import contextlib
import tempfile
from pathlib import Path
from typing import Any, AsyncGenerator
from unittest.mock import patch

from crabcode_core.api.base import APIAdapter, ModelConfig, StreamChunk
from crabcode_core.events import CoreSession
from crabcode_core.types.config import ApiConfig, CrabCodeSettings
from crabcode_core.types.event import ToolResultEvent
from crabcode_core.types.message import Message, MessageRole
from crabcode_core.types.tool import Tool, ToolContext, ToolResult


class DangerTool(Tool):
    name = "Danger"
    description = "Danger test tool."
    is_read_only = True
    is_concurrency_safe = True
    input_schema = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
    }

    def __init__(self) -> None:
        self.calls = 0

    async def call(self, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        self.calls += 1
        return ToolResult(result_for_model=f"danger {tool_input['value']}")


class HookAdapter(APIAdapter):
    def __init__(self, config: ApiConfig):
        self.config = config
        self.requests: list[list[Message]] = []

    async def stream_message(
        self,
        messages: list[Message],
        system: list[str],
        tools: list[dict[str, Any]],
        config: ModelConfig,
    ) -> AsyncGenerator[StreamChunk, None]:
        self.requests.append(list(messages))
        prompt = messages[-1].text_content
        yield StreamChunk(type="message_start", usage={"input_tokens": 1})
        if prompt.startswith("danger"):
            yield StreamChunk(type="tool_use_start", tool_use_id="tool-1", tool_name="Danger")
            yield StreamChunk(type="tool_use_delta", tool_use_id="tool-1", tool_input_json='{"value":"x"}')
            yield StreamChunk(
                type="tool_use_end",
                tool_use_id="tool-1",
                tool_name="Danger",
                tool_input_json='{"value":"x"}',
            )
        else:
            yield StreamChunk(type="text", text="done")
        yield StreamChunk(type="message_stop", usage={"output_tokens": 1})

    async def count_tokens(self, messages: list[Message], system: list[str]) -> int:
        return 1


@contextlib.contextmanager
def _patched_storage_home():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        with patch("crabcode_core.session.storage.Path.home", return_value=home), patch(
            "crabcode_core.session.meta_db.Path.home", return_value=home
        ):
            yield


def _session_with_hooks(hooks: dict[str, list[dict[str, Any]]]) -> tuple[CoreSession, DangerTool]:
    tool = DangerTool()
    settings = CrabCodeSettings(
        api=ApiConfig(provider="openai", model="fake"),
        hooks=hooks,
    )
    return CoreSession(cwd=".", settings=settings, tools=[tool]), tool


def test_pre_tool_hook_can_block_call():
    async def _run():
        with _patched_storage_home():
            session, tool = _session_with_hooks({"pre_tool_call": [{"command": "exit 7"}]})
            adapter = HookAdapter(session.settings.api)
            with patch("crabcode_core.api.create_adapter", return_value=adapter), patch(
                "crabcode_core.api.registry.create_adapter", return_value=adapter
            ):
                events = [event async for event in session.send_message("danger")]
            tool_results = [event for event in events if isinstance(event, ToolResultEvent)]
            assert tool_results
            assert tool_results[0].is_error is True
            assert "Hook blocked tool call" in tool_results[0].result
            assert tool.calls == 0

    asyncio.run(_run())


def test_post_tool_hook_feedback_is_injected():
    async def _run():
        with _patched_storage_home():
            session, tool = _session_with_hooks(
                {"post_tool_call": [{"matcher": "Danger", "command": "echo post-ok"}]}
            )
            adapter = HookAdapter(session.settings.api)
            with patch("crabcode_core.api.create_adapter", return_value=adapter), patch(
                "crabcode_core.api.registry.create_adapter", return_value=adapter
            ):
                _ = [event async for event in session.send_message("danger")]
            assert tool.calls == 1
            assert any(
                msg.role == MessageRole.USER
                and "<post-tool-call-hook>" in msg.text_content
                and "post-ok" in msg.text_content
                for msg in session.messages
            )

    asyncio.run(_run())


def test_user_prompt_submit_hook_feedback_is_injected():
    async def _run():
        with _patched_storage_home():
            session, _ = _session_with_hooks(
                {"user_prompt_submit": [{"command": "echo submit-ok"}]}
            )
            adapter = HookAdapter(session.settings.api)
            with patch("crabcode_core.api.create_adapter", return_value=adapter), patch(
                "crabcode_core.api.registry.create_adapter", return_value=adapter
            ):
                _ = [event async for event in session.send_message("hello")]
            first_user = next(msg for msg in session.messages if msg.role == MessageRole.USER)
            assert "<user-prompt-submit-hook>" in first_user.text_content
            assert "submit-ok" in first_user.text_content

    asyncio.run(_run())


def test_claude_style_pre_tool_use_hook_is_supported():
    async def _run():
        with _patched_storage_home():
            session, tool = _session_with_hooks(
                {
                    "PreToolUse": [
                        {
                            "matcher": "Danger",
                            "hooks": [{"type": "command", "command": "exit 3"}],
                        }
                    ]
                }
            )
            adapter = HookAdapter(session.settings.api)
            with patch("crabcode_core.api.create_adapter", return_value=adapter), patch(
                "crabcode_core.api.registry.create_adapter", return_value=adapter
            ):
                events = [event async for event in session.send_message("danger")]
            tool_results = [event for event in events if isinstance(event, ToolResultEvent)]
            assert tool_results
            assert tool_results[0].is_error is True
            assert "Hook blocked tool call" in tool_results[0].result
            assert tool.calls == 0

    asyncio.run(_run())
