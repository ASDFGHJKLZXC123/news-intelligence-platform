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
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Load `.env` the way Compose and Settings do, without overriding a real environment
# variable: explicit env > .env > the defaults below.
load_dotenv(_PROJECT_ROOT / ".env", override=False)


def _compose_database_url() -> str:
    """Build the host-side URL of the Compose database.

    docker-compose.yml publishes the container's 5432 on ``POSTGRES_HOST_PORT`` and
    bootstraps the role/db from ``POSTGRES_USER``/``POSTGRES_PASSWORD``/``POSTGRES_DB``.
    Deriving the URL from those same variables keeps host-run processes (pytest,
    alembic) pointed at the container even when it is published off the default port
    -- e.g. because a local PostgreSQL already owns 5432.
    """
    user = os.environ.get("POSTGRES_USER", "news")
    password = os.environ.get("POSTGRES_PASSWORD", "news")
    database = os.environ.get("POSTGRES_DB", "news")
    port = os.environ.get("POSTGRES_HOST_PORT", "5432")
    return f"postgresql+psycopg2://{user}:{password}@localhost:{port}/{database}"


# Ensure a deterministic, local-only configuration for the whole test session.
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("LOG_LEVEL", "INFO")
os.environ.setdefault("DATABASE_URL", _compose_database_url())
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
