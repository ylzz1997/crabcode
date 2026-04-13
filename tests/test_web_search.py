from __future__ import annotations

import asyncio
import contextlib
import tempfile
from pathlib import Path
from typing import Any, AsyncGenerator
from unittest.mock import AsyncMock, patch

from crabcode_core.api.base import APIAdapter, ModelConfig, StreamChunk
from crabcode_core.events import CoreSession
from crabcode_core.tools.web_search import WebSearchTool, _DuckDuckGoHTMLParser
from crabcode_core.types.config import ApiConfig, CrabCodeSettings
from crabcode_core.types.event import PermissionRequestEvent, PermissionResponseEvent, ToolResultEvent
from crabcode_core.types.message import Message
from crabcode_core.types.tool import PermissionBehavior, PermissionResult, Tool, ToolContext, ToolResult


@contextlib.contextmanager
def _patched_storage_home():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        with patch("crabcode_core.session.storage.Path.home", return_value=home), patch(
            "crabcode_core.session.meta_db.Path.home", return_value=home
        ):
            yield


async def _setup_tool(
    tool: WebSearchTool,
    *,
    tool_config: dict[str, Any] | None = None,
    env: dict[str, str] | None = None,
) -> ToolContext:
    context = ToolContext(
        cwd=".",
        env=env or {},
        tool_config=tool_config or {},
    )
    await tool.setup(context)
    return context


class CaptureToolsAdapter(APIAdapter):
    def __init__(self, config: ApiConfig):
        self.config = config
        self.last_tools: list[dict[str, Any]] = []

    async def stream_message(
        self,
        messages: list[Message],
        system: list[str],
        tools: list[dict[str, Any]],
        config: ModelConfig,
    ) -> AsyncGenerator[StreamChunk, None]:
        self.last_tools = tools
        yield StreamChunk(type="message_start", usage={"input_tokens": 1})
        yield StreamChunk(type="text", text="done")
        yield StreamChunk(type="message_stop", usage={"output_tokens": 1})

    async def count_tokens(self, messages: list[Message], system: list[str]) -> int:
        return 1


class PermissionAdapter(APIAdapter):
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
        if prompt == "web":
            yield StreamChunk(type="tool_use_start", tool_use_id="tool-1", tool_name="WebSearch")
            yield StreamChunk(
                type="tool_use_delta",
                tool_use_id="tool-1",
                tool_input_json='{"query":"crabcode"}',
            )
            yield StreamChunk(
                type="tool_use_end",
                tool_use_id="tool-1",
                tool_name="WebSearch",
                tool_input_json='{"query":"crabcode"}',
            )
        elif prompt == "peek":
            yield StreamChunk(type="tool_use_start", tool_use_id="tool-2", tool_name="Peek")
            yield StreamChunk(
                type="tool_use_delta",
                tool_use_id="tool-2",
                tool_input_json='{"value":"x"}',
            )
            yield StreamChunk(
                type="tool_use_end",
                tool_use_id="tool-2",
                tool_name="Peek",
                tool_input_json='{"value":"x"}',
            )
        elif prompt == "rewrite":
            yield StreamChunk(type="tool_use_start", tool_use_id="tool-3", tool_name="Rewrite")
            yield StreamChunk(
                type="tool_use_delta",
                tool_use_id="tool-3",
                tool_input_json='{"value":"original"}',
            )
            yield StreamChunk(
                type="tool_use_end",
                tool_use_id="tool-3",
                tool_name="Rewrite",
                tool_input_json='{"value":"original"}',
            )
        else:
            yield StreamChunk(type="text", text="done")
        yield StreamChunk(type="message_stop", usage={"output_tokens": 1})

    async def count_tokens(self, messages: list[Message], system: list[str]) -> int:
        return 1


class LocalWebSearchTool(WebSearchTool):
    async def setup(self, context: ToolContext) -> None:
        await Tool.setup(self, context)
        self._tool_config = dict(context.tool_config)
        self.is_enabled = True

    async def call(self, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        return ToolResult(result_for_model=f"web ok: {tool_input['query']}")


class PeekTool(Tool):
    name = "Peek"
    description = "Read-only test tool."
    is_read_only = True
    is_concurrency_safe = True
    input_schema = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
    }

    async def call(self, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        return ToolResult(result_for_model=f"peek ok: {tool_input['value']}")


class RewriteTool(Tool):
    name = "Rewrite"
    description = "Permission rewrite test tool."
    is_read_only = True
    is_concurrency_safe = True
    input_schema = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
    }

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> PermissionResult:
        return PermissionResult(
            behavior=PermissionBehavior.ASK,
            updated_input={"value": "rewritten"},
        )

    async def call(self, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        return ToolResult(result_for_model=f"rewrite ok: {tool_input['value']}")


def test_web_search_tavily_success():
    async def _run() -> None:
        tool = WebSearchTool()
        with patch.object(WebSearchTool, "_probe_endpoint", AsyncMock(return_value=True)):
            context = await _setup_tool(
                tool,
                tool_config={"provider": "tavily"},
                env={"TAVILY_API_KEY": "test-key"},
            )
        with patch.object(
            tool,
            "_search_tavily",
            AsyncMock(
                return_value=[
                    {
                        "title": "CrabCode",
                        "url": "https://example.com/crabcode",
                        "snippet": "A current result.",
                    }
                ]
            ),
        ):
            result = await tool.call({"query": "crabcode"}, context)
        assert result.is_error is False
        assert result.data["provider"] == "tavily"
        assert result.data["results"][0]["url"] == "https://example.com/crabcode"
        assert "[1] CrabCode" in result.result_for_model

    asyncio.run(_run())


def test_web_search_ddg_success():
    async def _run() -> None:
        tool = WebSearchTool()
        with patch.object(WebSearchTool, "_probe_endpoint", AsyncMock(return_value=True)):
            context = await _setup_tool(tool, tool_config={"provider": "ddg"})
        with patch.object(
            tool,
            "_search_ddg",
            AsyncMock(
                return_value=[
                    {
                        "title": "Duck Result",
                        "url": "https://example.com/ddg",
                        "snippet": "From duckduckgo.",
                    }
                ]
            ),
        ):
            result = await tool.call({"query": "crabcode"}, context)
        assert result.is_error is False
        assert result.data["provider"] == "ddg"
        assert "https://example.com/ddg" in result.result_for_model

    asyncio.run(_run())


def test_web_search_auto_falls_back_to_ddg():
    async def _run() -> None:
        tool = WebSearchTool()
        with patch.object(WebSearchTool, "_probe_endpoint", AsyncMock(return_value=True)):
            context = await _setup_tool(
                tool,
                tool_config={"provider": "auto"},
                env={"TAVILY_API_KEY": "test-key"},
            )
        with patch.object(tool, "_search_tavily", AsyncMock(side_effect=RuntimeError("boom"))), patch.object(
            tool,
            "_search_ddg",
            AsyncMock(
                return_value=[
                    {
                        "title": "Fallback",
                        "url": "https://example.com/fallback",
                        "snippet": "Fallback result.",
                    }
                ]
            ),
        ):
            result = await tool.call({"query": "crabcode"}, context)
        assert result.is_error is False
        assert result.data["provider"] == "ddg"
        assert result.data["results"][0]["title"] == "Fallback"

    asyncio.run(_run())


def test_web_search_validation_and_clamping():
    async def _run() -> None:
        tool = WebSearchTool()
        assert await tool.validate_input({"query": ""}) == "query is required"
        assert await tool.validate_input({"query": "ok", "num_results": "bad"}) == "num_results must be an integer"

        with patch.object(WebSearchTool, "_probe_endpoint", AsyncMock(return_value=True)):
            context = await _setup_tool(tool, tool_config={"provider": "ddg"})

        ddg_mock = AsyncMock(return_value=[])
        with patch.object(tool, "_search_ddg", ddg_mock):
            await tool.call({"query": "ok", "num_results": 99}, context)
        ddg_mock.assert_awaited_once_with("ok", 10)

    asyncio.run(_run())


def test_ddg_parser_resolves_redirect_urls():
    html = """
    <html>
      <body>
        <article>
          <a class="result__a" href="/l/?uddg=https%3A%2F%2Fgo.dev%2F">The Go Programming Language</a>
          <a class="result__snippet">Build simple, secure, scalable systems.</a>
        </article>
      </body>
    </html>
    """
    parser = _DuckDuckGoHTMLParser()
    parser.feed(html)
    parser.close()
    assert parser.results == [
        {
            "title": "The Go Programming Language",
            "url": "https://go.dev/",
            "snippet": "Build simple, secure, scalable systems.",
        }
    ]


def test_web_search_is_omitted_when_offline():
    async def _run() -> None:
        with tempfile.TemporaryDirectory() as tmp, _patched_storage_home():
            settings = CrabCodeSettings(api=ApiConfig(provider="openai", model="fake"))
            session = CoreSession(cwd=tmp, settings=settings, tools=[])
            adapter = CaptureToolsAdapter(settings.api)
            with patch("crabcode_core.api.create_adapter", return_value=adapter), patch(
                "crabcode_core.api.registry.create_adapter", return_value=adapter
            ), patch.object(WebSearchTool, "_probe_endpoint", AsyncMock(return_value=False)):
                events = [event async for event in session.send_message("hello")]
            assert any(getattr(event, "text", "") == "done" for event in events)
            assert "WebSearch" not in [tool["name"] for tool in adapter.last_tools]
            assert any(tool.name == "WebSearch" and tool.is_enabled is False for tool in session.tools)

    asyncio.run(_run())


def test_web_search_is_exposed_when_online():
    async def _run() -> None:
        with tempfile.TemporaryDirectory() as tmp, _patched_storage_home():
            settings = CrabCodeSettings(api=ApiConfig(provider="openai", model="fake"))
            session = CoreSession(cwd=tmp, settings=settings, tools=[])
            adapter = CaptureToolsAdapter(settings.api)
            with patch("crabcode_core.api.create_adapter", return_value=adapter), patch(
                "crabcode_core.api.registry.create_adapter", return_value=adapter
            ), patch.object(WebSearchTool, "_probe_endpoint", AsyncMock(return_value=True)):
                _ = [event async for event in session.send_message("hello")]
            assert "WebSearch" in [tool["name"] for tool in adapter.last_tools]
            assert any(tool.name == "WebSearch" and tool.is_enabled is True for tool in session.tools)

    asyncio.run(_run())


def test_web_search_always_allow_stops_future_prompts():
    async def _run() -> None:
        with _patched_storage_home():
            settings = CrabCodeSettings(api=ApiConfig(provider="openai", model="fake"))
            session = CoreSession(cwd=".", settings=settings, tools=[LocalWebSearchTool()])
            adapter = PermissionAdapter(settings.api)
            with patch("crabcode_core.api.create_adapter", return_value=adapter), patch(
                "crabcode_core.api.registry.create_adapter", return_value=adapter
            ):
                first_events = []
                async for event in session.send_message("web"):
                    first_events.append(event)
                    if isinstance(event, PermissionRequestEvent):
                        await session.respond_permission(
                            PermissionResponseEvent(
                                tool_use_id=event.tool_use_id,
                                allowed=True,
                                always_allow=True,
                            )
                        )

                second_events = [event async for event in session.send_message("web")]

            assert any(isinstance(event, PermissionRequestEvent) for event in first_events)
            assert not any(isinstance(event, PermissionRequestEvent) for event in second_events)
            result = next(event for event in second_events if isinstance(event, ToolResultEvent))
            assert result.result == "web ok: crabcode"

    asyncio.run(_run())


def test_web_search_requires_permission_even_as_read_only():
    async def _run() -> None:
        with _patched_storage_home():
            settings = CrabCodeSettings(api=ApiConfig(provider="openai", model="fake"))
            session = CoreSession(cwd=".", settings=settings, tools=[LocalWebSearchTool()])
            adapter = PermissionAdapter(settings.api)
            with patch("crabcode_core.api.create_adapter", return_value=adapter), patch(
                "crabcode_core.api.registry.create_adapter", return_value=adapter
            ):
                stream = session.send_message("web")
                events = []
                async for event in stream:
                    events.append(event)
                    if isinstance(event, PermissionRequestEvent):
                        await session.respond_permission(
                            PermissionResponseEvent(
                                tool_use_id=event.tool_use_id,
                                allowed=True,
                            )
                        )
                prompt = next(event for event in events if isinstance(event, PermissionRequestEvent))
                result = next(event for event in events if isinstance(event, ToolResultEvent))
                assert prompt.tool_name == "WebSearch"
                assert result.result == "web ok: crabcode"

    asyncio.run(_run())


def test_web_search_permission_denial_returns_error():
    async def _run() -> None:
        with _patched_storage_home():
            settings = CrabCodeSettings(api=ApiConfig(provider="openai", model="fake"))
            session = CoreSession(cwd=".", settings=settings, tools=[LocalWebSearchTool()])
            adapter = PermissionAdapter(settings.api)
            with patch("crabcode_core.api.create_adapter", return_value=adapter), patch(
                "crabcode_core.api.registry.create_adapter", return_value=adapter
            ):
                stream = session.send_message("web")
                events = []
                async for event in stream:
                    events.append(event)
                    if isinstance(event, PermissionRequestEvent):
                        await session.respond_permission(
                            PermissionResponseEvent(
                                tool_use_id=event.tool_use_id,
                                allowed=False,
                            )
                        )
                result = next(event for event in events if isinstance(event, ToolResultEvent))
                assert result.is_error is True
                assert result.result == "Permission denied by user."

    asyncio.run(_run())


def test_other_read_only_tools_keep_current_permission_behavior():
    async def _run() -> None:
        with _patched_storage_home():
            settings = CrabCodeSettings(api=ApiConfig(provider="openai", model="fake"))
            session = CoreSession(cwd=".", settings=settings, tools=[PeekTool()])
            adapter = PermissionAdapter(settings.api)
            with patch("crabcode_core.api.create_adapter", return_value=adapter), patch(
                "crabcode_core.api.registry.create_adapter", return_value=adapter
            ):
                events = [event async for event in session.send_message("peek")]
            assert not any(isinstance(event, PermissionRequestEvent) for event in events)
            result = next(event for event in events if isinstance(event, ToolResultEvent))
            assert result.result == "peek ok: x"

    asyncio.run(_run())


def test_tool_permission_updated_input_is_used_after_approval():
    async def _run() -> None:
        with _patched_storage_home():
            settings = CrabCodeSettings(api=ApiConfig(provider="openai", model="fake"))
            session = CoreSession(cwd=".", settings=settings, tools=[RewriteTool()])
            adapter = PermissionAdapter(settings.api)
            with patch("crabcode_core.api.create_adapter", return_value=adapter), patch(
                "crabcode_core.api.registry.create_adapter", return_value=adapter
            ):
                stream = session.send_message("rewrite")
                events = []
                async for event in stream:
                    events.append(event)
                    if isinstance(event, PermissionRequestEvent):
                        assert event.tool_input == {"value": "rewritten"}
                        await session.respond_permission(
                            PermissionResponseEvent(
                                tool_use_id=event.tool_use_id,
                                allowed=True,
                            )
                        )
                result = next(event for event in events if isinstance(event, ToolResultEvent))
                assert result.result == "rewrite ok: rewritten"

    asyncio.run(_run())
