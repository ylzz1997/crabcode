"""CLAUDE.md discovery — finds and loads CLAUDE.md files from the project hierarchy."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from crabcode_core.logging_utils import get_logger

logger = get_logger(__name__)


def discover_claude_md(cwd: str) -> list[dict[str, str]]:
    """Discover all CLAUDE.md files in the project hierarchy.

    Returns list of dicts with 'path' and 'content' keys,
    ordered from most general (home) to most specific (project root).
    """
    results: list[dict[str, str]] = []
    home = Path.home()

    for config_dir in [".claude", ".crabcode"]:
        home_md = home / config_dir / "CLAUDE.md"
        if home_md.exists():
            try:
                content = home_md.read_text(errors="replace")
                results.append({"path": str(home_md), "content": content})
            except Exception:
                logger.warning("Failed to read %s", home_md, exc_info=True)

    search_dir = Path(cwd).resolve()
    project_files: list[dict[str, str]] = []

    current = search_dir
    while current != current.parent:
        for name in ["CLAUDE.md", ".claude/CLAUDE.md", ".crabcode/CLAUDE.md"]:
            candidate = current / name
            if candidate.exists():
                try:
                    content = candidate.read_text(errors="replace")
                    project_files.append({"path": str(candidate), "content": content})
                except Exception:
                    logger.warning("Failed to read %s", candidate, exc_info=True)
        if (current / ".git").exists():
            break
        current = current.parent

    results.extend(reversed(project_files))
    return results


def load_claude_md_as_string(cwd: str) -> str | None:
    """Load all CLAUDE.md files and join them into a single string."""
    files = discover_claude_md(cwd)
    if not files:
        return None
    return "\n\n---\n\n".join(f["content"] for f in files)
