"""Context assembly — ported from src/context.ts.

Provides getSystemContext (git status etc.) and getUserContext (CLAUDE.md, date).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from datetime import date
from pathlib import Path
from typing import Any


async def _run_git(*args: str, cwd: str = ".") -> str:
    """Run a git command and return stdout, or empty string on failure."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        stdout, _ = await proc.communicate()
        return stdout.decode().strip()
    except Exception:
        return ""


async def _is_git_repo(cwd: str = ".") -> bool:
    result = await _run_git("rev-parse", "--is-inside-work-tree", cwd=cwd)
    return result == "true"


async def get_git_status(cwd: str = ".") -> str | None:
    """Build the git status context block."""
    is_git = await _is_git_repo(cwd)
    if not is_git:
        return None

    try:
        branch, main_branch, status, log, user_name = await asyncio.gather(
            _run_git("branch", "--show-current", cwd=cwd),
            _run_git("rev-parse", "--abbrev-ref", "origin/HEAD", cwd=cwd),
            _run_git("--no-optional-locks", "status", "--short", cwd=cwd),
            _run_git("--no-optional-locks", "log", "--oneline", "-n", "5", cwd=cwd),
            _run_git("config", "user.name", cwd=cwd),
        )

        main_branch = main_branch.replace("origin/", "") if main_branch else "main"

        max_chars = 2000
        if len(status) > max_chars:
            status = (
                status[:max_chars]
                + '\n... (truncated because it exceeds 2k characters. '
                'If you need more information, run "git status" using BashTool)'
            )

        parts = [
            "This is the git status at the start of the conversation. Note that this status is a snapshot in time, and will not update during the conversation.",
            f"Current branch: {branch}",
            f"Main branch (you will usually use this for PRs): {main_branch}",
        ]
        if user_name:
            parts.append(f"Git user: {user_name}")
        parts.append(f"Status:\n{status or '(clean)'}")
        parts.append(f"Recent commits:\n{log}")

        return "\n\n".join(parts)
    except Exception:
        return None


def get_system_context(cwd: str = ".") -> dict[str, str]:
    """Build system context (appended to system prompt).

    Synchronous wrapper — runs git commands in event loop if available.
    For the initial version, we do a simplified synchronous git check.
    """
    context: dict[str, str] = {}

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, cwd=cwd, timeout=5,
        )
        is_git = result.stdout.strip() == "true"
    except Exception:
        is_git = False

    if is_git:
        try:
            parts = []
            parts.append(
                "This is the git status at the start of the conversation. "
                "Note that this status is a snapshot in time, and will not "
                "update during the conversation."
            )

            branch = subprocess.run(
                ["git", "branch", "--show-current"],
                capture_output=True, text=True, cwd=cwd, timeout=5,
            ).stdout.strip()
            parts.append(f"Current branch: {branch}")

            status = subprocess.run(
                ["git", "--no-optional-locks", "status", "--short"],
                capture_output=True, text=True, cwd=cwd, timeout=5,
            ).stdout.strip()
            parts.append(f"Status:\n{status or '(clean)'}")

            log = subprocess.run(
                ["git", "--no-optional-locks", "log", "--oneline", "-n", "5"],
                capture_output=True, text=True, cwd=cwd, timeout=5,
            ).stdout.strip()
            parts.append(f"Recent commits:\n{log}")

            context["gitStatus"] = "\n\n".join(parts)
        except Exception:
            pass

    return context


def get_user_context(cwd: str = ".") -> dict[str, str]:
    """Build user context (prepended as first user message).

    Contains CLAUDE.md content, persistent memories, and current date.
    """
    context: dict[str, str] = {}

    context["currentDate"] = f"Today's date is {date.today().isoformat()}."

    claude_md = _load_claude_md(cwd)
    if claude_md:
        context["claudeMd"] = claude_md

    memories_text = _load_memories_context(cwd)
    if memories_text:
        context["memories"] = memories_text

    return context


def _load_claude_md(cwd: str) -> str | None:
    """Discover and load CLAUDE.md files from project hierarchy."""
    contents: list[str] = []

    search_dir = Path(cwd).resolve()
    home = Path.home()

    home_claude_md = home / ".claude" / "CLAUDE.md"
    if home_claude_md.exists():
        try:
            contents.append(home_claude_md.read_text(errors="replace"))
        except Exception:
            pass

    crabcode_md = home / ".crabcode" / "CLAUDE.md"
    if crabcode_md.exists():
        try:
            contents.append(crabcode_md.read_text(errors="replace"))
        except Exception:
            pass

    current = search_dir
    project_files: list[str] = []
    while current != current.parent:
        for name in ["CLAUDE.md", ".claude/CLAUDE.md"]:
            candidate = current / name
            if candidate.exists():
                try:
                    project_files.append(candidate.read_text(errors="replace"))
                except Exception:
                    pass
        if (current / ".git").exists():
            break
        current = current.parent

    contents.extend(reversed(project_files))

    if not contents:
        return None
    return "\n\n---\n\n".join(contents)


def _load_memories_context(cwd: str) -> str | None:
    """Load persistent memories and format them for context injection."""
    try:
        from crabcode_core.tools.memory import load_all_memories
    except ImportError:
        return None

    memories = load_all_memories(cwd)
    if not memories:
        return None

    lines = []
    for m in memories:
        scope = m.pop("_scope", "?")
        lines.append(f"- [{scope}] {m['title']}: {m['content']}")

    return (
        "The following memories were saved from previous conversations. "
        "They may or may not be relevant to the current task.\n\n"
        + "\n".join(lines)
    )
