"""Shared test configuration.

Sets safe defaults in the environment *before* application settings are imported, and
gates the integration suite on the ``REQUIRE_POSTGRES`` flag. The unit suite never
touches a real database, broker, or network.

Integration policy:
- ``REQUIRE_POSTGRES`` unset/not ``1``: integration tests are skipped by default.
- ``REQUIRE_POSTGRES=1`` and Postgres reachable: integration tests run.
- ``REQUIRE_POSTGRES=1`` and Postgres unreachable: integration tests *fail* (never
  silently skip), so migration/pgvector coverage cannot quietly disappear in CI.
"""

from __future__ import annotations

import os

# Ensure a deterministic, local-only configuration for the whole test session.
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("LOG_LEVEL", "INFO")
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://news:news@localhost:5432/news")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("CELERY_BROKER_URL", "redis://localhost:6379/1")
os.environ.setdefault("CELERY_RESULT_BACKEND", "redis://localhost:6379/2")

import pytest  # noqa: E402

# Opt-in flag for the integration suite. When unset, integration tests are skipped.
REQUIRE_POSTGRES = os.environ.get("REQUIRE_POSTGRES") == "1"
_SKIP_REASON = "integration tests skipped; set REQUIRE_POSTGRES=1 to run"


def postgres_available() -> bool:
    """Return True if the configured PostgreSQL is reachable (for integration tests)."""
    try:
        from sqlalchemy import text

        from db.base import engine

        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip integration-marked tests by default unless REQUIRE_POSTGRES=1."""
    if REQUIRE_POSTGRES:
        return
    skip_integration = pytest.mark.skip(reason=_SKIP_REASON)
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_integration)


@pytest.fixture
def require_postgres() -> None:
    if not REQUIRE_POSTGRES:
        pytest.skip(_SKIP_REASON)
    if not postgres_available():
        # REQUIRE_POSTGRES=1 means coverage is mandatory: fail loudly, never skip.
        pytest.fail("REQUIRE_POSTGRES=1 but PostgreSQL is not reachable")
