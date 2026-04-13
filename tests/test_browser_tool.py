from __future__ import annotations

import asyncio
import contextlib
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

from crabcode_core.events import CoreSession
from crabcode_core.tools.browser import BrowserTool
from crabcode_core.types.config import ApiConfig, CrabCodeSettings
from crabcode_core.types.event import PermissionRequestEvent, PermissionResponseEvent, ToolResultEvent
from crabcode_core.types.tool import Tool, ToolContext, ToolResult


@contextlib.contextmanager
def _patched_storage_home():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        with patch("crabcode_core.session.storage.Path.home", return_value=home), patch(
            "crabcode_core.session.meta_db.Path.home", return_value=home
        ):
            yield


class _FakePage:
    def __init__(self) -> None:
        self.url = "about:blank"
        self.text = "initial page"
        self.title_text = "Fake Page"
        self.fields: dict[str, str] = {}
        self.default_timeout = 0
        self.default_navigation_timeout = 0

    def set_default_timeout(self, value: int) -> None:
        self.default_timeout = value

    def set_default_navigation_timeout(self, value: int) -> None:
        self.default_navigation_timeout = value

    async def goto(self, url: str, wait_until: str, timeout: int) -> None:
        self.url = url
        self.text = f"body for {url}"

    async def click(self, selector: str, timeout: int) -> None:
        self.url = f"{self.url}#clicked"

    async def fill(self, selector: str, text: str, timeout: int) -> None:
        self.fields[selector] = text

    async def press(self, selector: str, key: str, timeout: int) -> None:
        self.fields[f"press:{selector}"] = key

    async def wait_for_selector(self, selector: str, timeout: int) -> None:
        return None

    async def wait_for_load_state(self, wait_until: str, timeout: int) -> None:
        return None

    async def content(self) -> str:
        return f"<html><body>{self.text}</body></html>"

    async def eval_on_selector_all(self, selector: str, script: str) -> list[dict[str, str]]:
        return [{"text": "link", "href": f"{self.url}/link"}]

    async def evaluate(self, script: str) -> Any:
        if "document.title" in script:
            return {"title": self.title_text, "text": self.text, "url": self.url}
        return {"script": script, "url": self.url}

    async def text_content(self, selector: str) -> str:
        return self.text

    async def screenshot(self, path: str, full_page: bool) -> None:
        Path(path).write_bytes(b"png")

    async def close(self) -> None:
        return None


class _FakeContext:
    def __init__(self) -> None:
        self.pages: list[_FakePage] = []

    async def new_page(self) -> _FakePage:
        page = _FakePage()
        self.pages.append(page)
        return page

    async def close(self) -> None:
        return None


class _FakeBrowser:
    def __init__(self) -> None:
        self.contexts: list[_FakeContext] = []

    async def new_context(self, **kwargs: Any) -> _FakeContext:
        context = _FakeContext()
        self.contexts.append(context)
        return context

    async def close(self) -> None:
        return None


class _FakeBrowserType:
    def __init__(self) -> None:
        self.last_launch_kwargs: dict[str, Any] = {}

    async def launch(self, **kwargs: Any) -> _FakeBrowser:
        self.last_launch_kwargs = dict(kwargs)
        return _FakeBrowser()


class _FakePlaywrightRuntime:
    def __init__(self) -> None:
        self.chromium = _FakeBrowserType()

    async def stop(self) -> None:
        return None


class _FakePlaywrightStarter:
    def __init__(self) -> None:
        self.runtime = _FakePlaywrightRuntime()

    async def start(self) -> _FakePlaywrightRuntime:
        return self.runtime


async def _setup_browser_tool(tool: BrowserTool, *, tool_config: dict[str, Any] | None = None) -> ToolContext:
    context = ToolContext(cwd=".", tool_config=tool_config or {})
    await tool.setup(context)
    return context


def test_browser_tool_is_disabled_without_playwright():
    async def _run() -> None:
        tool = BrowserTool()
        with patch("builtins.__import__", side_effect=ImportError("missing playwright")):
            context = await _setup_browser_tool(tool)
        result = await tool.call({"action": "create_session"}, context)
        assert result.is_error is True
        assert "Playwright" in result.result_for_model

    asyncio.run(_run())


def test_browser_tool_session_lifecycle_and_actions():
    async def _run() -> None:
        tool = BrowserTool()
        context = ToolContext(cwd=".", tool_config={})
        await Tool.setup(tool, context)
        starter = _FakePlaywrightStarter()
        tool._playwright_module = lambda: starter  # type: ignore[attr-defined]
        tool._playwright_available = True  # type: ignore[attr-defined]

        created = await tool.call(
            {"action": "create_session", "url": "https://example.com", "headless": False},
            context,
        )
        assert created.is_error is False
        session_id = created.data["session_id"]
        tab_id = created.data["tab_id"]
        assert created.data["headless"] is False
        assert starter.runtime.chromium.last_launch_kwargs["headless"] is False

        navigated = await tool.call(
            {"action": "goto", "session_id": session_id, "url": "https://example.com/docs"},
            context,
        )
        assert navigated.is_error is False
        assert navigated.data["url"] == "https://example.com/docs"

        filled = await tool.call(
            {
                "action": "fill",
                "session_id": session_id,
                "tab_id": tab_id,
                "selector": "#q",
                "text": "crabcode",
            },
            context,
        )
        assert filled.is_error is False

        extracted = await tool.call(
            {"action": "extract", "session_id": session_id, "tab_id": tab_id, "return_format": "json"},
            context,
        )
        assert extracted.is_error is False
        assert extracted.data["data"]["url"] == "https://example.com/docs"

        evaluated = await tool.call(
            {"action": "evaluate", "session_id": session_id, "tab_id": tab_id, "script": "() => 42"},
            context,
        )
        assert evaluated.is_error is False
        assert evaluated.data["data"]["script"] == "() => 42"

        listed = await tool.call({"action": "list_tabs", "session_id": session_id}, context)
        assert listed.is_error is False
        assert len(listed.data["data"]["tabs"]) == 1

        closed = await tool.call({"action": "close_session", "session_id": session_id}, context)
        assert closed.is_error is False

        missing = await tool.call({"action": "list_tabs", "session_id": session_id}, context)
        assert missing.is_error is True
        assert "Unknown browser session" in missing.result_for_model

    asyncio.run(_run())


def test_browser_tool_uses_default_headless_when_not_overridden():
    async def _run() -> None:
        tool = BrowserTool()
        context = ToolContext(cwd=".", tool_config={"headless": True})
        await Tool.setup(tool, context)
        starter = _FakePlaywrightStarter()
        tool._playwright_module = lambda: starter  # type: ignore[attr-defined]
        tool._playwright_available = True  # type: ignore[attr-defined]

        created = await tool.call({"action": "create_session"}, context)
        assert created.is_error is False
        assert created.data["headless"] is True
        assert starter.runtime.chromium.last_launch_kwargs["headless"] is True

    asyncio.run(_run())


def test_browser_permission_keys_are_action_scoped():
    async def _run() -> None:
        tool = BrowserTool()
        context = ToolContext(cwd=".", tool_config={})
        await Tool.setup(tool, context)
        tool.is_enabled = True
        tool._enabled = True  # type: ignore[attr-defined]
        tool._playwright_available = True  # type: ignore[attr-defined]

        goto_perm = await tool.check_permissions({"action": "goto"}, context)
        fill_perm = await tool.check_permissions({"action": "fill"}, context)
        close_perm = await tool.check_permissions({"action": "close_session"}, context)

        assert goto_perm.behavior.value == "ask"
        assert goto_perm.permission_key == "Browser:goto"
        assert fill_perm.behavior.value == "ask"
        assert fill_perm.permission_key == "Browser:fill"
        assert close_perm.behavior.value == "allow"

    asyncio.run(_run())


class _ClosingTool(Tool):
    name = "CloseSpy"
    description = "Close spy."
    is_read_only = True
    is_concurrency_safe = True
    input_schema = {"type": "object", "properties": {}}

    def __init__(self) -> None:
        self.closed = False

    async def call(self, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        return ToolResult(result_for_model="ok")

    async def close(self) -> None:
        self.closed = True


def test_core_session_close_calls_tool_close():
    async def _run() -> None:
        with _patched_storage_home():
            tool = _ClosingTool()
            session = CoreSession(
                cwd=".",
                settings=CrabCodeSettings(api=ApiConfig(provider="openai", model="fake")),
                tools=[tool],
            )
            await session.close()
            assert tool.closed is True

    asyncio.run(_run())


class _BrowserPermissionAdapter:
    def __init__(self, config: ApiConfig):
        self.config = config

    async def stream_message(self, messages, system, tools, config):
        from crabcode_core.api.base import StreamChunk

        prompt = messages[-1].text_content
        yield StreamChunk(type="message_start", usage={"input_tokens": 1})
        if prompt == "browser-goto":
            yield StreamChunk(type="tool_use_start", tool_use_id="tool-1", tool_name="Browser")
            yield StreamChunk(
                type="tool_use_delta",
                tool_use_id="tool-1",
                tool_input_json='{"action":"goto","session_id":"s1","url":"https://example.com"}',
            )
            yield StreamChunk(
                type="tool_use_end",
                tool_use_id="tool-1",
                tool_name="Browser",
                tool_input_json='{"action":"goto","session_id":"s1","url":"https://example.com"}',
            )
        elif prompt == "browser-fill":
            yield StreamChunk(type="tool_use_start", tool_use_id="tool-2", tool_name="Browser")
            yield StreamChunk(
                type="tool_use_delta",
                tool_use_id="tool-2",
                tool_input_json='{"action":"fill","session_id":"s1","selector":"#q","text":"hello"}',
            )
            yield StreamChunk(
                type="tool_use_end",
                tool_use_id="tool-2",
                tool_name="Browser",
                tool_input_json='{"action":"fill","session_id":"s1","selector":"#q","text":"hello"}',
            )
        yield StreamChunk(type="message_stop", usage={"output_tokens": 1})

    async def count_tokens(self, messages, system) -> int:
        return 1


class _PermissionOnlyBrowser(BrowserTool):
    async def setup(self, context: ToolContext) -> None:
        await Tool.setup(self, context)
        self.is_enabled = True
        self._enabled = True  # type: ignore[attr-defined]
        self._playwright_available = True  # type: ignore[attr-defined]
        self._sessions["s1"] = _BrowserSessionStub()

    async def call(self, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        return ToolResult(result_for_model=f"browser ok: {tool_input['action']}")


class _BrowserSessionStub:
    trusted_origins: set[str] = set()


def test_browser_always_allow_is_action_scoped():
    async def _run() -> None:
        with _patched_storage_home():
            settings = CrabCodeSettings(api=ApiConfig(provider="openai", model="fake"))
            session = CoreSession(cwd=".", settings=settings, tools=[_PermissionOnlyBrowser()])
            adapter = _BrowserPermissionAdapter(settings.api)
            with patch("crabcode_core.api.create_adapter", return_value=adapter), patch(
                "crabcode_core.api.registry.create_adapter", return_value=adapter
            ):
                first_events = []
                async for event in session.send_message("browser-goto"):
                    first_events.append(event)
                    if isinstance(event, PermissionRequestEvent):
                        await session.respond_permission(
                            PermissionResponseEvent(
                                tool_use_id=event.tool_use_id,
                                allowed=True,
                                always_allow=True,
                            )
                        )

                second_events = [event async for event in session.send_message("browser-goto")]
                third_events = []
                async for event in session.send_message("browser-fill"):
                    third_events.append(event)
                    if isinstance(event, PermissionRequestEvent):
                        await session.respond_permission(
                            PermissionResponseEvent(tool_use_id=event.tool_use_id, allowed=True)
                        )

            assert any(isinstance(event, PermissionRequestEvent) for event in first_events)
            assert not any(isinstance(event, PermissionRequestEvent) for event in second_events)
            fill_prompt = next(event for event in third_events if isinstance(event, PermissionRequestEvent))
            assert fill_prompt.permission_key == "Browser:fill"
            fill_result = next(event for event in third_events if isinstance(event, ToolResultEvent))
            assert fill_result.result == "browser ok: fill"

    asyncio.run(_run())
