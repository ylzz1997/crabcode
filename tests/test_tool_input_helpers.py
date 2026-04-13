"""Tests for tool argument normalization helpers."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from crabcode_core.tools._input_helpers import (
    first_non_empty_str,
    latest_user_text_for_agent_fallback,
)
from crabcode_core.tools.agent import AgentTool
from crabcode_core.tools.file_read import FileReadTool
from crabcode_core.tools.glob import GlobTool
from crabcode_core.tools.grep import GrepTool
from crabcode_core.types.message import TextBlock, create_user_message
from crabcode_core.types.tool import ToolContext


def test_first_non_empty_str() -> None:
    assert first_non_empty_str({"a": "x", "b": "y"}, ("b", "a")) == "y"
    assert first_non_empty_str({"prompt": "  hi  "}, ("prompt",)) == "hi"
    assert first_non_empty_str({}, ("prompt",)) == ""
    assert first_non_empty_str({"prompt": ""}, ("task", "prompt"),) == ""


def test_agent_resolves_task_alias() -> None:
    async def _run() -> None:
        tool = AgentTool(manager=None)
        ctx = ToolContext()
        r = await tool.call(
            {"task": "Summarize README", "subagent_type": "explore"},
            ctx,
        )
        assert r.is_error
        assert "agent manager" in (r.result_for_model or "").lower()

    asyncio.run(_run())


def test_glob_resolves_glob_alias() -> None:
    async def _run() -> None:
        root = Path(__file__).resolve().parents[1]
        tool = GlobTool()
        ctx = ToolContext(cwd=str(root))
        r = await tool.call({"glob": "*.toml", "path": str(root)}, ctx)
        assert not r.is_error
        assert "pyproject.toml" in (r.result_for_model or "")

    asyncio.run(_run())


def test_grep_resolves_regex_alias() -> None:
    async def _run() -> None:
        root = Path(__file__).resolve().parents[1]
        tool = GrepTool()
        ctx = ToolContext(cwd=str(root))
        r = await tool.call(
            {
                "regex": "def first_non_empty_str",
                "path": "packages/core",
                "glob": "*.py",
            },
            ctx,
        )
        # Must accept "regex" alias — not fail with missing pattern.
        assert "pattern is required" not in (r.result_for_model or "").lower()

    asyncio.run(_run())


def test_read_resolves_path_alias() -> None:
    async def _run() -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".txt",
            delete=False,
            dir=root,
        ) as f:
            f.write("hello read alias\n")
            tmp_path = f.name
        try:
            tool = FileReadTool()
            rel = Path(tmp_path).relative_to(root)
            ctx = ToolContext(cwd=str(root))
            r = await tool.call({"path": str(rel)}, ctx)
            assert not r.is_error
            assert "hello read alias" in (r.result_for_model or "")
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    asyncio.run(_run())


def test_latest_user_text_skips_tool_only_messages() -> None:
    from crabcode_core.types.message import (
        ToolResultBlock,
        create_tool_result_message,
    )

    u1 = create_user_message([TextBlock(text="Real question")])
    u2 = create_tool_result_message("tid", "tool output", is_error=False)
    assert latest_user_text_for_agent_fallback([u1, u2]) == "Real question"

