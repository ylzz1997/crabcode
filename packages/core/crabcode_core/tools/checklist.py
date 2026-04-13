"""ChecklistTool — per-session task tracking for long-running work."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from crabcode_core.types.tool import Tool, ToolContext, ToolResult


@dataclass
class ChecklistItem:
    text: str
    checked: bool = False


@dataclass
class Checklist:
    id: str
    title: str
    items: list[ChecklistItem] = field(default_factory=list)
    created_at: str = ""

    def render(self) -> str:
        lines = [f"  📋 {self.title}"]
        for i, item in enumerate(self.items, 1):
            mark = "✅" if item.checked else "◻"
            lines.append(f"  {mark} {i}. {item.text}")
        done = sum(1 for it in self.items if it.checked)
        total = len(self.items)
        lines.append(f"  ({done}/{total} completed)")
        return "\n".join(lines)


class ChecklistTool(Tool):
    name = "Checklist"
    description = "Manage per-session checklists for tracking progress on long tasks."
    is_read_only = False
    is_concurrency_safe = False
    input_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "update", "check", "uncheck", "list", "clear"],
                "description": "The action to perform.",
            },
            "title": {
                "type": "string",
                "description": "Title of the checklist. Required for 'create'.",
            },
            "items": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of item texts. Required for 'create'. Used for 'update' to add items.",
            },
            "checklist_id": {
                "type": "string",
                "description": "ID of an existing checklist. Required for 'update', 'check', 'uncheck'.",
            },
            "item": {
                "type": "integer",
                "description": "1-based index of the item to check/uncheck. Required for 'check' and 'uncheck'.",
            },
            "remove_items": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "1-based indices of items to remove. Used for 'update'.",
            },
        },
        "required": ["action"],
    }

    def __init__(self) -> None:
        self._checklists: dict[str, Checklist] = {}

    async def setup(self, context: ToolContext) -> None:
        self._checklists.clear()
        self._setup_context = context

    async def get_prompt(self, **kwargs: Any) -> str:
        return (
            "Manage per-session checklists to track progress on long or "
            "multi-step tasks. Checklists live only within the current "
            "session — they are not persisted to disk.\n\n"
            "Actions:\n"
            "- create: Start a new checklist with a title and items "
            "(requires title + items)\n"
            "- update: Modify a checklist — add/remove items or rename "
            "(requires checklist_id)\n"
            "- check: Mark an item as done (requires checklist_id + item index)\n"
            "- uncheck: Unmark an item (requires checklist_id + item index)\n"
            "- list: Show all checklists and their progress\n"
            "- clear: Remove one or all checklists\n\n"
            "When to use:\n"
            "- Before starting a long task, create a checklist to plan the steps\n"
            "- After completing each step, check it off\n"
            "- Use 'list' at any time to review progress\n\n"
            "Guidelines:\n"
            "- Use item indices (1-based) for check/uncheck, not text\n"
            "- Keep items concise — one actionable step per item\n"
            "- If you need to persist insights for future sessions, use the "
            "Memory tool instead"
        )

    async def validate_input(self, tool_input: dict[str, Any]) -> str | None:
        action = tool_input.get("action")
        if action not in ("create", "update", "check", "uncheck", "list", "clear"):
            return "action must be one of: create, update, check, uncheck, list, clear"

        if action == "create":
            if not tool_input.get("title"):
                return "title is required for 'create'"
            if not tool_input.get("items"):
                return "items is required for 'create'"

        if action in ("update", "check", "uncheck"):
            if not tool_input.get("checklist_id"):
                return "checklist_id is required for this action"

        if action in ("check", "uncheck"):
            if tool_input.get("item") is None:
                return "item (1-based index) is required for this action"

        return None

    def _find_checklist(self, checklist_id: str) -> Checklist | None:
        return self._checklists.get(checklist_id)

    async def call(
        self,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        action = tool_input["action"]

        if action == "create":
            return self._action_create(tool_input)
        if action == "update":
            return self._action_update(tool_input)
        if action == "check":
            return self._action_check(tool_input, checked=True)
        if action == "uncheck":
            return self._action_check(tool_input, checked=False)
        if action == "list":
            return self._action_list()
        if action == "clear":
            return self._action_clear(tool_input)

        return ToolResult(result_for_model=f"Unknown action: {action}", is_error=True)

    def _action_create(self, tool_input: dict[str, Any]) -> ToolResult:
        cl_id = uuid.uuid4().hex[:8]
        items = [ChecklistItem(text=t) for t in tool_input["items"]]
        cl = Checklist(
            id=cl_id,
            title=tool_input["title"],
            items=items,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self._checklists[cl_id] = cl
        return ToolResult(
            data={"checklist_id": cl_id, "item_count": len(items)},
            result_for_model=f"Checklist created (id: {cl_id}):\n{cl.render()}",
        )

    def _action_update(self, tool_input: dict[str, Any]) -> ToolResult:
        cl = self._find_checklist(tool_input["checklist_id"])
        if not cl:
            return ToolResult(
                result_for_model=f"Error: checklist '{tool_input['checklist_id']}' not found.",
                is_error=True,
            )

        if tool_input.get("title"):
            cl.title = tool_input["title"]

        # Remove items (process in reverse to keep indices stable)
        remove_indices = sorted(
            (i for i in tool_input.get("remove_items", []) if isinstance(i, int)),
            reverse=True,
        )
        for idx in remove_indices:
            if 1 <= idx <= len(cl.items):
                cl.items.pop(idx - 1)

        # Add items
        for text in tool_input.get("items", []):
            cl.items.append(ChecklistItem(text=text))

        return ToolResult(
            data={"checklist_id": cl.id, "item_count": len(cl.items)},
            result_for_model=f"Checklist updated (id: {cl.id}):\n{cl.render()}",
        )

    def _action_check(self, tool_input: dict[str, Any], checked: bool) -> ToolResult:
        cl = self._find_checklist(tool_input["checklist_id"])
        if not cl:
            return ToolResult(
                result_for_model=f"Error: checklist '{tool_input['checklist_id']}' not found.",
                is_error=True,
            )

        idx = tool_input["item"]
        if not isinstance(idx, int) or idx < 1 or idx > len(cl.items):
            return ToolResult(
                result_for_model=f"Error: item index {idx} out of range (1-{len(cl.items)}).",
                is_error=True,
            )

        cl.items[idx - 1].checked = checked
        verb = "checked" if checked else "unchecked"
        return ToolResult(
            data={"checklist_id": cl.id, "item": idx, verb: verb},
            result_for_model=f"Item {idx} {verb} (id: {cl.id}):\n{cl.render()}",
        )

    def _action_list(self) -> ToolResult:
        if not self._checklists:
            return ToolResult(result_for_model="No checklists.")
        parts = [cl.render() for cl in self._checklists.values()]
        return ToolResult(
            data={"count": len(self._checklists)},
            result_for_model=f"{len(self._checklists)} checklist(s):\n\n" + "\n\n".join(parts),
        )

    def _action_clear(self, tool_input: dict[str, Any]) -> ToolResult:
        cl_id = tool_input.get("checklist_id")
        if cl_id:
            if cl_id not in self._checklists:
                return ToolResult(
                    result_for_model=f"Error: checklist '{cl_id}' not found.",
                    is_error=True,
                )
            del self._checklists[cl_id]
            return ToolResult(result_for_model=f"Checklist '{cl_id}' cleared.")
        count = len(self._checklists)
        self._checklists.clear()
        return ToolResult(result_for_model=f"All {count} checklist(s) cleared.")
