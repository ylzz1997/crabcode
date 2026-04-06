"""Diff utilities for file edit/write tools."""

from __future__ import annotations

import difflib


def compute_diff(
    old: str,
    new: str,
    filepath: str = "",
    context_lines: int = 3,
) -> dict:
    """Compute a structured diff between old and new file content.

    Returns a dict with:
      line_range  - (start, end) 1-indexed line range of first change
      diff_text   - unified diff string (compact, for model)
      display     - formatted diff string (for terminal display)
      stats       - {"added": N, "removed": N}
    """
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)

    diff_lines = list(difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=filepath,
        tofile=filepath,
        lineterm="",
        n=context_lines,
    ))

    if not diff_lines:
        return {
            "line_range": None,
            "diff_text": "",
            "display": "",
            "stats": {"added": 0, "removed": 0},
        }

    added = sum(1 for l in diff_lines if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in diff_lines if l.startswith("-") and not l.startswith("---"))

    first_change_line = _find_first_change_line(old_lines, new_lines)
    last_change_line = _find_last_change_line(old_lines, new_lines)

    diff_text = "\n".join(diff_lines)

    max_diff_chars = 10_000
    if len(diff_text) > max_diff_chars:
        diff_text = diff_text[:max_diff_chars] + "\n... (diff truncated)"

    return {
        "line_range": (first_change_line, last_change_line),
        "diff_text": diff_text,
        "display": diff_text,
        "stats": {"added": added, "removed": removed},
    }


def _find_first_change_line(old: list[str], new: list[str]) -> int:
    """Find the 1-indexed line number of the first difference."""
    for i, (a, b) in enumerate(zip(old, new)):
        if a != b:
            return i + 1
    return min(len(old), len(new)) + 1


def _find_last_change_line(old: list[str], new: list[str]) -> int:
    """Find the 1-indexed line number (in new file) of the last difference."""
    old_rev = list(reversed(old))
    new_rev = list(reversed(new))
    tail_common = 0
    for a, b in zip(old_rev, new_rev):
        if a != b:
            break
        tail_common += 1
    return max(1, len(new) - tail_common)


def format_edit_summary(
    filepath: str,
    diff_info: dict,
    replacements: int = 1,
) -> tuple[str, str]:
    """Format diff info into (result_for_model, result_for_display).

    Returns a pair of strings optimized for model consumption and
    terminal display respectively.
    """
    lr = diff_info["line_range"]
    stats = diff_info["stats"]

    if lr:
        line_desc = f"lines {lr[0]}-{lr[1]}" if lr[0] != lr[1] else f"line {lr[0]}"
    else:
        line_desc = ""

    stat_parts = []
    if stats["added"]:
        stat_parts.append(f"+{stats['added']}")
    if stats["removed"]:
        stat_parts.append(f"-{stats['removed']}")
    stat_str = ", ".join(stat_parts)

    model_msg = f"Updated {filepath}"
    if line_desc:
        model_msg += f" ({line_desc})"
    if stat_str:
        model_msg += f" [{stat_str}]"
    if replacements > 1:
        model_msg += f" ({replacements} replacements)"

    display_parts = [model_msg]
    if diff_info["display"]:
        display_parts.append(diff_info["display"])
    display_msg = "\n".join(display_parts)

    return model_msg, display_msg
