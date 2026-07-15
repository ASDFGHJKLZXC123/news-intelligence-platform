"""Health and metrics endpoints.

``/health`` verifies the four Stage 1 concerns — configuration, PostgreSQL, Redis, and
worker (Celery) configuration — via dependency functions and reports an aggregate
status. It returns 200 with a component report when healthy and 503 when any critical
component is degraded, so it doubles as a readiness probe. The dependency functions are
injected via FastAPI ``Depends`` so tests can override them without a live database or
broker.

The 503 is a non-2xx response, so it is rendered through the shared error envelope
(``{"error": {"code", "message", "request_id"}}``) like every other error — raising
``HTTPException`` routes it through the global handler, which stamps the correlation id
into the body and the ``X-Request-ID`` header. The degraded component diagnostics are
preserved in the envelope ``message`` rather than a bespoke top-level body.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from apps.api.deps import (
    ComponentStatus,
    check_config,
    check_database,
    check_redis,
    check_worker_config,
)
from packages.config import metrics
from packages.config.settings import get_settings

router = APIRouter()


@router.get("/health")
def health(
    config: Annotated[ComponentStatus, Depends(check_config)],
    database: Annotated[ComponentStatus, Depends(check_database)],
    redis_status: Annotated[ComponentStatus, Depends(check_redis)],
    worker: Annotated[ComponentStatus, Depends(check_worker_config)],
) -> JSONResponse:
    components = [config, database, redis_status, worker]
    settings = get_settings()

    if not all(c.ok for c in components):
        # Degraded readiness is a non-2xx response and must use the shared error
        # envelope. Raise so the global HTTPException handler renders it with the
        # correlation id in the body and X-Request-ID header; the per-component
        # diagnostics ride along in the message.
        degraded = "; ".join(f"{c.name}: {c.detail}" for c in components if not c.ok)
        raise HTTPException(status_code=503, detail=f"degraded components — {degraded}")

    body = {
        "status": "ok",
        "app": settings.app_name,
        "env": settings.app_env,
        "components": {c.name: c.as_dict() for c in components},
    }
    return JSONResponse(status_code=200, content=body)


@router.get("/metrics")
def get_metrics() -> dict:
    """Expose the Stage 1 in-process counters as JSON."""
    return metrics.snapshot()
