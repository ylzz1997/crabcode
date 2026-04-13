"""MemoryTool — persistent memory across conversations."""

from __future__ import annotations

import json
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
            return data
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


class MemoryTool(Tool):
    name = "Memory"
    description = "Create, update, or delete persistent memories for future reference."
    is_read_only = False
    is_concurrency_safe = False
    input_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "update", "delete", "list"],
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
                    "Required for 'update' and 'delete'."
                ),
            },
            "scope": {
                "type": "string",
                "enum": ["project", "global"],
                "description": (
                    "Where to store the memory. "
                    "'project' (default) stores in the current project, "
                    "'global' stores in ~/.crabcode/ for all projects."
                ),
            },
        },
        "required": ["action"],
    }

    async def get_prompt(self, **kwargs: Any) -> str:
        return (
            "Create, update, or delete persistent memories that survive across "
            "conversations. Memories are automatically loaded into context at "
            "the start of each session.\n\n"
            "Actions:\n"
            "- create: Store a new memory (requires title + content)\n"
            "- update: Modify an existing memory (requires memory_id + title + content)\n"
            "- delete: Remove a memory (requires memory_id)\n"
            "- list: Show all stored memories\n\n"
            "Scope:\n"
            "- 'project' (default): Stored in .crabcode/memories.json in the project root\n"
            "- 'global': Stored in ~/.crabcode/memories.json, available across all projects\n\n"
            "Guidelines:\n"
            "- Only create memories when the user explicitly asks to remember something.\n"
            "- If the user contradicts an existing memory, DELETE the old one rather than updating.\n"
            "- Keep memories concise — no more than a paragraph each.\n"
            "- Use 'project' scope for project-specific conventions and preferences.\n"
            "- Use 'global' scope for general user preferences that apply everywhere."
        )

    async def validate_input(self, tool_input: dict[str, Any]) -> str | None:
        action = tool_input.get("action")
        if action not in ("create", "update", "delete", "list"):
            return "action must be one of: create, update, delete, list"

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

        if action == "delete":
            if not tool_input.get("memory_id"):
                return "memory_id is required for 'delete'"

        return None

    async def call(
        self,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        action = tool_input["action"]
        scope = tool_input.get("scope", "project")
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
            target = next((m for m in memories if m["id"] == memory_id), None)
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
            memories = [m for m in memories if m["id"] != memory_id]
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
            all_memories = load_all_memories(context.cwd)
            if not all_memories:
                return ToolResult(
                    result_for_model="No memories stored.",
                )
            lines = []
            for m in all_memories:
                s = m.pop("_scope", "?")
                lines.append(
                    f"- [{s}] {m['id']}: {m['title']}\n  {m['content']}"
                )
            return ToolResult(
                data={"count": len(all_memories)},
                result_for_model=f"{len(all_memories)} memory(ies):\n" + "\n".join(lines),
            )

        return ToolResult(
            result_for_model=f"Unknown action: {action}",
            is_error=True,
        )
