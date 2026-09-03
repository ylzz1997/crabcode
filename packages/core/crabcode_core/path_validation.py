"""Validation helpers for portable filesystem path components."""

from __future__ import annotations

from pathlib import Path


_WINDOWS_INVALID_CHARS = frozenset('<>:"|?*')
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)


def validate_path_component(value: str, label: str) -> str:
    """Return a safe, cross-platform filename component or raise ValueError."""
    if not isinstance(value, str) or not value or value in {".", ".."}:
        raise ValueError(f"Invalid {label}")
    if Path(value).is_absolute() or "/" in value or "\\" in value:
        raise ValueError(f"Invalid {label}: path separators are not allowed")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"Invalid {label}: control characters are not allowed")
    invalid = sorted(set(value) & _WINDOWS_INVALID_CHARS)
    if invalid:
        raise ValueError(
            f"Invalid {label}: unsupported filename character(s): {''.join(invalid)}"
        )
    if value.endswith((" ", ".")):
        raise ValueError(f"Invalid {label}: trailing spaces and dots are not allowed")
    stem = value.split(".", 1)[0].rstrip(" .").upper()
    if stem in _WINDOWS_RESERVED_NAMES:
        raise ValueError(f"Invalid {label}: reserved Windows device name")
    return value
