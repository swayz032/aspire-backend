"""Structured JSON logging configuration for Aspire orchestrator.

Dev mode: human-readable text output (default).
Production mode: JSON lines with correlation_id, suitable for Railway log search.

Set ASPIRE_LOG_FORMAT=json to enable JSON logging.

Every log line includes:
- timestamp (ISO8601)
- level
- logger (module name)
- message
- correlation_id (from ContextVar, empty if outside request)
- Extra fields from the log call
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any


class CorrelationIdFilter(logging.Filter):
    """Inject correlation_id from ContextVar into every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        # Import here to avoid circular imports
        from aspire_orchestrator.middleware.correlation import get_correlation_id

        record.correlation_id = get_correlation_id() or ""  # type: ignore[attr-defined]
        return True


class JsonFormatter(logging.Formatter):
    """Compact JSON formatter for structured logging.

    Output: one JSON object per line, no pretty printing.
    Fields: timestamp, level, logger, message, correlation_id, + extras.
    """

    def format(self, record: logging.LogRecord) -> str:
        import json

        log_entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": getattr(record, "correlation_id", ""),
        }

        # Add extra fields (skip standard LogRecord attributes)
        _standard = {
            "name", "msg", "args", "created", "relativeCreated",
            "exc_info", "exc_text", "stack_info", "lineno", "funcName",
            "pathname", "filename", "module", "levelno", "levelname",
            "msecs", "thread", "threadName", "process", "processName",
            "taskName", "message", "correlation_id",
        }
        for key, value in record.__dict__.items():
            if key not in _standard and not key.startswith("_"):
                log_entry[key] = value

        # Add exception info if present
        if record.exc_info and record.exc_info[1]:
            log_entry["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_entry, default=str)


def configure_logging() -> None:
    """Configure logging for the entire application.

    Call once at server startup (before any other logging).

    Env vars:
        ASPIRE_LOG_FORMAT: "json" for JSON output, anything else for text (default).
        ASPIRE_LOG_LEVEL: DEBUG/INFO/WARNING/ERROR (default: INFO).
    """
    log_format = os.environ.get("ASPIRE_LOG_FORMAT", "text").lower()
    log_level = os.environ.get("ASPIRE_LOG_LEVEL", "INFO").upper()

    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level, logging.INFO))

    # Remove existing handlers to avoid duplicate output
    root_logger.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(CorrelationIdFilter())

    if log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        # Human-readable format for dev
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s "
                "[%(correlation_id)s] %(message)s",
                datefmt="%H:%M:%S",
            )
        )

    root_logger.addHandler(handler)

    # Quiet noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("hpack").setLevel(logging.WARNING)
