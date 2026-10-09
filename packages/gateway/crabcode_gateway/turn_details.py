"""Restore recorded statistics and provide honest fallback for older histories."""

from typing import Any


def with_turn_details(messages: list[dict[str, Any]], session_id: str) -> list[dict[str, Any]]:
    result = [dict(message) for message in messages]
    current: list[dict[str, Any]] = []
    started_at: str | None = None

    def finish() -> None:
        assistants = [message for message in current if message.get("role") == "assistant"]
        if not assistants:
            return
        final = assistants[-1]
        if final.get("turn_details"):
            # A fork belongs to its new session, while retaining original timing.
            final["turn_details"] = {**final["turn_details"], "session_id": session_id}
            return
        tool_ids: set[str] = set()
        thinking_count = 0
        usage: dict[str, int] = {}
        for message in assistants:
            content = message.get("content")
            for index, block in enumerate(content if isinstance(content, list) else []):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    tool_ids.add(str(block.get("id") or f"{message.get('uuid')}:{index}"))
                elif block.get("type") == "thinking" and block.get("thinking"):
                    thinking_count += 1
            for key, value in (message.get("usage") or {}).items():
                if isinstance(value, int) and not isinstance(value, bool):
                    usage[key] = usage.get(key, 0) + value
        final["turn_details"] = {
            "session_id": session_id, "started_at": started_at,
            "tool_call_count": len(tool_ids), "thinking_count": thinking_count,
            "request_count": len(assistants), "usage": usage, "source": "history",
        }

    for message in result:
        content = message.get("content")
        tool_result = isinstance(content, list) and any(
            isinstance(block, dict) and block.get("type") == "tool_result" for block in content
        )
        starts_turn = message.get("role") == "user" and not tool_result and message.get("origin") not in {
            "user-steering", "document-action",
        }
        if starts_turn:
            finish()
            current = []
            started_at = message.get("timestamp") or None
        current.append(message)
        # Recorded boundaries also separate queued and synthetic turns.
        if message.get("turn_details"):
            message["turn_details"] = {**message["turn_details"], "session_id": session_id}
            current = []
            started_at = None
    finish()
    return result
