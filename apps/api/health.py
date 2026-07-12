"""Health and metrics endpoints.

``/health`` verifies the four Stage 1 concerns — configuration, PostgreSQL, Redis, and
worker (Celery) configuration — via dependency functions and reports an aggregate
status. It returns 200 when healthy and 503 when any critical component is degraded,
so it doubles as a readiness probe. The dependency functions are injected via FastAPI
``Depends`` so tests can override them without a live database or broker.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
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

    overall_ok = all(c.ok for c in components)
    settings = get_settings()
    body = {
        "status": "ok" if overall_ok else "degraded",
        "app": settings.app_name,
        "env": settings.app_env,
        "components": {c.name: c.as_dict() for c in components},
    }
    return JSONResponse(status_code=200 if overall_ok else 503, content=body)


@router.get("/metrics")
def get_metrics() -> dict:
    """Expose the Stage 1 in-process counters as JSON."""
    return metrics.snapshot()
