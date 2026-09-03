"""Cross-platform advisory file locking for cooperating CrabCode processes."""

from __future__ import annotations

import errno
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

try:  # POSIX whole-file locking.
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover - exercised only on Windows
    fcntl = None  # type: ignore[assignment]

try:  # Windows byte-range locking.
    import msvcrt  # type: ignore
except ImportError:  # pragma: no cover - exercised only on POSIX
    msvcrt = None  # type: ignore[assignment]


_LOCK_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[str, threading.Lock] = {}
_WINDOWS_LOCK_RETRY_SECONDS = 0.05


def _process_lock(path: Path) -> threading.Lock:
    key = os.path.normcase(str(path.resolve()))
    with _LOCK_GUARD:
        return _PROCESS_LOCKS.setdefault(key, threading.Lock())


def _lock_windows_byte(lock_file: object, *, exclusive: bool) -> None:
    """Acquire byte zero until it is available instead of timing out after 10s."""
    if msvcrt is None:  # pragma: no cover - guarded by the caller
        return
    handle = lock_file  # Keep the file-like protocol local for type checkers.
    handle.seek(0, os.SEEK_END)  # type: ignore[attr-defined]
    if handle.tell() == 0:  # type: ignore[attr-defined]
        handle.write(b"\0")  # type: ignore[attr-defined]
        handle.flush()  # type: ignore[attr-defined]
    mode = msvcrt.LK_NBLCK if exclusive else msvcrt.LK_NBRLCK
    while True:
        handle.seek(0)  # type: ignore[attr-defined]
        try:
            msvcrt.locking(handle.fileno(), mode, 1)  # type: ignore[attr-defined]
            return
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise
            time.sleep(_WINDOWS_LOCK_RETRY_SECONDS)


@contextmanager
def file_lock(path: Path, *, exclusive: bool = True) -> Iterator[None]:
    """Lock *path* across threads and processes until the context exits.

    All callers must use the same sidecar path. POSIX uses ``flock`` while
    Windows locks byte zero with ``msvcrt``. The in-process lock also prevents
    same-process threads from racing on platforms whose OS lock semantics are
    process- or handle-oriented.
    """
    thread_lock = _process_lock(path)
    with thread_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a+b") as lock_file:
            if fcntl is not None:
                mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
                fcntl.flock(lock_file.fileno(), mode)
            elif msvcrt is not None:
                _lock_windows_byte(lock_file, exclusive=exclusive)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                elif msvcrt is not None:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
