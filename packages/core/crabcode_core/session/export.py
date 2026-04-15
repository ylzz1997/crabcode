"""Session export — convert session transcripts to Markdown or JSON."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from crabcode_core.logging_utils import get_logger
from crabcode_core.session.storage import SessionStorage

logger = get_logger(__name__)


def _format_ts(ts: Any) -> str:
    if isinstance(ts, (int, float)) and ts > 0:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    if isinstance(ts, str) and ts:
        return ts[:19]
    return "unknown"


def export_markdown(session_id: str, cwd: str) -> str:
    """Export a session transcript as Markdown."""
    storage = SessionStorage(cwd, session_id)
    raw_messages = storage.load_messages()
    meta = storage.meta

    if not raw_messages and not meta:
        cross = SessionStorage.from_session_id(session_id)
        if cross:
            storage = cross
            raw_messages = storage.load_messages()
            meta = storage.meta

    lines: list[str] = []

    title = meta.get("title", "") or f"Session {session_id[:8]}"
    lines.append(f"# {title}")
    lines.append("")

    header_parts: list[str] = []
    if meta.get("model"):
        header_parts.append(f"**Model:** {meta['model']}")
    if meta.get("provider"):
        header_parts.append(f"**Provider:** {meta['provider']}")
    if meta.get("created_at"):
        header_parts.append(f"**Created:** {_format_ts(meta['created_at'])}")
    if meta.get("cwd"):
        header_parts.append(f"**Project:** `{meta['cwd']}`")
    if meta.get("tokens_used"):
        header_parts.append(f"**Tokens:** {meta['tokens_used']:,}")
    if header_parts:
        lines.append(" | ".join(header_parts))
        lines.append("")
        lines.append("---")
        lines.append("")

    for raw in raw_messages:
        role = raw.get("type", "user")
        content = raw.get("content", "")
        timestamp = raw.get("timestamp", "")

        if role == "user":
            lines.append(f"## User")
        elif role == "assistant":
            lines.append(f"## Assistant")
        else:
            lines.append(f"## {role}")

        if timestamp:
            lines.append(f"*{_format_ts(timestamp)}*")
        lines.append("")

        text = _extract_text(content)
        tool_uses = _extract_tool_uses(content)

        if text:
            lines.append(text)
            lines.append("")

        for tool in tool_uses:
            lines.append(f"<details><summary>Tool: {tool['name']}</summary>")
            lines.append("")
            lines.append("```json")
            lines.append(json.dumps(tool.get("input", {}), indent=2, ensure_ascii=False))
            lines.append("```")
            lines.append("")
            lines.append("</details>")
            lines.append("")

    return "\n".join(lines)


def export_json(session_id: str, cwd: str) -> str:
    """Export a session transcript as formatted JSON."""
    storage = SessionStorage(cwd, session_id)
    raw_messages = storage.load_messages()
    meta = storage.meta

    if not raw_messages and not meta:
        cross = SessionStorage.from_session_id(session_id)
        if cross:
            storage = cross
            raw_messages = storage.load_messages()
            meta = storage.meta

    data = {
        "session_id": session_id,
        "meta": meta,
        "messages": raw_messages,
    }
    return json.dumps(data, indent=2, ensure_ascii=False)


def _extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text" and "text" in block:
                    parts.append(block["text"])
                elif block.get("type") == "thinking" and "thinking" in block:
                    parts.append(f"*Thinking: {block['thinking'][:200]}...*")
        return "\n\n".join(parts)
    return ""


def _extract_tool_uses(content: Any) -> list[dict[str, Any]]:
    if not isinstance(content, list):
        return []
    tools: list[dict[str, Any]] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            tools.append({
                "name": block.get("name", "unknown"),
                "input": block.get("input", {}),
            })
    return tools
