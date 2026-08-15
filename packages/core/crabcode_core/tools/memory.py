"""MemoryTool — persistent memory across conversations."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from crabcode_core.logging_utils import get_logger
from crabcode_core.types.tool import Tool, ToolContext, ToolResult

logger = get_logger(__name__)

GLOBAL_MEMORY_DIR = Path.home() / ".crabcode"
PROJECT_MEMORY_DIR_NAME = ".crabcode"
MEMORY_FILENAME = "memories.json"
MEMORY_DIRECTORY_LIMIT = 100
MEMORY_SEARCH_DEFAULT_LIMIT = 10
MEMORY_SEARCH_MAX_LIMIT = 50
MEMORY_SEARCH_EXCERPT_LENGTH = 180


def _memory_path(scope: str, cwd: str) -> Path:
    if scope == "global":
        return GLOBAL_MEMORY_DIR / MEMORY_FILENAME
    return Path(cwd) / PROJECT_MEMORY_DIR_NAME / MEMORY_FILENAME


def _load_memories(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [entry for entry in data if isinstance(entry, dict)]
    except (json.JSONDecodeError, OSError):
        logger.warning("Failed to load memories from %s", path, exc_info=True)
    return []


def _save_memories(path: Path, memories: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(memories, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def load_all_memories(cwd: str) -> list[dict[str, Any]]:
    """Load memories from both global and project scopes for context injection."""
    results: list[dict[str, Any]] = []

    global_path = _memory_path("global", cwd)
    for m in _load_memories(global_path):
        m["_scope"] = "global"
        results.append(m)

    project_path = _memory_path("project", cwd)
    if project_path != global_path:
        for m in _load_memories(project_path):
            m["_scope"] = "project"
            results.append(m)

    return results


def _memory_sort_key(memory: dict[str, Any]) -> str:
    """Sort memories by their most recent timestamp, newest first."""
    updated_at = memory.get("updated_at")
    if isinstance(updated_at, str):
        return updated_at
    created_at = memory.get("created_at")
    return created_at if isinstance(created_at, str) else ""


def _memory_summary(memory: dict[str, Any]) -> str:
    """Return a compact, defensive summary for a stored memory."""
    title = memory.get("title")
    if isinstance(title, str) and title.strip():
        return " ".join(title.split())
    return "Untitled memory"


def format_memory_directory(
    memories: list[dict[str, Any]],
    *,
    limit: int = MEMORY_DIRECTORY_LIMIT,
) -> str:
    """Format a bounded title-only directory suitable for prompt injection."""
    ordered = sorted(memories, key=_memory_sort_key, reverse=True)
    visible = ordered[:limit]
    lines: list[str] = []
    for memory in visible:
        memory_id = memory.get("id")
        if not isinstance(memory_id, str) or not memory_id:
            continue
        scope = memory.get("_scope", "?")
        lines.append(f"- [{scope}] {memory_id}: {_memory_summary(memory)}")

    if not lines:
        return ""

    heading = f"Persistent memory directory ({len(lines)} of {len(memories)} shown):"
    return "\n".join([heading, *lines])


def _scoped_memories(cwd: str, scope: str | None) -> list[dict[str, Any]]:
    memories = load_all_memories(cwd)
    if scope is None:
        return memories
    return [memory for memory in memories if memory.get("_scope") == scope]


def _search_score(memory: dict[str, Any], query: str) -> int:
    title = str(memory.get("title", "")).casefold()
    content = str(memory.get("content", "")).casefold()
    normalized_query = query.casefold().strip()
    if not normalized_query:
        return 0

    score = 0
    if normalized_query in title:
        score += 8
    if normalized_query in content:
        score += 4

    terms = [term for term in re.split(r"\s+", normalized_query) if term]
    for term in terms:
        if term in title:
            score += 2
        if term in content:
            score += 1
    return score


def _memory_excerpt(content: Any, query: str) -> str:
    text = " ".join(str(content or "").split())
    if len(text) <= MEMORY_SEARCH_EXCERPT_LENGTH:
        return text

    folded = text.casefold()
    candidates = [query.casefold().strip(), *query.casefold().split()]
    match_at = next(
        (folded.find(candidate) for candidate in candidates if candidate and candidate in folded),
        0,
    )
    half = MEMORY_SEARCH_EXCERPT_LENGTH // 2
    start = max(0, match_at - half)
    end = min(len(text), start + MEMORY_SEARCH_EXCERPT_LENGTH)
    if end - start < MEMORY_SEARCH_EXCERPT_LENGTH:
        start = max(0, end - MEMORY_SEARCH_EXCERPT_LENGTH)
    prefix = "…" if start else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{text[start:end]}{suffix}"


class MemoryTool(Tool):
    name = "Memory"
    description = "Search, read, create, update, or delete persistent memories."
    is_read_only = False
    is_concurrency_safe = False
    input_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "update", "delete", "list", "search", "read"],
                "description": "The action to perform.",
            },
            "title": {
                "type": "string",
                "description": (
                    "Short title capturing the essence of the memory. "
                    "Required for 'create' and 'update'."
                ),
            },
            "content": {
                "type": "string",
                "description": (
                    "The memory content (no more than a paragraph). "
                    "Required for 'create' and 'update'."
                ),
            },
            "memory_id": {
                "type": "string",
                "description": (
                    "ID of an existing memory. "
                    "Required for 'update', 'delete', and 'read'."
                ),
            },
            "query": {
                "type": "string",
                "description": "Search terms. Required for 'search'.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": MEMORY_SEARCH_MAX_LIMIT,
                "description": (
                    "Maximum search results (default 10, maximum 50). "
                    "Only used by 'search'."
                ),
            },
            "scope": {
                "type": "string",
                "enum": ["project", "global"],
                "description": (
                    "Memory scope. For writes, 'project' is the default. "
                    "For list/search/read, omitting scope checks both project "
                    "and global memories."
                ),
            },
        },
        "required": ["action"],
    }

    async def get_prompt(self, **kwargs: Any) -> str:
        return (
            "Search, read, create, update, or delete persistent memories that survive "
            "across conversations. Only a compact directory of memory summaries is "
            "automatically loaded into context; use search and read to retrieve details.\n\n"
            "Actions:\n"
            "- create: Store a new memory (requires title + content)\n"
            "- update: Modify an existing memory (requires memory_id + title and/or content)\n"
            "- delete: Remove a memory (requires memory_id)\n"
            "- list: Show the title-only memory directory\n"
            "- search: Find memories by title or content (requires query; returns snippets)\n"
            "- read: Retrieve one memory's full content (requires memory_id)\n\n"
            "Scope:\n"
            "- 'project' (default): Stored in .crabcode/memories.json in the project root\n"
            "- 'global': Stored in ~/.crabcode/memories.json, available across all projects\n"
            "- list/search/read check both scopes when scope is omitted\n\n"
            "Guidelines:\n"
            "- Only create memories when the user explicitly asks to remember something.\n"
            "- If the user contradicts an existing memory, DELETE the old one rather than updating.\n"
            "- Keep memories concise — no more than a paragraph each.\n"
            "- Use 'project' scope for project-specific conventions and preferences.\n"
            "- Use 'global' scope for general user preferences that apply everywhere.\n"
            "- Search first when the directory summary is insufficient, then read the relevant memory."
        )

    async def validate_input(self, tool_input: dict[str, Any]) -> str | None:
        action = tool_input.get("action")
        if action not in ("create", "update", "delete", "list", "search", "read"):
            return "action must be one of: create, update, delete, list, search, read"

        scope = tool_input.get("scope")
        if scope is not None and scope not in ("project", "global"):
            return "scope must be one of: project, global"

        if action == "create":
            if not tool_input.get("title"):
                return "title is required for 'create'"
            if not tool_input.get("content"):
                return "content is required for 'create'"

        if action == "update":
            if not tool_input.get("memory_id"):
                return "memory_id is required for 'update'"
            if not tool_input.get("title") and not tool_input.get("content"):
                return "title or content is required for 'update'"

        if action in ("delete", "read"):
            if not tool_input.get("memory_id"):
                return f"memory_id is required for '{action}'"

        if action == "search":
            query = tool_input.get("query")
            if not isinstance(query, str) or not query.strip():
                return "query is required for 'search'"
            limit = tool_input.get("limit", MEMORY_SEARCH_DEFAULT_LIMIT)
            if (
                not isinstance(limit, int)
                or isinstance(limit, bool)
                or limit < 1
                or limit > MEMORY_SEARCH_MAX_LIMIT
            ):
                return f"limit must be an integer between 1 and {MEMORY_SEARCH_MAX_LIMIT}"

        return None

    async def call(
        self,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        action = tool_input["action"]
        requested_scope = tool_input.get("scope")
        scope = requested_scope or "project"
        path = _memory_path(scope, context.cwd)
        memories = _load_memories(path)

        if action == "create":
            entry = {
                "id": uuid.uuid4().hex[:12],
                "title": tool_input["title"],
                "content": tool_input["content"],
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            memories.append(entry)
            _save_memories(path, memories)
            return ToolResult(
                data=entry,
                result_for_model=(
                    f"Memory created (id: {entry['id']}, scope: {scope}):\n"
                    f"  {entry['title']}"
                ),
            )

        if action == "update":
            memory_id = tool_input["memory_id"]
            target = next((m for m in memories if m.get("id") == memory_id), None)
            if not target:
                return ToolResult(
                    result_for_model=f"Error: memory '{memory_id}' not found in {scope} scope.",
                    is_error=True,
                )
            if tool_input.get("title"):
                target["title"] = tool_input["title"]
            if tool_input.get("content"):
                target["content"] = tool_input["content"]
            target["updated_at"] = datetime.now(timezone.utc).isoformat()
            _save_memories(path, memories)
            return ToolResult(
                data=target,
                result_for_model=(
                    f"Memory updated (id: {memory_id}, scope: {scope}):\n"
                    f"  {target['title']}"
                ),
            )

        if action == "delete":
            memory_id = tool_input["memory_id"]
            before = len(memories)
            memories = [m for m in memories if m.get("id") != memory_id]
            if len(memories) == before:
                return ToolResult(
                    result_for_model=f"Error: memory '{memory_id}' not found in {scope} scope.",
                    is_error=True,
                )
            _save_memories(path, memories)
            return ToolResult(
                result_for_model=f"Memory deleted (id: {memory_id}, scope: {scope}).",
            )

        if action == "list":
            all_memories = _scoped_memories(context.cwd, requested_scope)
            if not all_memories:
                return ToolResult(
                    result_for_model="No memories stored.",
                )
            directory = format_memory_directory(all_memories)
            return ToolResult(
                data={"count": len(all_memories)},
                result_for_model=(
                    f"{directory}\n"
                    "Use action='read' with a memory_id to retrieve full content."
                ),
            )

        if action == "search":
            query = tool_input["query"].strip()
            limit = tool_input.get("limit", MEMORY_SEARCH_DEFAULT_LIMIT)
            candidates = _scoped_memories(context.cwd, requested_scope)
            ranked = [
                (score, memory)
                for memory in candidates
                if (score := _search_score(memory, query)) > 0
            ]
            ranked.sort(
                key=lambda item: (item[0], _memory_sort_key(item[1])),
                reverse=True,
            )
            search_matches = ranked[:limit]
            if not search_matches:
                return ToolResult(
                    data={"count": 0, "matches": []},
                    result_for_model=f"No memories matched {query!r}.",
                )

            result_data: list[dict[str, Any]] = []
            lines: list[str] = []
            for _, memory in search_matches:
                memory_id = memory.get("id", "?")
                memory_scope = memory.get("_scope", "?")
                excerpt = _memory_excerpt(memory.get("content"), query)
                lines.append(
                    f"- [{memory_scope}] {memory_id}: {_memory_summary(memory)}"
                    + (f"\n  Match: {excerpt}" if excerpt else "")
                )
                result_data.append(
                    {
                        "id": memory_id,
                        "scope": memory_scope,
                        "title": _memory_summary(memory),
                        "excerpt": excerpt,
                    }
                )
            return ToolResult(
                data={"count": len(result_data), "matches": result_data},
                result_for_model=(
                    f"{len(result_data)} memory match(es) for {query!r}:\n"
                    + "\n".join(lines)
                    + "\nUse action='read' with a memory_id for the full content."
                ),
            )

        if action == "read":
            memory_id = tool_input["memory_id"]
            candidates = _scoped_memories(context.cwd, requested_scope)
            read_matches = [
                memory for memory in candidates if memory.get("id") == memory_id
            ]
            if not read_matches:
                scope_text = requested_scope or "project/global"
                return ToolResult(
                    result_for_model=(
                        f"Error: memory '{memory_id}' not found in {scope_text} scope."
                    ),
                    is_error=True,
                )
            if len(read_matches) > 1 and requested_scope is None:
                return ToolResult(
                    result_for_model=(
                        f"Error: memory id '{memory_id}' exists in multiple scopes; "
                        "specify scope='project' or scope='global'."
                    ),
                    is_error=True,
                )

            target_memory = read_matches[0]
            memory_scope = target_memory.get("_scope", "?")
            content = str(target_memory.get("content", ""))
            return ToolResult(
                data={
                    "id": memory_id,
                    "scope": memory_scope,
                    "title": _memory_summary(target_memory),
                    "content": content,
                    "created_at": target_memory.get("created_at"),
                    "updated_at": target_memory.get("updated_at"),
                },
                result_for_model=(
                    f"Memory [{memory_scope}] {memory_id}: "
                    f"{_memory_summary(target_memory)}\n"
                    f"{content}"
                ),
            )

        return ToolResult(
            result_for_model=f"Unknown action: {action}",
            is_error=True,
        )
