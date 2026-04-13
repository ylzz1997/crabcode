"""WebSearchTool — search the web using Tavily or DuckDuckGo."""

from __future__ import annotations

import os
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse

import httpx

from crabcode_core.logging_utils import get_logger
from crabcode_core.types.tool import PermissionBehavior, PermissionResult, Tool, ToolContext, ToolResult

logger = get_logger(__name__)

_DEFAULT_TIMEOUT = 8
_DEFAULT_MAX_RESULTS = 5
_MAX_RESULTS = 10
_DDG_BASE_URL = "https://html.duckduckgo.com/html/"
_DDG_RESULT_BASE_URL = "https://duckduckgo.com"
_TAVILY_URL = "https://api.tavily.com/search"


def _is_http_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _resolve_ddg_result_url(url: str) -> str:
    raw = url.strip()
    if not raw:
        return ""

    if raw.startswith("//"):
        raw = f"https:{raw}"
    elif raw.startswith("/"):
        raw = urljoin(_DDG_RESULT_BASE_URL, raw)

    parsed = urlparse(raw)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        uddg = parse_qs(parsed.query).get("uddg", [])
        if uddg:
            return unquote(uddg[0]).strip()

    return raw


def _clean_text(value: str) -> str:
    return " ".join(value.split())


class _DuckDuckGoHTMLParser(HTMLParser):
    """Extract result links from DuckDuckGo's HTML results page."""

    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._current: dict[str, str] | None = None
        self._capture_title = False
        self._capture_snippet = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_map = dict(attrs)
        class_name = attrs_map.get("class", "")
        if tag == "a" and "result__a" in class_name:
            self._flush_current()
            href = attrs_map.get("href") or ""
            self._current = {"title": "", "url": href, "snippet": ""}
            self._capture_title = True
            return
        if self._current and tag in {"a", "div", "span"} and "result__snippet" in class_name:
            self._capture_snippet = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._capture_title:
            self._capture_title = False
        if tag in {"a", "div", "span"} and self._capture_snippet:
            self._capture_snippet = False
        if tag == "article" and self._current:
            self._flush_current()

    def handle_data(self, data: str) -> None:
        if not self._current:
            return
        if self._capture_title:
            self._current["title"] += data
        elif self._capture_snippet:
            self._current["snippet"] += data

    def close(self) -> None:
        super().close()
        self._flush_current()

    def _flush_current(self) -> None:
        if not self._current:
            return
        title = _clean_text(self._current.get("title", ""))
        url = _resolve_ddg_result_url(self._current.get("url", ""))
        snippet = _clean_text(self._current.get("snippet", ""))
        if title and _is_http_url(url):
            self.results.append({"title": title, "url": url, "snippet": snippet})
        self._current = None
        self._capture_title = False
        self._capture_snippet = False


class WebSearchTool(Tool):
    name = "WebSearch"
    description = "Search the web for current external information using Tavily or DuckDuckGo."
    is_read_only = True
    is_concurrency_safe = True
    input_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query to run on the public web.",
            },
            "num_results": {
                "type": "integer",
                "description": "Maximum number of results to return (default 5, max 10).",
                "default": _DEFAULT_MAX_RESULTS,
            },
        },
        "required": ["query"],
    }

    def __init__(self) -> None:
        self._tool_config: dict[str, Any] = {}
        self._provider = "auto"
        self._timeout = _DEFAULT_TIMEOUT
        self._max_results = _DEFAULT_MAX_RESULTS
        self._api_key_env = "TAVILY_API_KEY"
        self._api_key: str | None = None

    async def setup(self, context: ToolContext) -> None:
        await super().setup(context)
        self._tool_config = dict(context.tool_config)
        self._provider = str(self._tool_config.get("provider", "auto")).lower()
        self._timeout = int(self._tool_config.get("timeout_seconds", _DEFAULT_TIMEOUT))
        self._max_results = max(1, min(int(self._tool_config.get("max_results", _DEFAULT_MAX_RESULTS)), _MAX_RESULTS))
        self._api_key_env = str(self._tool_config.get("api_key_env", "TAVILY_API_KEY"))
        self._api_key = os.environ.get(self._api_key_env) or context.env.get(self._api_key_env)

        if self._provider not in {"auto", "tavily", "ddg"}:
            logger.warning("Unknown WebSearch provider %r; falling back to auto", self._provider)
            self._provider = "auto"

        if self._provider == "tavily" and not self._api_key:
            logger.info("Disabling WebSearch: provider=tavily but %s is not configured", self._api_key_env)
            self.is_enabled = False
            return

        if not await self._has_connectivity():
            logger.info("Disabling WebSearch: outbound network probe failed")
            self.is_enabled = False

    async def get_prompt(self, **kwargs: Any) -> str:
        return (
            "Search the public web for current external information. "
            "Use this instead of shell-based web access when you need recent facts, docs, or sources outside the repo. "
            "Returns a compact list of result titles, URLs, and snippets. "
            "This tool performs search only; it does not fetch and read full pages."
        )

    async def validate_input(self, tool_input: dict[str, Any]) -> str | None:
        query = str(tool_input.get("query", "")).strip()
        if not query:
            return "query is required"
        raw_num = tool_input.get("num_results", _DEFAULT_MAX_RESULTS)
        try:
            int(raw_num)
        except (TypeError, ValueError):
            return "num_results must be an integer"
        return None

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> PermissionResult:
        return PermissionResult(
            behavior=PermissionBehavior.ASK,
            reason="WebSearch requires confirmation before making a network request",
        )

    async def call(
        self,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        if not self.is_enabled:
            return ToolResult(
                result_for_model="WebSearch is unavailable in this session because network access was not detected during initialization.",
                is_error=True,
            )

        query = str(tool_input.get("query", "")).strip()
        if not query:
            return ToolResult(result_for_model="query is required.", is_error=True)

        num_results = max(1, min(int(tool_input.get("num_results", self._max_results)), _MAX_RESULTS))

        try:
            provider, results = await self._run_search(query, num_results)
        except Exception as exc:
            logger.exception("WebSearch failed for query %r", query)
            return ToolResult(
                result_for_model=f"WebSearch error: {exc}",
                is_error=True,
            )

        if not results:
            return ToolResult(
                data={"provider": provider, "query": query, "results": []},
                result_for_model="No results found.",
            )

        lines = []
        for idx, item in enumerate(results, 1):
            snippet = item["snippet"] or "(no snippet)"
            lines.append(f"[{idx}] {item['title']}\nURL: {item['url']}\nSnippet: {snippet}")

        return ToolResult(
            data={"provider": provider, "query": query, "results": results},
            result_for_model="\n\n".join(lines),
        )

    async def _has_connectivity(self) -> bool:
        for probe_url in self._probe_urls():
            if await self._probe_endpoint(probe_url):
                return True
        return False

    def _probe_urls(self) -> list[str]:
        if self._provider == "tavily":
            return [_TAVILY_URL]
        if self._provider == "ddg":
            return [_DDG_BASE_URL]
        if self._api_key:
            return [_TAVILY_URL, _DDG_BASE_URL]
        return [_DDG_BASE_URL]

    async def _probe_endpoint(self, url: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True) as client:
                response = await client.get(url)
            return response.status_code < 500
        except Exception:
            return False

    async def _run_search(self, query: str, num_results: int) -> tuple[str, list[dict[str, str]]]:
        if self._provider == "tavily":
            return "tavily", await self._search_tavily(query, num_results)
        if self._provider == "ddg":
            return "ddg", await self._search_ddg(query, num_results)

        if self._api_key:
            try:
                return "tavily", await self._search_tavily(query, num_results)
            except Exception:
                logger.warning("Tavily search failed; retrying with DuckDuckGo", exc_info=True)

        return "ddg", await self._search_ddg(query, num_results)

    async def _search_tavily(self, query: str, num_results: int) -> list[dict[str, str]]:
        if not self._api_key:
            raise RuntimeError(f"{self._api_key_env} is not configured")

        payload = {
            "api_key": self._api_key,
            "query": query,
            "max_results": num_results,
            "search_depth": "basic",
            "include_answer": False,
            "include_raw_content": False,
        }
        async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True) as client:
            response = await client.post(_TAVILY_URL, json=payload)
            response.raise_for_status()
            data = response.json()

        raw_results = data.get("results", [])
        return self._normalize_results(
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("content", "") or item.get("snippet", ""),
            }
            for item in raw_results
        )[:num_results]

    async def _search_ddg(self, query: str, num_results: int) -> list[dict[str, str]]:
        url = f"{_DDG_BASE_URL}?q={quote(query)}"
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; CrabCode/0.1; +https://example.invalid/crabcode)"
        }
        async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True, headers=headers) as client:
            response = await client.get(url)
            response.raise_for_status()
            html = response.text

        parser = _DuckDuckGoHTMLParser()
        parser.feed(html)
        parser.close()
        return self._normalize_results(parser.results)[:num_results]

    def _normalize_results(self, items: Any) -> list[dict[str, str]]:
        results: list[dict[str, str]] = []
        seen_urls: set[str] = set()
        for item in items:
            title = _clean_text(str(item.get("title", "")))
            url = str(item.get("url", "")).strip()
            snippet = _clean_text(str(item.get("snippet", "")))
            if not title or not _is_http_url(url) or url in seen_urls:
                continue
            seen_urls.add(url)
            results.append({"title": title, "url": url, "snippet": snippet})
        return results
