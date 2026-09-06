"""Private stdio supervisor: keep a Windows process tree in a kill-on-close job.

The supervisor joins the job before starting the command, avoiding the race
where a short-lived launcher spawns children before it can be assigned a job.
Only stdlib imports are used; this file also runs with an isolated environment.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import subprocess
import sys


def attach_process_tree_job() -> int:
    """Own descendants until this process exits; keep the job handle open."""
    class BasicLimit(ctypes.Structure):
        _fields_ = [
            ("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
            ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
            ("max_working_set", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
            ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
            ("scheduling", wintypes.DWORD),
        ]

    class ExtendedLimit(ctypes.Structure):
        _fields_ = [
            ("basic", BasicLimit), ("io", ctypes.c_uint64 * 6),
            ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
            ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    job = kernel.CreateJobObjectW(None, None)  # Non-inheritable handle.
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = ExtendedLimit()
    limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        raise ctypes.WinError(ctypes.get_last_error())
    if not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess()):
        raise ctypes.WinError(ctypes.get_last_error())
    return job


def main() -> None:
    attach_process_tree_job()
    code = subprocess.call(
        sys.argv[1:], stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    # Process exit closes the sole job handle and reaps any surviving children.
    # Do not explicitly CloseHandle(job) while still running inside the job.
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.ExitProcess.argtypes = [wintypes.UINT]
    kernel.ExitProcess(code & 0xFFFFFFFF)


if __name__ == "__main__":
    main()
