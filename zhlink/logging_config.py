"""Centralised logging helpers for zhlink.

Errors are written in both plain text (logs/zhlink.log) and as structured
JSON Lines (logs/errors.jsonl) so they can later be replayed into regression
tests without leaking private keys.
"""

from __future__ import annotations

import json
import logging
import re
import traceback
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, Optional

MAX_BYTES = 10 * 1024 * 1024  # 10 MiB
BACKUP_COUNT = 5

_SENSITIVE_KEYS_RE = re.compile(
    r"(private_key|priv_key|privatekey|wif|secret|password|token|api_key|apikey)",
    re.IGNORECASE,
)


def _sanitize(value: Any) -> Any:
    """Remove private-key-like values from a log context."""
    if isinstance(value, dict):
        return {k: "***" if _SENSITIVE_KEYS_RE.search(str(k)) else _sanitize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize(v) for v in value]
    if isinstance(value, str) and len(value) >= 48:
        # Long strings that look like WIFs or hex private keys.
        if value.startswith(("L", "K", "5", "Q")) and 51 <= len(value) <= 64:
            return "***"
        if re.fullmatch(r"[0-9a-fA-F]{64,}", value):
            return "***"
    return value


def _ensure_log_dir(log_dir: str | Path) -> Path:
    path = Path(log_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


class JsonErrorHandler(logging.FileHandler):
    """Append errors as JSON Lines for later regression-test generation."""

    def __init__(self, filename: str | Path) -> None:
        super().__init__(filename, mode="a", encoding="utf-8", delay=False)

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < logging.ERROR:
            return
        entry: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            entry["exception"] = traceback.format_exception(*record.exc_info)
        if hasattr(record, "context"):
            entry["context"] = _sanitize(getattr(record, "context"))
        try:
            self.stream.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
            self.flush()
        except Exception:
            self.handleError(record)


def setup_logging(
    log_dir: str | Path,
    level: int = logging.INFO,
    json_errors: bool = True,
    console: bool = True,
) -> None:
    """Configure logging for zhlink.

    - ``<log_dir>/zhlink.log`` — rotating plain-text log.
    - ``<log_dir>/errors.jsonl`` — structured errors for regression tests.
    """
    log_path = _ensure_log_dir(log_dir)

    root_logger = logging.getLogger("zhlink")
    root_logger.setLevel(level)
    # Reset handlers on repeated calls so applications can reconfigure logging.
    for handler in list(root_logger.handlers):
        try:
            handler.close()
        except Exception:
            pass
        root_logger.removeHandler(handler)

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s:%(funcName)s — %(message)s"
    )

    if console:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)

    app_log = log_path / "zhlink.log"
    file_handler = RotatingFileHandler(
        app_log, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    if json_errors:
        error_log = log_path / "errors.jsonl"
        json_handler = JsonErrorHandler(error_log)
        json_handler.setLevel(logging.ERROR)
        root_logger.addHandler(json_handler)


def log_error(
    logger: logging.Logger,
    exception: BaseException,
    context: Optional[Dict[str, Any]] = None,
) -> None:
    """Log an exception with sanitised context for regression tests."""
    extra: Dict[str, Any] = {"context": _sanitize(context or {})}
    logger.error(
        "Operation failed: %s",
        exception,
        exc_info=exception,
        extra=extra,
    )
