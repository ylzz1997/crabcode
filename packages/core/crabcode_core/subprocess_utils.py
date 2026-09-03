"""Cross-platform subprocess helpers."""

from __future__ import annotations

import asyncio
import codecs
import locale
import os
import shutil
import signal
import subprocess
from typing import Any


_POWERSHELL_UTF8_SETUP = (
    "$utf8 = [System.Text.UTF8Encoding]::new($false); "
    "[Console]::InputEncoding = $utf8; "
    "[Console]::OutputEncoding = $utf8; "
    "$OutputEncoding = $utf8; "
)


def resolve_executable_command(command: list[str]) -> list[str]:
    """Resolve the executable token, including Windows ``.cmd`` shims.

    Windows ``CreateProcess`` does not apply ``PATHEXT`` consistently for a
    bare command passed to ``subprocess``/``asyncio``. ``shutil.which`` does,
    so replacing the first token makes npm-installed launchers work without a
    shell and keeps argument handling safe.
    """
    if not command:
        raise ValueError("command must not be empty")
    if os.name != "nt":
        return list(command)
    resolved = shutil.which(command[0])
    return [resolved or command[0], *command[1:]]


def shell_command(command: str) -> list[str]:
    """Return an explicit platform shell invocation with UTF-8 output."""
    if os.name == "nt":
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        if powershell:
            return powershell_command(powershell, command)
        comspec = os.environ.get("COMSPEC") or "cmd.exe"
        return [comspec, "/d", "/s", "/c", f"chcp 65001>nul & {command}"]

    bash = shutil.which("bash")
    if bash:
        return [bash, "-c", command]
    return ["/bin/sh", "-c", command]


def powershell_command(executable: str, command: str) -> list[str]:
    """Build a non-interactive PowerShell command that emits UTF-8."""
    return [
        executable,
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        _POWERSHELL_UTF8_SETUP + command,
    ]


def subprocess_group_options() -> dict[str, Any]:
    """Options used for processes that may need tree-wide cancellation."""
    if os.name != "nt":
        return {"start_new_session": True}
    # CrabCode subprocesses are non-interactive and communicate through pipes.
    # Prevent console programs spawned by the GUI/Gateway from flashing a new
    # terminal window on Windows.
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return {"creationflags": creation_flags} if creation_flags else {}


def decode_subprocess_output(data: bytes) -> str:
    """Decode subprocess output without assuming every Windows tool uses UTF-8."""
    if not data:
        return ""
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16", errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        encodings = [locale.getpreferredencoding(False) or "utf-8"]
        if os.name == "nt":
            encodings.append("mbcs")
        for encoding in dict.fromkeys(encodings):
            try:
                return data.decode(encoding)
            except (LookupError, UnicodeDecodeError):
                continue
        return data.decode(encodings[-1], errors="replace")


def is_process_running(pid: int) -> bool:
    """Return whether *pid* identifies a live process without signalling it."""
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    access_denied = 5
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return ctypes.get_last_error() == access_denied
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


async def terminate_process_tree(process: Any, *, timeout: float = 5.0) -> None:
    """Terminate a subprocess and all descendants created beneath it."""
    if process is None or process.returncode is not None:
        return

    if os.name == "nt":
        taskkill = shutil.which("taskkill")
        tree_killed = False
        if taskkill:
            options: dict[str, Any] = {}
            if hasattr(subprocess, "CREATE_NO_WINDOW"):
                options["creationflags"] = subprocess.CREATE_NO_WINDOW
            try:
                killer = await asyncio.create_subprocess_exec(
                    taskkill,
                    "/PID",
                    str(process.pid),
                    "/T",
                    "/F",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    **options,
                )
                await killer.wait()
                tree_killed = killer.returncode == 0
            except OSError:
                pass
        if not tree_killed and process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        if process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=timeout)
                return
            except asyncio.TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
        await process.wait()
        return

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        try:
            process.terminate()
        except ProcessLookupError:
            return
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except OSError:
            try:
                process.kill()
            except ProcessLookupError:
                return
        await process.wait()
