"""Shared runtime logging configuration for CrabCode."""

from __future__ import annotations

import json
import logging
import copy
import queue
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from crabcode_core.paths import get_config_home

if TYPE_CHECKING:
    from crabcode_core.types.config import LoggingSettings


LOG_NAMESPACE = "crabcode"
LOG_KEY = "crabcode"
LOG_FILE_NAME = "crabcode.log"
LOG_INDEX_NAME = "index.json"
LOGS_DIR_NAME = ".crabcode/logs"

_configured_signature: tuple[str, str, str] | None = None


class _AsyncFileHandler(logging.Handler):
    """Keep disk writes and traceback source lookups off application threads.

    A stalled sink has bounded memory and a daemon owner. It must never hold
    the logging handler lock or make logging.shutdown wait for remote storage.
    """

    def __init__(self, path: Path) -> None:
        super().__init__()
        self._path = path
        self._records: queue.Queue[logging.LogRecord | None] = queue.Queue(maxsize=1024)
        self._stopped = threading.Event()
        self._writer = threading.Thread(target=self._write, name="crabcode-log-writer", daemon=True)
        self._writer.start()

    def emit(self, record: logging.LogRecord) -> None:
        if self._stopped.is_set():
            return
        try:
            # Formatting exceptions can read source files; do it in the writer.
            self._records.put_nowait(copy.copy(record))
        except queue.Full:
            pass

    def _write(self) -> None:
        try:
            with open(self._path, "a", encoding="utf-8") as stream:
                while True:
                    try:
                        record = self._records.get(timeout=0.1)
                    except queue.Empty:
                        if self._stopped.is_set():
                            break
                        continue
                    if record is None:
                        break
                    stream.write(self.format(record) + "\n")
                    stream.flush()
        except Exception:
            # Do not recursively log a failed/full logging sink.
            self._stopped.set()

    def close(self) -> None:
        self._stopped.set()
        try:
            self._records.put_nowait(None)
        except queue.Full:
            pass
        if threading.current_thread() is not self._writer:
            self._writer.join(timeout=0.2)
        super().close()


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a logger under the shared CrabCode namespace."""
    if not name:
        return logging.getLogger(LOG_NAMESPACE)
    if name.startswith(f"{LOG_NAMESPACE}."):
        return logging.getLogger(name)
    return logging.getLogger(f"{LOG_NAMESPACE}.{name}")


def get_logs_dir(cwd: str) -> Path:
    """Return the per-project logs directory."""
    logs_dir = Path(cwd).resolve() / LOGS_DIR_NAME
    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        # cwd may be read-only (e.g. "/" on macOS) — fall back to config home
        logs_dir = get_config_home() / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
    return logs_dir


def get_log_path(cwd: str, settings: LoggingSettings | None = None) -> Path:
    """Resolve the main CrabCode log path."""
    if settings and settings.file:
        configured = Path(settings.file)
        if not configured.is_absolute():
            configured = Path(cwd).resolve() / configured
        try:
            configured.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            configured = get_config_home() / "logs" / configured.name
            configured.parent.mkdir(parents=True, exist_ok=True)
        return configured
    return get_logs_dir(cwd) / LOG_FILE_NAME


def configure_logging(cwd: str, settings: LoggingSettings | None = None) -> Path:
    """Configure the shared CrabCode logger to write to a project log file."""
    global _configured_signature

    from crabcode_core.types.config import LoggingSettings

    logging_settings = settings or LoggingSettings()
    level_name = logging_settings.level.upper()
    log_path = get_log_path(cwd, logging_settings).resolve()
    signature = (str(Path(cwd).resolve()), level_name, str(log_path))
    logger = logging.getLogger(LOG_NAMESPACE)

    if _configured_signature == signature and logger.handlers:
        _register_log(cwd, LOG_KEY, log_path)
        return log_path

    for old_handler in logger.handlers[:]:
        logger.removeHandler(old_handler)
        old_handler.close()
    logger.setLevel(getattr(logging, level_name, logging.WARNING))
    logger.propagate = False

    handler = _AsyncFileHandler(log_path)
    handler.setLevel(logger.level)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logger.addHandler(handler)

    _configured_signature = signature
    _register_log(cwd, LOG_KEY, log_path)
    logger.debug("Logging configured: level=%s path=%s", level_name, log_path)
    return log_path


def _register_log(cwd: str, key: str, log_path: Path) -> None:
    """Register a log file so the CLI /logs command can discover it."""
    index_path = get_logs_dir(cwd) / LOG_INDEX_NAME
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
