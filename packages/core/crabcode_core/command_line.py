"""Slash-command argument parsing, preserving Windows path separators."""

from __future__ import annotations

import os
import shlex


def split_command_arguments(value: str) -> list[str]:
    if os.name != "nt":
        return shlex.split(value)
    lexer = shlex.shlex(value, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    # Slash commands are not a shell: backslashes in paths are literal,
    # including a trailing separator inside quotes. Both quote styles work.
    lexer.escape = ""
    return list(lexer)
