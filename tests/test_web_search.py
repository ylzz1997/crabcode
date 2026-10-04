"""WebSearch must not treat a DuckDuckGo bot check as an empty result set."""

import asyncio

import pytest

from crabcode_core.tools.web_search import (
    DuckDuckGoUnavailable,
    WebSearchTool,
    _is_ddg_challenge,
    _note_ddg_unavailable,
    _parse_bing_rss,
    _parse_ddg_html,
    _reset_ddg_backoff,
)
from crabcode_core.types.tool import ToolContext


@pytest.fixture(autouse=True)
def _clear_ddg_backoff():
    _reset_ddg_backoff()
    yield
    _reset_ddg_backoff()


def test_ddg_challenge_is_not_an_empty_result_page():
    html = (
        "<html><div class='anomaly-modal__title'>"
        "Unfortunately, bots use DuckDuckGo too."
        "</div></html>"
    )
    assert _is_ddg_challenge(202, html)
    assert _is_ddg_challenge(200, html)
    assert _parse_ddg_html(html) == []


def test_ddg_html_parser_unwraps_result_links():
    html = """
    <html><body>
      <a class="result__a" href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage&rut=1">
        Example Title
      </a>
      <a class="result__snippet">A useful snippet</a>
    </body></html>
    """
    results = _parse_ddg_html(html)
    assert results == [
        {
            "title": "Example Title",
            "url": "https://example.com/page",
            "snippet": "A useful snippet",
        }
    ]


def test_bing_rss_parser_reads_items_and_strips_markup():
    xml_text = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <item>
        <title>Limbus Company on Steam</title>
        <link>https://store.steampowered.com/app/1973530/Limbus_Company/</link>
        <description>Lead your &amp; group of &lt;b&gt;Sinners&lt;/b&gt;.</description>
      </item>
    </channel></rss>
    """
    assert _parse_bing_rss(xml_text) == [
        {
            "title": "Limbus Company on Steam",
            "url": "https://store.steampowered.com/app/1973530/Limbus_Company/",
            "snippet": "Lead your & group of Sinners .",
        }
    ]


def test_bing_html_page_is_an_error():
    with pytest.raises(RuntimeError, match="HTML page"):
        _parse_bing_rss("<!DOCTYPE html><html><title>captcha</title></html>")


def test_auto_falls_back_to_bing_when_duckduckgo_is_blocked(monkeypatch):
    tool = WebSearchTool()
    tool._provider = "auto"
    tool._api_key = None
    calls = {"bing": 0}

    async def blocked(query, num_results):
        raise DuckDuckGoUnavailable("bot check")

    async def bing(query, num_results):
        calls["bing"] += 1
        return [
            {
                "title": "Steam",
                "url": "https://store.steampowered.com/app/1973530",
                "snippet": "Sinners",
            }
        ]

    monkeypatch.setattr(tool, "_search_ddg", blocked)
    monkeypatch.setattr(tool, "_search_bing", bing)

    result = asyncio.run(tool.call({"query": "limbus company"}, ToolContext()))

    assert result.is_error is False
    assert result.data["provider"] == "bing"
    assert "Steam" in result.result_for_model
    assert calls["bing"] == 1


def test_auto_keeps_a_genuine_empty_duckduckgo_result(monkeypatch):
    tool = WebSearchTool()
    tool._provider = "auto"
    tool._api_key = None

    async def empty(query, num_results):
        return []

    async def bing(query, num_results):
        raise AssertionError("Bing should not run when DuckDuckGo returned a real page")

    monkeypatch.setattr(tool, "_search_ddg", empty)
    monkeypatch.setattr(tool, "_search_bing", bing)

    result = asyncio.run(tool.call({"query": "zzzz-no-such-page"}, ToolContext()))

    assert result.is_error is False
    assert result.data["provider"] == "ddg"
    assert result.result_for_model == "No results found."


def test_ddg_provider_reports_the_block_instead_of_no_results(monkeypatch):
    tool = WebSearchTool()
    tool._provider = "ddg"

    async def blocked(query, num_results):
        _note_ddg_unavailable()
        raise DuckDuckGoUnavailable("bot check")

    monkeypatch.setattr(tool, "_search_ddg", blocked)

    first = asyncio.run(tool.call({"query": "limbus company"}, ToolContext()))
    second = asyncio.run(tool.call({"query": "limbus company"}, ToolContext()))

    assert first.is_error is True
    assert "bot check" in first.result_for_model
    assert second.is_error is True
    assert "bot check" in second.result_for_model


def test_backoff_skips_duckduckgo_and_uses_bing(monkeypatch):
    tool = WebSearchTool()
    tool._provider = "auto"
    tool._api_key = None
    _note_ddg_unavailable()

    async def ddg(query, num_results):
        raise AssertionError("DuckDuckGo should stay skipped during backoff")

    async def bing(query, num_results):
        return [{"title": "Wiki", "url": "https://example.com/wiki", "snippet": "plot"}]

    monkeypatch.setattr(tool, "_search_ddg", ddg)
    monkeypatch.setattr(tool, "_search_bing", bing)

    provider, results = asyncio.run(tool._run_search("canto", 5))

    assert provider == "bing"
    assert results[0]["title"] == "Wiki"
