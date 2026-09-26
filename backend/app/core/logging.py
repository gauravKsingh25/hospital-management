"""Logging setup.

Human-readable console output locally; single-line JSON in deployed
environments so a log shipper can parse it without regexes.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

from app.core.config import settings

_NOISY_LOGGERS = (
    # SQLAlchemy's own engine logger duplicates DB_ECHO; keep one source.
    "sqlalchemy.engine",
    "asyncio",
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging() -> None:
    level = getattr(logging, settings.LOG_LEVEL, logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        JsonFormatter()
        if settings.LOG_JSON
        else logging.Formatter(
            "%(asctime)s %(levelname)-8s %(name)s : %(message)s",
            datefmt="%H:%M:%S",
        )
    )

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(max(level, logging.WARNING))

    # uvicorn installs its own handlers; make it use ours instead.
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
