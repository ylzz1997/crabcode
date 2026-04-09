"""Normalize text for UTF-8 I/O (JSON, HTTP, terminals).

Python allows lone surrogate code units in ``str``; strict UTF-8 encoders
(API clients, ``sys.stdout`` on UTF-8, JSON lines) reject them.
"""

from __future__ import annotations

from typing import Any


def safe_utf8_str(s: str) -> str:
    """Replace lone surrogates so the string encodes as UTF-8."""
    if not s:
        return s
    return s.encode("utf-8", errors="surrogatepass").decode("utf-8", errors="replace")


def safe_utf8_json_tree(obj: Any) -> Any:
    """Recursively sanitize strings for JSON or API payloads."""
    if isinstance(obj, str):
        return safe_utf8_str(obj)
    if isinstance(obj, dict):
        return {k: safe_utf8_json_tree(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [safe_utf8_json_tree(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(safe_utf8_json_tree(v) for v in obj)
    return obj
