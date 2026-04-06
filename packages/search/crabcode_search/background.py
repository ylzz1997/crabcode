"""Background prewarm/index process for CodebaseSearch."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any

# Must be set before ANY native library (FAISS / PyTorch) loads libomp.
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")

STATUS_FILE_NAME = "background-status.json"
PID_FILE_NAME = "background.pid"
LOG_FILE_NAME = "background.log"
CODEBASE_SEARCH_TOOL = "crabcode_search.CodebaseSearchTool"
BACKGROUND_LOGS_DIR = ".crabcode/logs"
BACKGROUND_LOG_INDEX = "index.json"
CODEBASE_SEARCH_LOG_KEY = "search"


def is_codebase_search_enabled(extra_tools: list[str]) -> bool:
    """Return True if CodebaseSearch is configured as an extra tool."""
    return CODEBASE_SEARCH_TOOL in extra_tools


STALE_TIMEOUT_SECONDS = 300


def maybe_spawn_background_indexer(
    cwd: str,
    tool_config: dict[str, Any] | None = None,
) -> bool:
    """Start a detached background indexer if one is not already running."""
    index_dir = Path(cwd).resolve() / ".crabcode" / "search"
    index_dir.mkdir(parents=True, exist_ok=True)

    pid_file = index_dir / PID_FILE_NAME
    status_file = index_dir / STATUS_FILE_NAME
    log_file = get_background_log_path(cwd, CODEBASE_SEARCH_LOG_KEY)

    existing_pid = _read_pid(pid_file)
    if existing_pid and _pid_is_running(existing_pid):
        if _is_stale(status_file):
            try:
                import signal
                os.kill(existing_pid, signal.SIGTERM)
            except OSError:
                pass
            pid_file.unlink(missing_ok=True)
        else:
            return False

    if pid_file.exists():
        pid_file.unlink(missing_ok=True)

    _write_status(
        status_file,
        {
            "state": "starting",
            "cwd": str(Path(cwd).resolve()),
            "pid": None,
        },
    )

    # Avoid `python -m crabcode_search.background` — the __main__ / package
    # double-init in `-m` mode breaks PyTorch MPS on macOS, causing
    # model.encode() to hang indefinitely.
    cmd = [
        sys.executable,
        "-c",
        "from crabcode_search.background import main; raise SystemExit(main())",
        "--cwd",
        str(Path(cwd).resolve()),
        "--tool-config-json",
        json.dumps(tool_config or {}, ensure_ascii=True),
    ]

    env = os.environ.copy()
    env["KMP_DUPLICATE_LIB_OK"] = "TRUE"
    env.setdefault("TOKENIZERS_PARALLELISM", "false")

    with open(log_file, "ab") as log:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            close_fds=True,
            env=env,
        )

    _write_status(
        status_file,
        {
            "state": "starting",
            "cwd": str(Path(cwd).resolve()),
            "pid": proc.pid,
        },
    )
    pid_file.write_text(str(proc.pid), encoding="utf-8")
    return True


def read_background_status(cwd: str) -> dict[str, Any] | None:
    """Read the current background indexer status from disk."""
    status_file = Path(cwd).resolve() / ".crabcode" / "search" / STATUS_FILE_NAME
    if not status_file.exists():
        return None
    try:
        data = json.loads(status_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def get_background_logs_dir(cwd: str) -> Path:
    """Return the directory that stores background tool logs."""
    logs_dir = Path(cwd).resolve() / BACKGROUND_LOGS_DIR
    logs_dir.mkdir(parents=True, exist_ok=True)
    return logs_dir


def get_background_log_path(cwd: str, key: str) -> Path:
    """Return the log path for a background tool key and register it."""
    logs_dir = get_background_logs_dir(cwd)
    log_path = logs_dir / f"{key}.log"
    _register_background_log(cwd, key, log_path)
    return log_path


def list_background_logs(cwd: str) -> dict[str, str]:
    """Return registered background logs as {key: absolute_path}."""
    index_path = get_background_logs_dir(cwd) / BACKGROUND_LOG_INDEX

    legacy = Path(cwd).resolve() / ".crabcode" / "search" / LOG_FILE_NAME
    if not index_path.exists() and legacy.exists():
        _register_background_log(cwd, CODEBASE_SEARCH_LOG_KEY, legacy)

    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}

    result: dict[str, str] = {}
    for key, value in data.items():
        if not (isinstance(key, str) and isinstance(value, str)):
            continue
        path = Path(value)
        if not path.exists() and key == CODEBASE_SEARCH_LOG_KEY and legacy.exists():
            result[key] = str(legacy)
            _register_background_log(cwd, key, legacy)
        else:
            result[key] = value
    return result


async def _run_background_indexer(cwd: str, tool_config: dict[str, Any]) -> int:
    from crabcode_search.embedder import create_embedder
    from crabcode_search.indexer import CodebaseIndexer

    threads = tool_config.get("threads")
    try:
        import torch

        n = int(threads) if threads else max(os.cpu_count() or 4, 4)
        torch.set_num_threads(n)
    except ImportError:
        pass

    index_dir = Path(cwd).resolve() / ".crabcode" / "search"
    status_file = index_dir / STATUS_FILE_NAME
    pid_file = index_dir / PID_FILE_NAME

    pid_file.write_text(str(os.getpid()), encoding="utf-8")

    _write_status(
        status_file,
        {
            "state": "preloading",
            "cwd": str(Path(cwd).resolve()),
            "pid": os.getpid(),
        },
    )

    try:
        # Force CPU in the background subprocess: MPS hangs when
        # stdout/stderr are redirected to files (no TTY / Metal context).
        device = tool_config.get("device") or "cpu"

        embedder = create_embedder(
            backend=tool_config.get("embedder", "ollama"),
            model=tool_config.get("model"),
            dimension=tool_config.get("dimension"),
            batch_size=tool_config.get("batch_size"),
            device=device,
        )
        await embedder.preload()

        _write_status(
            status_file,
            {
                "state": "scanning",
                "cwd": str(Path(cwd).resolve()),
                "pid": os.getpid(),
            },
        )

        indexer = CodebaseIndexer(cwd, embedder=embedder)
        all_files = indexer._scan_files()
        indexer.total_files = len(all_files)

        changed = indexer._detect_changes(all_files)

        if indexer.store.count > 0 and not changed:
            _write_status(
                status_file,
                {
                    "state": "ready",
                    "cwd": str(Path(cwd).resolve()),
                    "pid": os.getpid(),
                    "chunks": indexer.store.count,
                    "files": indexer.total_files,
                },
            )
            return 0

        _write_status(
            status_file,
            {
                "state": "indexing",
                "cwd": str(Path(cwd).resolve()),
                "pid": os.getpid(),
                "done": 0,
                "total": len(changed),
            },
        )

        async for progress in indexer.build_or_update():
            _write_status(
                status_file,
                {
                    "state": "indexing",
                    "cwd": str(Path(cwd).resolve()),
                    "pid": os.getpid(),
                    "done": progress.done,
                    "total": progress.total,
                },
            )

        _write_status(
            status_file,
            {
                "state": "ready",
                "cwd": str(Path(cwd).resolve()),
                "pid": os.getpid(),
                "chunks": indexer.store.count,
                "files": indexer.total_files,
            },
        )
        return 0
    except Exception as exc:
        _write_status(
            status_file,
            {
                "state": "error",
                "cwd": str(Path(cwd).resolve()),
                "pid": os.getpid(),
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
            },
        )
        return 1
    finally:
        current_pid = _read_pid(pid_file)
        if current_pid == os.getpid():
            pid_file.unlink(missing_ok=True)


def _read_pid(pid_file: Path) -> int | None:
    try:
        raw = pid_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _is_stale(status_file: Path) -> bool:
    """Return True if the status file hasn't been updated for too long."""
    try:
        mtime = status_file.stat().st_mtime
    except OSError:
        return True
    import time
    return (time.time() - mtime) > STALE_TIMEOUT_SECONDS


def _write_status(status_file: Path, payload: dict[str, Any]) -> None:
    status_file.parent.mkdir(parents=True, exist_ok=True)
    status_file.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _register_background_log(cwd: str, key: str, log_path: Path) -> None:
    index_path = get_background_logs_dir(cwd) / BACKGROUND_LOG_INDEX
    try:
        data = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {}
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data[key] = str(log_path.resolve())
    index_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Background prewarm/index process for CodebaseSearch.",
    )
    parser.add_argument("--cwd", required=True, help="Repository root to index")
    parser.add_argument(
        "--tool-config-json",
        default="{}",
        help="JSON object for tool_settings.CodebaseSearch",
    )
    args = parser.parse_args(argv)

    try:
        tool_config = json.loads(args.tool_config_json)
        if not isinstance(tool_config, dict):
            raise ValueError("tool config must be a JSON object")
    except Exception as exc:
        sys.stderr.write(f"Invalid --tool-config-json: {exc}\n")
        return 2

    return asyncio.run(_run_background_indexer(args.cwd, tool_config))


if __name__ == "__main__":
    raise SystemExit(main())
