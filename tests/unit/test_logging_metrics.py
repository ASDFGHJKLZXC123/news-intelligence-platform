"""Structured logging and metrics counter tests."""

from __future__ import annotations

import json
import logging

from packages.config import metrics
from packages.config.logging import JsonFormatter, set_job_id, set_request_id


def _format(record: logging.LogRecord) -> dict:
    return json.loads(JsonFormatter().format(record))


def test_json_log_includes_correlation_ids() -> None:
    set_request_id("req-123")
    set_job_id("job-456")
    try:
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="hello %s",
            args=("world",),
            exc_info=None,
        )
        payload = _format(record)
        assert payload["message"] == "hello world"
        assert payload["level"] == "INFO"
        assert payload["request_id"] == "req-123"
        assert payload["job_id"] == "job-456"
        assert "timestamp" in payload
    finally:
        set_request_id(None)
        set_job_id(None)


def test_json_log_includes_extra_fields() -> None:
    record = logging.LogRecord(
        name="test",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="msg",
        args=(),
        exc_info=None,
    )
    record.path = "/jobs/ingest"  # simulate logger.warning(..., extra={"path": ...})
    payload = _format(record)
    assert payload["path"] == "/jobs/ingest"


def test_metrics_counters() -> None:
    metrics.reset()
    metrics.increment(metrics.REQUESTS)
    metrics.increment(metrics.REQUESTS, 2)
    assert metrics.get(metrics.REQUESTS) == 3
    snap = metrics.snapshot()
    # snapshot always reports the four canonical counters.
    assert snap[metrics.REQUESTS] == 3
    assert snap[metrics.JOB_FAILURES] == 0
