"""Dependency functions used by the API, primarily for the /health check.

Each check returns a ``ComponentStatus`` rather than raising, so the health endpoint
can report a degraded component without returning a 500. This keeps /health usable as
a liveness/readiness probe even when a dependency is down.
"""

from __future__ import annotations

from dataclasses import dataclass

from packages.config.settings import get_settings


@dataclass(frozen=True)
class ComponentStatus:
    name: str
    ok: bool
    detail: str = ""

    def as_dict(self) -> dict:
        return {"ok": self.ok, "detail": self.detail}


def check_database() -> ComponentStatus:
    """Verify PostgreSQL connectivity with a trivial ``SELECT 1``.

    The engine is imported lazily inside the ``try`` so a bad ``DATABASE_URL`` (which
    can raise when the engine is constructed) surfaces as a degraded status rather than
    an import-time error.
    """
    try:
        from sqlalchemy import text

        from db.base import engine

        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return ComponentStatus(name="database", ok=True, detail="reachable")
    except Exception as exc:  # noqa: BLE001 - report any failure as degraded
        return ComponentStatus(name="database", ok=False, detail=f"{type(exc).__name__}")


def check_redis() -> ComponentStatus:
    """Verify Redis connectivity with PING.

    The client is created lazily inside the ``try`` so creation, ping, and close
    failures all degrade the status instead of raising. The client is closed only if it
    was successfully created.
    """
    client = None
    try:
        import redis

        settings = get_settings()
        client = redis.Redis.from_url(settings.redis_url, socket_connect_timeout=2)
        client.ping()
        client.close()
        return ComponentStatus(name="redis", ok=True, detail="reachable")
    except Exception as exc:  # noqa: BLE001 - report any failure as degraded
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001 - ignore secondary close failures
                pass
        return ComponentStatus(name="redis", ok=False, detail=f"{type(exc).__name__}")


def check_config() -> ComponentStatus:
    """Validate that required configuration is present and internally consistent."""
    settings = get_settings()
    issues: list[str] = []
    if not settings.database_url:
        issues.append("database_url missing")
    if not settings.redis_url:
        issues.append("redis_url missing")
    if not settings.celery_broker_url:
        issues.append("celery_broker_url missing")
    if not settings.celery_result_backend:
        issues.append("celery_result_backend missing")
    if not settings.cors_origins_list:
        issues.append("cors_allow_origins empty")
    ok = not issues
    return ComponentStatus(name="config", ok=ok, detail="ok" if ok else "; ".join(issues))


def check_worker_config() -> ComponentStatus:
    """Confirm Celery is configured (broker + at least one queue) without contacting it."""
    try:
        from workers.celery_app import celery_app

        settings = get_settings()
        broker = celery_app.conf.broker_url
        result_backend = celery_app.conf.result_backend
        queues = celery_app.conf.task_queues
        ok = (
            bool(settings.celery_broker_url)
            and bool(settings.celery_result_backend)
            and bool(broker)
            and bool(result_backend)
            and bool(queues)
        )
        detail = "configured" if ok else "missing broker, result backend, or queues"
        return ComponentStatus(name="worker", ok=ok, detail=detail)
    except Exception as exc:  # noqa: BLE001
        return ComponentStatus(name="worker", ok=False, detail=f"{type(exc).__name__}")
