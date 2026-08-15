from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from crabcode_core.prompts.context import _load_memories_context
from crabcode_core.tools import memory as memory_module
from crabcode_core.tools.memory import MemoryTool, format_memory_directory
from crabcode_core.types.tool import ToolContext


@pytest.fixture
def memory_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    project = tmp_path / "project"
    project.mkdir()
    global_dir = tmp_path / "global"
    monkeypatch.setattr(memory_module, "GLOBAL_MEMORY_DIR", global_dir)
    return project, global_dir


def _call(tool: MemoryTool, tool_input: dict[str, object], project: Path):
    return asyncio.run(tool.call(tool_input, ToolContext(cwd=str(project))))


def test_context_injects_directory_without_full_content(memory_paths: tuple[Path, Path]) -> None:
    project, _ = memory_paths
    tool = MemoryTool()
    created = _call(
        tool,
        {
            "action": "create",
            "title": "Database migration convention",
            "content": "Use expand-and-contract migrations and never drop a column immediately.",
        },
        project,
    )

    context = _load_memories_context(str(project))

    assert context is not None
    assert created.data["id"] in context
    assert "Database migration convention" in context
    assert "never drop a column immediately" not in context
    assert "search and read" in context


def test_search_then_read_retrieves_full_content(memory_paths: tuple[Path, Path]) -> None:
    project, _ = memory_paths
    tool = MemoryTool()
    project_memory = _call(
        tool,
        {
            "action": "create",
            "title": "Cache choice",
            "content": "The API cache uses Redis with a five-minute TTL.",
        },
        project,
    )
    _call(
        tool,
        {
            "action": "create",
            "scope": "global",
            "title": "Response style",
            "content": "Prefer concise answers.",
        },
        project,
    )

    search = _call(tool, {"action": "search", "query": "Redis"}, project)
    read = _call(
        tool,
        {"action": "read", "memory_id": project_memory.data["id"]},
        project,
    )

    assert search.data["count"] == 1
    assert search.data["matches"][0]["id"] == project_memory.data["id"]
    assert search.data["matches"][0]["scope"] == "project"
    assert "Redis" in search.data["matches"][0]["excerpt"]
    assert not read.is_error
    assert read.data["content"] == "The API cache uses Redis with a five-minute TTL."


def test_list_is_title_only_and_can_filter_scope(memory_paths: tuple[Path, Path]) -> None:
    project, _ = memory_paths
    tool = MemoryTool()
    _call(
        tool,
        {
            "action": "create",
            "title": "Project-only summary",
            "content": "project detail that should stay out of the listing",
        },
        project,
    )
    _call(
        tool,
        {
            "action": "create",
            "scope": "global",
            "title": "Global summary",
            "content": "global detail that should stay out of the listing",
        },
        project,
    )

    result = _call(tool, {"action": "list", "scope": "project"}, project)

    assert result.data == {"count": 1}
    assert "Project-only summary" in result.result_for_model
    assert "Global summary" not in result.result_for_model
    assert "project detail" not in result.result_for_model


def test_directory_is_bounded_and_prefers_recent_memories() -> None:
    memories = [
        {
            "id": f"memory-{index}",
            "title": f"Summary {index}",
            "content": f"Detail {index}",
            "created_at": f"2026-01-{index + 1:02d}T00:00:00+00:00",
            "_scope": "project",
        }
        for index in range(5)
    ]

    directory = format_memory_directory(memories, limit=2)

    assert "2 of 5 shown" in directory
    assert "Summary 4" in directory
    assert "Summary 3" in directory
    assert "Summary 2" not in directory
    assert "Detail" not in directory


def test_search_and_read_validation() -> None:
    tool = MemoryTool()

    assert asyncio.run(tool.validate_input({"action": "search"})) == (
        "query is required for 'search'"
    )
    assert asyncio.run(
        tool.validate_input({"action": "search", "query": "cache", "limit": 51})
    ) == "limit must be an integer between 1 and 50"
    assert asyncio.run(tool.validate_input({"action": "read"})) == (
        "memory_id is required for 'read'"
    )
    assert asyncio.run(
        tool.validate_input({"action": "read", "memory_id": "abc", "scope": "team"})
    ) == "scope must be one of: project, global"
