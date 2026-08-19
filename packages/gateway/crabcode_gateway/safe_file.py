"""Bounded, race-resistant reads for Gateway-owned output files."""

from __future__ import annotations

import os
import stat
from pathlib import Path


_MAX_TAIL_BYTES = 8 * 1024 * 1024


def read_regular_file_tail(
    path: Path,
    *,
    max_lines: int,
    max_bytes: int = _MAX_TAIL_BYTES,
) -> tuple[list[str], bool]:
    """Read a bounded tail from one non-linked regular file.

    ``O_NOFOLLOW`` closes the final-component symlink race on platforms that
    support it.  The descriptor is then checked independently so a missing
    flag or a path replacement cannot turn a task-output endpoint into an
    arbitrary file reader.
    """
    target = Path(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(target, flags)
    try:
        descriptor_stat = os.fstat(fd)
        if not stat.S_ISREG(descriptor_stat.st_mode) or descriptor_stat.st_nlink != 1:
            raise OSError("output is not a dedicated regular file")
        path_stat = os.stat(target, follow_symlinks=False)
        if (
            not stat.S_ISREG(path_stat.st_mode)
            or path_stat.st_nlink != 1
            or (path_stat.st_dev, path_stat.st_ino)
            != (descriptor_stat.st_dev, descriptor_stat.st_ino)
        ):
            raise OSError("output file changed while opening")

        size = descriptor_stat.st_size
        byte_limit = max(1, int(max_bytes))
        start = max(0, size - byte_limit)
        os.lseek(fd, start, os.SEEK_SET)
        remaining = size - start
        chunks: list[bytes] = []
        while remaining > 0:
            chunk = os.read(fd, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        decoded = b"".join(chunks).decode("utf-8", errors="replace")
        lines = decoded.splitlines()
        line_limit = max(1, int(max_lines))
        truncated = start > 0 or len(lines) > line_limit
        return lines[-line_limit:], truncated
    finally:
        os.close(fd)


__all__ = ["read_regular_file_tail"]
