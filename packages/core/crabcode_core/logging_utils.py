"""Shared runtime logging configuration for CrabCode."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from crabcode_core.types.config import LoggingSettings


LOG_NAMESPACE = "crabcode"
LOG_KEY = "crabcode"
LOG_FILE_NAME = "crabcode.log"
LOG_INDEX_NAME = "index.json"
LOGS_DIR_NAME = ".crabcode/logs"

_configured_signature: tuple[str, str, str] | None = None


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
    logs_dir.mkdir(parents=True, exist_ok=True)
    return logs_dir


def get_log_path(cwd: str, settings: LoggingSettings | None = None) -> Path:
    """Resolve the main CrabCode log path."""
    if settings and settings.file:
        configured = Path(settings.file)
        if not configured.is_absolute():
            configured = Path(cwd).resolve() / configured
        configured.parent.mkdir(parents=True, exist_ok=True)
        return configured
    return get_logs_dir(cwd) / LOG_FILE_NAME


def configure_logging(cwd: str, settings: LoggingSettings | None = None) -> Path:
    """Configure the shared CrabCode logger to write to a project log file."""
    global _configured_signature

    logging_settings = settings or LoggingSettings()
    level_name = logging_settings.level.upper()
    log_path = get_log_path(cwd, logging_settings).resolve()
    signature = (str(Path(cwd).resolve()), level_name, str(log_path))
    logger = logging.getLogger(LOG_NAMESPACE)

    if _configured_signature == signature and logger.handlers:
        _register_log(cwd, LOG_KEY, log_path)
        return log_path

    logger.handlers.clear()
    logger.setLevel(getattr(logging, level_name, logging.WARNING))
    logger.propagate = False

    handler = logging.FileHandler(log_path, encoding="utf-8")
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
