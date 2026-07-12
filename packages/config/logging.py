"""Structured JSON logging with request-id and job-id correlation.

Log records are emitted as single-line JSON so they can be ingested by any log
pipeline. Correlation IDs (request id for HTTP, job id for Celery tasks) are carried
on context variables so they attach automatically to every record in scope.
"""

from __future__ import annotations

import contextvars
import datetime
import json
import logging
from typing import Any

# Context variables propagate correlation IDs without threading them through every call.
request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)
job_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("job_id", default=None)

# Reserved LogRecord attributes we never want to duplicate into the JSON "extra" block.
_RESERVED = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    """Render log records as compact JSON with correlation IDs and extras."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.datetime.fromtimestamp(
                record.created, tz=datetime.UTC
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        request_id = request_id_var.get()
        if request_id:
            payload["request_id"] = request_id
        job_id = job_id_var.get()
        if job_id:
            payload["job_id"] = job_id

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)

        # Surface any structured extras passed via logger.info(..., extra={...}).
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value

        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Install the JSON formatter on the root logger (idempotent)."""
    root = logging.getLogger()
    root.setLevel(level)
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    # Replace existing handlers so re-configuration (e.g. in tests) stays clean.
    root.handlers = [handler]


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def set_request_id(value: str | None) -> None:
    request_id_var.set(value)


def set_job_id(value: str | None) -> None:
    job_id_var.set(value)
