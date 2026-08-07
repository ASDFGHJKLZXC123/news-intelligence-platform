"""Dependency functions used by the API, primarily for the /health check.

Each check returns a ``ComponentStatus`` rather than raising, so the health endpoint
can report a degraded component without returning a 500. This keeps /health usable as
a liveness/readiness probe even when a dependency is down.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MethodType
from typing import Any

from packages.config.settings import get_settings

# Celery's inspect timeout bounds the reply collection window. The connection created by
# ``_inspect_active_worker_queues`` separately receives the same socket/connect ceiling and a
# zero-retry policy, so a readiness request cannot wait behind the worker broker's production
# reconnect policy. This is intentionally short: health is a snapshot, not a recovery loop.
WORKER_INSPECT_TIMEOUT_SECONDS = 1.0
WORKER_INSPECT_MAX_REPLIES = 100
WORKER_INSPECT_MAX_QUEUES_PER_REPLY = 64

WorkerQueueProbe = Callable[[float], object]


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
        client = redis.Redis.from_url(
            settings.redis_url,
            socket_connect_timeout=2,
            socket_timeout=2,
            retry_on_timeout=False,
        )
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


def _bounded_connection_ensure(connection: Any) -> None:
    """Prevent Celery control publication from inheriting an unbounded retry loop.

    Kombu's pidbox publisher hard-codes ``retry=True`` without a retry policy. Supplying a
    short-lived connection bounds its initial connect, but a connection loss during publication
    would otherwise call ``Connection.ensure(..., max_retries=None)``. Pinning that one
    connection's ``ensure`` method to zero retries keeps the health probe fail-fast without
    changing the application's normal task-publication policy.
    """

    original_ensure = connection.ensure

    def ensure(
        _connection: Any,
        obj: Any,
        fun: Callable[..., Any],
        errback: Callable[..., Any] | None = None,
        max_retries: int | None = None,
        interval_start: float = 1,
        interval_step: float = 1,
        interval_max: float = 1,
        on_revive: Callable[..., Any] | None = None,
        retry_errors: tuple[type[BaseException], ...] | None = None,
    ) -> Callable[..., Any]:
        del max_retries
        return original_ensure(
            obj,
            fun,
            errback=errback,
            max_retries=0,
            interval_start=interval_start,
            interval_step=interval_step,
            interval_max=interval_max,
            on_revive=on_revive,
            retry_errors=retry_errors,
        )

    connection.ensure = MethodType(ensure, connection)


def _inspect_active_worker_queues(timeout_seconds: float) -> object:
    """Return Celery ``active_queues`` replies through a bounded one-shot connection."""

    from workers.celery_app import celery_app

    configured_transport_options = celery_app.conf.broker_transport_options or {}
    transport_options = {
        **configured_transport_options,
        # Bound initial connection attempts and transport I/O. The localized ``ensure`` override
        # below also prevents pidbox publication from retrying after a connection loss.
        "max_retries": 0,
        "connect_retries_timeout": timeout_seconds,
        "interval_start": 0,
        "interval_step": 0,
        "interval_max": 0,
        "socket_connect_timeout": timeout_seconds,
        "socket_timeout": timeout_seconds,
    }
    connection = celery_app.connection_for_write(
        connect_timeout=timeout_seconds,
        transport_options=transport_options,
    )
    _bounded_connection_ensure(connection)
    try:
        inspector = celery_app.control.inspect(
            timeout=timeout_seconds,
            limit=WORKER_INSPECT_MAX_REPLIES,
            connection=connection,
        )
        return inspector.active_queues()
    finally:
        connection.close()


def _configured_queue_names(celery_app: Any) -> frozenset[str]:
    queues = celery_app.conf.task_queues
    return frozenset(
        name
        for queue in queues or ()
        if isinstance((name := getattr(queue, "name", None)), str) and name
    )


def _visible_worker_queues(replies: object) -> tuple[int, frozenset[str]] | None:
    """Normalize inspect replies while rejecting malformed or excessive broker data."""

    if not isinstance(replies, Mapping) or not replies:
        return None
    if len(replies) > WORKER_INSPECT_MAX_REPLIES:
        return None

    visible: set[str] = set()
    for queues in replies.values():
        if not isinstance(queues, Sequence) or isinstance(queues, (str, bytes)):
            return None
        if len(queues) > WORKER_INSPECT_MAX_QUEUES_PER_REPLY:
            return None
        for queue in queues:
            if not isinstance(queue, Mapping):
                return None
            name = queue.get("name")
            if not isinstance(name, str) or not name:
                return None
            visible.add(name)
    return len(replies), frozenset(visible)


def _check_worker_runtime(probe: WorkerQueueProbe) -> ComponentStatus:
    """Check configured Celery queues against a bounded live worker inspection."""

    try:
        from workers.celery_app import celery_app

        settings = get_settings()
        broker = celery_app.conf.broker_url
        result_backend = celery_app.conf.result_backend
        required_queues = _configured_queue_names(celery_app)
        configured = all(
            (
                settings.celery_broker_url,
                settings.celery_result_backend,
                broker,
                result_backend,
                required_queues,
            )
        )
        if not configured:
            return ComponentStatus(
                name="worker",
                ok=False,
                detail="missing broker, result backend, or queues",
            )

        replies = probe(WORKER_INSPECT_TIMEOUT_SECONDS)
        runtime = _visible_worker_queues(replies)
        if runtime is None:
            return ComponentStatus(name="worker", ok=False, detail="no valid live worker replies")

        worker_count, visible_queues = runtime
        missing_queues = required_queues - visible_queues
        if missing_queues:
            missing = ",".join(sorted(missing_queues))
            return ComponentStatus(
                name="worker",
                ok=False,
                detail=f"live workers missing required queues: {missing}",
            )

        queues = ",".join(sorted(required_queues))
        return ComponentStatus(
            name="worker",
            ok=True,
            detail=f"reachable; live_workers={worker_count}; queues={queues}",
        )
    except Exception as exc:  # noqa: BLE001
        return ComponentStatus(name="worker", ok=False, detail=f"{type(exc).__name__}")


def check_worker_config() -> ComponentStatus:
    """Verify Celery configuration plus bounded live worker and queue availability.

    The historic function name is retained because it is a FastAPI dependency override seam used
    by tests and deployments. Its readiness semantics are intentionally stronger now: configured
    broker settings without a replying worker are degraded.
    """

    return _check_worker_runtime(_inspect_active_worker_queues)
