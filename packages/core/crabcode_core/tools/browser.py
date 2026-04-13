"""BrowserTool — headless browser automation via Playwright."""

from __future__ import annotations

import json
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from crabcode_core.logging_utils import get_logger
from crabcode_core.types.tool import PermissionBehavior, PermissionResult, Tool, ToolContext, ToolResult

logger = get_logger(__name__)

_ACTIONS = {
    "create_session",
    "goto",
    "click",
    "fill",
    "press",
    "wait_for",
    "extract",
    "screenshot",
    "evaluate",
    "list_tabs",
    "new_tab",
    "switch_tab",
    "close_tab",
    "close_session",
}
_WAIT_UNTIL = {"load", "domcontentloaded", "networkidle"}
_RETURN_FORMATS = {"text", "html", "links", "json"}
_SENSITIVE_ACTIONS = {"fill", "press", "evaluate"}
_SESSION_ACTIONS = {"create_session", "goto"}


@dataclass
class _BrowserSession:
    session_id: str
    playwright: Any
    browser: Any
    context: Any
    headless: bool
    tabs: dict[str, Any] = field(default_factory=dict)
    active_tab_id: str | None = None
    user_data_dir: str | None = None
    trusted_origins: set[str] = field(default_factory=set)


def _origin_from_url(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


class BrowserTool(Tool):
    name = "Browser"
    description = "Use a Chromium browser to open pages, interact with them, extract content, and take screenshots. Headless is the default."
    is_read_only = False
    is_concurrency_safe = False
    uses_tool_permission_policy = True
    input_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": sorted(_ACTIONS),
                "description": "Browser action to perform.",
            },
            "session_id": {
                "type": "string",
                "description": "Existing browser session ID. Omit for create_session.",
            },
            "headless": {
                "type": "boolean",
                "description": "Optional create_session override. true runs headless, false opens a visible browser window. Defaults to tool_settings.Browser.headless.",
            },
            "url": {"type": "string", "description": "Navigation target URL."},
            "selector": {"type": "string", "description": "CSS selector for the target element."},
            "text": {"type": "string", "description": "Text to input or key to press."},
            "script": {"type": "string", "description": "JavaScript snippet to evaluate in the page."},
            "timeout_seconds": {"type": "number", "description": "Optional timeout override in seconds."},
            "wait_until": {
                "type": "string",
                "enum": sorted(_WAIT_UNTIL),
                "description": "Page readiness target for navigation.",
            },
            "return_format": {
                "type": "string",
                "enum": sorted(_RETURN_FORMATS),
                "description": "Format for extract/evaluate output.",
            },
            "path": {"type": "string", "description": "Output path for screenshots."},
            "tab_id": {"type": "string", "description": "Target tab ID."},
            "options": {
                "type": "object",
                "description": "Additional action-specific options.",
                "additionalProperties": True,
            },
        },
        "required": ["action"],
    }

    def __init__(self) -> None:
        self._tool_config: dict[str, Any] = {}
        self._sessions: dict[str, _BrowserSession] = {}
        self._playwright_module: Any | None = None
        self._playwright_available = False
        self._dependency_error: str | None = None
        self._enabled = True
        self._headless = True
        self._default_timeout_ms = 15_000
        self._max_sessions = 3
        self._default_browser = "chromium"
        self._launch_options: dict[str, Any] = {}
        self._context_options: dict[str, Any] = {}
        self._storage_dir: str | None = None
        self._block_downloads = True
        self._allowed_domains: set[str] = set()
        self._blocked_domains: set[str] = set()

    async def setup(self, context: ToolContext) -> None:
        await super().setup(context)
        self._tool_config = dict(context.tool_config)
        self._enabled = bool(self._tool_config.get("enabled", True))
        self._default_browser = str(self._tool_config.get("default_browser", "chromium")).lower()
        self._headless = bool(self._tool_config.get("headless", True))
        self._default_timeout_ms = max(1, int(float(self._tool_config.get("default_timeout_seconds", 15)) * 1000))
        self._max_sessions = max(1, int(self._tool_config.get("max_sessions", 3)))
        self._launch_options = dict(self._tool_config.get("launch_options", {}))
        self._context_options = dict(self._tool_config.get("context_options", {}))
        self._storage_dir = self._tool_config.get("storage_dir")
        self._block_downloads = bool(self._tool_config.get("block_downloads", True))
        self._allowed_domains = {str(v) for v in self._tool_config.get("allowed_domains", []) if str(v)}
        self._blocked_domains = {str(v) for v in self._tool_config.get("blocked_domains", []) if str(v)}

        if not self._enabled:
            self.is_enabled = False
            return

        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            self._dependency_error = (
                "Browser is unavailable because Playwright is not installed. "
                "Install the optional browser extra and run 'playwright install chromium'."
            )
            self.is_enabled = False
            logger.info("Disabling Browser tool: %s", exc)
            return

        self._playwright_module = async_playwright
        self._playwright_available = True

    async def close(self) -> None:
        for session_id in list(self._sessions):
            await self._close_session(session_id)

    async def get_prompt(self, **kwargs: Any) -> str:
        return (
            "Use a persistent Chromium browser session to navigate pages, interact with DOM elements, "
            "extract content, evaluate page-side JavaScript, and take screenshots. "
            "Prefer WebSearch for discovering URLs or searching the public web; use Browser when you need "
            "to open a specific page, click, fill forms, inspect DOM state, or capture a screenshot. "
            "Browser sessions run headless by default, but create_session can override that with headless=false when needed. "
            "Create a session once, reuse the returned session_id across actions, and close sessions when done."
        )

    async def validate_input(self, tool_input: dict[str, Any]) -> str | None:
        action = str(tool_input.get("action", "")).strip()
        if action not in _ACTIONS:
            return f"action must be one of: {', '.join(sorted(_ACTIONS))}"

        if action != "create_session" and not str(tool_input.get("session_id", "")).strip():
            return "session_id is required for this action"

        if action in {"goto", "create_session"} and tool_input.get("url"):
            url = str(tool_input["url"]).strip()
            if url and urlparse(url).scheme not in {"http", "https"}:
                return "url must be http or https"

        wait_until = tool_input.get("wait_until")
        if wait_until is not None and str(wait_until) not in _WAIT_UNTIL:
            return f"wait_until must be one of: {', '.join(sorted(_WAIT_UNTIL))}"

        return_format = tool_input.get("return_format")
        if return_format is not None and str(return_format) not in _RETURN_FORMATS:
            return f"return_format must be one of: {', '.join(sorted(_RETURN_FORMATS))}"

        if "headless" in tool_input and not isinstance(tool_input["headless"], bool):
            return "headless must be a boolean"

        return None

    def get_permission_key(self, tool_input: dict[str, Any]) -> str:
        action = str(tool_input.get("action", "")).strip() or "unknown"
        return f"{self.name}:{action}"

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> PermissionResult:
        action = str(tool_input.get("action", "")).strip()
        permission_key = self.get_permission_key(tool_input)

        if action in {"close_tab", "close_session", "list_tabs", "extract", "new_tab", "switch_tab", "wait_for"}:
            return PermissionResult(behavior=PermissionBehavior.ALLOW, permission_key=permission_key)

        if action == "screenshot":
            path = str(tool_input.get("path", "")).strip()
            if not path:
                return PermissionResult(behavior=PermissionBehavior.ALLOW, permission_key=permission_key)
            abs_path = Path(path)
            if not abs_path.is_absolute():
                abs_path = Path(context.cwd) / abs_path
            try:
                abs_path.relative_to(Path(context.cwd))
            except ValueError:
                return PermissionResult(
                    behavior=PermissionBehavior.ASK,
                    reason="Screenshot path is outside the working directory",
                    permission_key=permission_key,
                )
            return PermissionResult(behavior=PermissionBehavior.ALLOW, permission_key=permission_key)

        if action in _SENSITIVE_ACTIONS:
            return PermissionResult(
                behavior=PermissionBehavior.ASK,
                reason=f"Browser action '{action}' is sensitive and requires confirmation",
                permission_key=permission_key,
            )

        if action in _SESSION_ACTIONS:
            return PermissionResult(
                behavior=PermissionBehavior.ASK,
                reason=f"Browser action '{action}' requires confirmation",
                permission_key=permission_key,
            )

        if action == "click":
            session = self._sessions.get(str(tool_input.get("session_id", "")).strip())
            url = str(tool_input.get("url", "")).strip()
            if url and session:
                target_origin = _origin_from_url(url)
                if target_origin and session.trusted_origins and target_origin not in session.trusted_origins:
                    return PermissionResult(
                        behavior=PermissionBehavior.ASK,
                        reason="Browser click may navigate to a new origin",
                        permission_key=permission_key,
                    )
            return PermissionResult(behavior=PermissionBehavior.ALLOW, permission_key=permission_key)

        return PermissionResult(behavior=PermissionBehavior.ALLOW, permission_key=permission_key)

    async def call(
        self,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        if not self.is_enabled or not self._enabled:
            return ToolResult(
                result_for_model=self._dependency_error or "Browser is disabled in this session.",
                is_error=True,
            )
        if not self._playwright_available or self._playwright_module is None:
            return ToolResult(
                result_for_model=self._dependency_error or "Browser is unavailable.",
                is_error=True,
            )

        action = str(tool_input["action"]).strip()
        try:
            if action == "create_session":
                return await self._create_session(tool_input)
            if action == "close_session":
                return await self._close_session_result(str(tool_input["session_id"]).strip())

            session = self._require_session(str(tool_input["session_id"]).strip())
            if isinstance(session, ToolResult):
                return session

            if action == "goto":
                return await self._goto(session, tool_input)
            if action == "click":
                return await self._click(session, tool_input)
            if action == "fill":
                return await self._fill(session, tool_input)
            if action == "press":
                return await self._press(session, tool_input)
            if action == "wait_for":
                return await self._wait_for(session, tool_input)
            if action == "extract":
                return await self._extract(session, tool_input)
            if action == "screenshot":
                return await self._screenshot(session, tool_input, context)
            if action == "evaluate":
                return await self._evaluate(session, tool_input)
            if action == "list_tabs":
                return self._list_tabs(session)
            if action == "new_tab":
                return await self._new_tab(session, tool_input)
            if action == "switch_tab":
                return self._switch_tab(session, tool_input)
            if action == "close_tab":
                return await self._close_tab(session, tool_input)
        except Exception as exc:
            logger.exception("Browser action failed: %s", action)
            return ToolResult(
                result_for_model=f"Browser error during {action}: {exc}",
                is_error=True,
            )

        return ToolResult(result_for_model=f"Unsupported browser action: {action}", is_error=True)

    def _timeout_ms(self, tool_input: dict[str, Any]) -> int:
        raw = tool_input.get("timeout_seconds")
        if raw is None:
            return self._default_timeout_ms
        return max(1, int(float(raw) * 1000))

    def _require_session(self, session_id: str) -> _BrowserSession | ToolResult:
        session = self._sessions.get(session_id)
        if session is None:
            return ToolResult(
                result_for_model=f"Unknown browser session: {session_id}",
                is_error=True,
            )
        return session

    def _get_tab(self, session: _BrowserSession, tool_input: dict[str, Any]) -> tuple[str, Any] | ToolResult:
        tab_id = str(tool_input.get("tab_id", "")).strip() or session.active_tab_id or ""
        page = session.tabs.get(tab_id)
        if page is None:
            return ToolResult(
                result_for_model=f"Unknown browser tab: {tab_id or '(none)'}",
                is_error=True,
            )
        return tab_id, page

    def _check_domain(self, url: str) -> ToolResult | None:
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        if not host:
            return None
        if self._allowed_domains and host not in self._allowed_domains:
            return ToolResult(result_for_model=f"Blocked by Browser.allowed_domains: {host}", is_error=True)
        if self._blocked_domains and host in self._blocked_domains:
            return ToolResult(result_for_model=f"Blocked by Browser.blocked_domains: {host}", is_error=True)
        return None

    async def _create_session(self, tool_input: dict[str, Any]) -> ToolResult:
        if len(self._sessions) >= self._max_sessions:
            return ToolResult(
                result_for_model=f"Maximum Browser sessions reached ({self._max_sessions})",
                is_error=True,
            )

        playwright = await self._playwright_module().start()
        browser_type = getattr(playwright, self._default_browser, None)
        if browser_type is None:
            await playwright.stop()
            return ToolResult(result_for_model=f"Unsupported browser type: {self._default_browser}", is_error=True)

        headless = bool(tool_input["headless"]) if "headless" in tool_input else self._headless
        launch_options = dict(self._launch_options)
        launch_options["headless"] = headless
        browser = await browser_type.launch(**launch_options)

        user_data_dir: str | None = None
        if self._storage_dir:
            base = Path(self._storage_dir)
            base.mkdir(parents=True, exist_ok=True)
            user_data_dir = str(base / f"browser-{uuid.uuid4().hex[:8]}")
            Path(user_data_dir).mkdir(parents=True, exist_ok=True)
        else:
            user_data_dir = tempfile.mkdtemp(prefix="crabcode-browser-")

        context_options = dict(self._context_options)
        context_options.setdefault("accept_downloads", not self._block_downloads)
        context = await browser.new_context(**context_options)
        page = await context.new_page()
        page.set_default_timeout(self._default_timeout_ms)
        page.set_default_navigation_timeout(self._default_timeout_ms)

        session_id = str(uuid.uuid4())
        tab_id = str(uuid.uuid4())
        session = _BrowserSession(
            session_id=session_id,
            playwright=playwright,
            browser=browser,
            context=context,
            headless=headless,
            tabs={tab_id: page},
            active_tab_id=tab_id,
            user_data_dir=user_data_dir,
        )
        self._sessions[session_id] = session

        url = str(tool_input.get("url", "")).strip()
        if url:
            denied = self._check_domain(url)
            if denied is not None:
                await self._close_session(session_id)
                return denied
            await page.goto(url, wait_until=str(tool_input.get("wait_until", "load")), timeout=self._timeout_ms(tool_input))
            origin = _origin_from_url(page.url)
            if origin:
                session.trusted_origins.add(origin)

        return self._result(
            session=session,
            tab_id=tab_id,
            status="created",
            data={"tabs": [self._tab_summary(tab_id, page)]},
        )

    async def _goto(self, session: _BrowserSession, tool_input: dict[str, Any]) -> ToolResult:
        url = str(tool_input.get("url", "")).strip()
        denied = self._check_domain(url)
        if denied is not None:
            return denied
        tab = self._get_tab(session, tool_input)
        if isinstance(tab, ToolResult):
            return tab
        tab_id, page = tab
        await page.goto(url, wait_until=str(tool_input.get("wait_until", "load")), timeout=self._timeout_ms(tool_input))
        origin = _origin_from_url(page.url)
        if origin:
            session.trusted_origins.add(origin)
        session.active_tab_id = tab_id
        return self._result(session=session, tab_id=tab_id, status="navigated")

    async def _click(self, session: _BrowserSession, tool_input: dict[str, Any]) -> ToolResult:
        tab = self._get_tab(session, tool_input)
        if isinstance(tab, ToolResult):
            return tab
        tab_id, page = tab
        selector = str(tool_input.get("selector", "")).strip()
        await page.click(selector, timeout=self._timeout_ms(tool_input))
        origin = _origin_from_url(page.url)
        if origin:
            session.trusted_origins.add(origin)
        return self._result(session=session, tab_id=tab_id, status="clicked")

    async def _fill(self, session: _BrowserSession, tool_input: dict[str, Any]) -> ToolResult:
        tab = self._get_tab(session, tool_input)
        if isinstance(tab, ToolResult):
            return tab
        tab_id, page = tab
        await page.fill(
            str(tool_input.get("selector", "")).strip(),
            str(tool_input.get("text", "")),
            timeout=self._timeout_ms(tool_input),
        )
        return self._result(session=session, tab_id=tab_id, status="filled")

    async def _press(self, session: _BrowserSession, tool_input: dict[str, Any]) -> ToolResult:
        tab = self._get_tab(session, tool_input)
        if isinstance(tab, ToolResult):
            return tab
        tab_id, page = tab
        selector = str(tool_input.get("selector", "")).strip()
        key = str(tool_input.get("text", "")).strip()
        if selector:
            await page.press(selector, key, timeout=self._timeout_ms(tool_input))
        else:
            await page.keyboard.press(key)
        return self._result(session=session, tab_id=tab_id, status="pressed")

    async def _wait_for(self, session: _BrowserSession, tool_input: dict[str, Any]) -> ToolResult:
        tab = self._get_tab(session, tool_input)
        if isinstance(tab, ToolResult):
            return tab
        tab_id, page = tab
        selector = str(tool_input.get("selector", "")).strip()
        if selector:
            await page.wait_for_selector(selector, timeout=self._timeout_ms(tool_input))
        else:
            await page.wait_for_load_state(str(tool_input.get("wait_until", "load")), timeout=self._timeout_ms(tool_input))
        return self._result(session=session, tab_id=tab_id, status="ready")

    async def _extract(self, session: _BrowserSession, tool_input: dict[str, Any]) -> ToolResult:
        tab = self._get_tab(session, tool_input)
        if isinstance(tab, ToolResult):
            return tab
        tab_id, page = tab
        return_format = str(tool_input.get("return_format", "text"))
        if return_format == "html":
            data = await page.content()
        elif return_format == "links":
            data = await page.eval_on_selector_all(
                "a[href]",
                """els => els.map(el => ({
                    text: (el.textContent || '').trim(),
                    href: el.href
                }))""",
            )
        elif return_format == "json":
            data = await page.evaluate(
                """() => ({
                    title: document.title,
                    text: (document.body?.innerText || '').trim(),
                    url: location.href
                })"""
            )
        else:
            data = (await page.text_content("body")) or ""
        return self._result(session=session, tab_id=tab_id, status="extracted", data=data)

    async def _screenshot(self, session: _BrowserSession, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        tab = self._get_tab(session, tool_input)
        if isinstance(tab, ToolResult):
            return tab
        tab_id, page = tab
        path = str(tool_input.get("path", "")).strip()
        if not path:
            session_dir = Path(context.cwd) / ".crabcode" / "browser"
            session_dir.mkdir(parents=True, exist_ok=True)
            path = str(session_dir / f"{session.session_id}-{tab_id}.png")
        out = Path(path)
        if not out.is_absolute():
            out = Path(context.cwd) / out
        out.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(out), full_page=bool(tool_input.get("options", {}).get("full_page", True)))
        return self._result(
            session=session,
            tab_id=tab_id,
            status="screenshot",
            data={"path": str(out)},
        )

    async def _evaluate(self, session: _BrowserSession, tool_input: dict[str, Any]) -> ToolResult:
        tab = self._get_tab(session, tool_input)
        if isinstance(tab, ToolResult):
            return tab
        tab_id, page = tab
        data = await page.evaluate(str(tool_input.get("script", "")))
        return self._result(session=session, tab_id=tab_id, status="evaluated", data=data)

    def _list_tabs(self, session: _BrowserSession) -> ToolResult:
        tabs = [self._tab_summary(tab_id, page) for tab_id, page in session.tabs.items()]
        return self._result(
            session=session,
            tab_id=session.active_tab_id,
            status="listed_tabs",
            data={"tabs": tabs},
        )

    async def _new_tab(self, session: _BrowserSession, tool_input: dict[str, Any]) -> ToolResult:
        page = await session.context.new_page()
        page.set_default_timeout(self._default_timeout_ms)
        page.set_default_navigation_timeout(self._default_timeout_ms)
        tab_id = str(uuid.uuid4())
        session.tabs[tab_id] = page
        session.active_tab_id = tab_id
        url = str(tool_input.get("url", "")).strip()
        if url:
            denied = self._check_domain(url)
            if denied is not None:
                await page.close()
                session.tabs.pop(tab_id, None)
                return denied
            await page.goto(url, wait_until=str(tool_input.get("wait_until", "load")), timeout=self._timeout_ms(tool_input))
            origin = _origin_from_url(page.url)
            if origin:
                session.trusted_origins.add(origin)
        return self._result(session=session, tab_id=tab_id, status="new_tab")

    def _switch_tab(self, session: _BrowserSession, tool_input: dict[str, Any]) -> ToolResult:
        tab_id = str(tool_input.get("tab_id", "")).strip()
        if tab_id not in session.tabs:
            return ToolResult(result_for_model=f"Unknown browser tab: {tab_id}", is_error=True)
        session.active_tab_id = tab_id
        return self._result(session=session, tab_id=tab_id, status="switched_tab")

    async def _close_tab(self, session: _BrowserSession, tool_input: dict[str, Any]) -> ToolResult:
        tab = self._get_tab(session, tool_input)
        if isinstance(tab, ToolResult):
            return tab
        tab_id, page = tab
        await page.close()
        session.tabs.pop(tab_id, None)
        session.active_tab_id = next(iter(session.tabs), None)
        return self._result(session=session, tab_id=session.active_tab_id, status="closed_tab")

    async def _close_session_result(self, session_id: str) -> ToolResult:
        session = self._sessions.get(session_id)
        if session is None:
            return ToolResult(result_for_model=f"Unknown browser session: {session_id}", is_error=True)
        await self._close_session(session_id)
        return ToolResult(
            data={"session_id": session_id, "status": "closed"},
            result_for_model=f"session_id: {session_id}\nstatus: closed",
        )

    async def _close_session(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session is None:
            return
        try:
            await session.context.close()
        except Exception:
            logger.debug("Failed to close browser context %s", session_id, exc_info=True)
        try:
            await session.browser.close()
        except Exception:
            logger.debug("Failed to close browser %s", session_id, exc_info=True)
        try:
            await session.playwright.stop()
        except Exception:
            logger.debug("Failed to stop playwright %s", session_id, exc_info=True)
        if session.user_data_dir:
            shutil.rmtree(session.user_data_dir, ignore_errors=True)

    def _tab_summary(self, tab_id: str, page: Any) -> dict[str, Any]:
        return {
            "tab_id": tab_id,
            "url": page.url,
            "title": "",
            "active": False,
        }

    def _result(
        self,
        *,
        session: _BrowserSession,
        tab_id: str | None,
        status: str,
        data: Any = None,
    ) -> ToolResult:
        page = session.tabs.get(tab_id) if tab_id else None
        url = page.url if page is not None else ""
        payload = {
            "session_id": session.session_id,
            "tab_id": tab_id,
            "url": url,
            "title": "",
            "headless": session.headless,
            "status": status,
            "data": data,
        }
        text = [
            f"session_id: {session.session_id}",
            f"tab_id: {tab_id or '(none)'}",
            f"url: {url or '(blank)'}",
            "title: ",
            f"headless: {str(session.headless).lower()}",
            f"status: {status}",
        ]
        if data is not None:
            compact = json.dumps(data, ensure_ascii=False) if not isinstance(data, str) else data
            if len(compact) > 4000:
                compact = compact[:4000] + "…"
            text.append("data:")
            text.append(compact)
        return ToolResult(data=payload, result_for_model="\n".join(text))
