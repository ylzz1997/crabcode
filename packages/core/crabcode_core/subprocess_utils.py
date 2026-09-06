"""Cross-platform subprocess helpers."""

from __future__ import annotations

import asyncio
import codecs
import locale
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
from typing import Any


_POWERSHELL_UTF8_SETUP = (
    "$utf8 = [System.Text.UTF8Encoding]::new($false); "
    "[Console]::InputEncoding = $utf8; "
    "[Console]::OutputEncoding = $utf8; "
    "$OutputEncoding = $utf8; "
)


def resolve_executable_command(
    command: list[str], *, env: dict[str, str] | None = None, cwd: str | None = None,
) -> list[str]:
    """Resolve using the launch environment, without interpreting batch arguments."""
    if not command:
        raise ValueError("command must not be empty")
    if os.name != "nt":
        return list(command)
    environment = {key.upper(): value for key, value in (env if env is not None else os.environ).items()}
    search_path = environment.get("PATH", "")
    base = Path(cwd or os.getcwd()).resolve()
    search_path = os.pathsep.join(str(base / part.strip('"')) for part in search_path.split(os.pathsep))
    token = command[0]
    if os.path.dirname(token):
        token = str(base / token)
    resolved = shutil.which(token, path=search_path)
    # which() takes PATHEXT from the parent process, not its path argument.
    if env is not None:
        roots = [Path()] if os.path.dirname(token) else [Path(p) for p in search_path.split(os.pathsep)]
        extensions = environment.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";")
        names = [token] if Path(token).suffix.upper() in {e.upper() for e in extensions} else [token + ext for ext in extensions]
        resolved = next((str(root / name) for root in roots for name in names if (root / name).is_file()), None)
    if not resolved:
        raise FileNotFoundError(f"Executable not found in the launch environment: {command[0]}")
    result = [resolved, *command[1:]]
    if Path(result[0]).suffix.lower() in {".cmd", ".bat"}:
        # npm's cmd-shim template has a fixed Node invocation. Bypass cmd.exe
        # for this template so filenames such as a&b.js remain literal argv.
        shim = Path(result[0])
        try:
            body = shim.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            body = ""
        match = re.fullmatch(
            r'@ECHO off\s+GOTO start\s+:find_dp0\s+SET dp0=%~dp0\s+EXIT /b\s+'
            r':start\s+SETLOCAL\s+CALL :find_dp0\s+'
            r'IF EXIST "%dp0%\\node.exe" \(\s+SET "_prog=%dp0%\\node.exe"\s+'
            r'\) ELSE \(\s+SET "_prog=node"\s+SET PATHEXT=%PATHEXT:;.JS;=;%\s+\)\s+'
            r'endLocal & goto #_undefined_# 2>NUL \|\| title %COMSPEC% & "%_prog%"\s+'
            r'"%dp0%[\\/]([^"\r\n]+)" %\*\s*', body,
        )
        if match:
            script = shim.parent / match.group(1).replace("\\", "/")
            node = shim.parent / "node.exe"
            executable = str(node) if node.is_file() else shutil.which("node", path=search_path)
            if executable and script.is_file():
                return [executable, str(script), *command[1:]]
        if any(re.search(r'[&|<>^%!"()\r\n]', value) for value in result):
            raise ValueError(
                "Cannot safely pass shell metacharacters to a Windows batch launcher. "
                "Configure the native executable (for Node tools: node.exe and its JS entry point)."
            )
    return result


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


def managed_process_command(command: list[str]) -> list[str]:
    """Wrap a Windows command in a job whose lifetime includes all descendants."""
    if os.name != "nt":
        return command
    return [sys.executable, "-I", str(Path(__file__).with_name("_windows_process.py")), *command]


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
