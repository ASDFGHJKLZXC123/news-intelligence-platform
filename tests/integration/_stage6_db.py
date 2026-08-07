"""Shared Stage 6 integration-test database plumbing: disposable, and never `news`.

Every Stage 6 live test owns a throwaway database on the docker Postgres (``localhost:55432`` by
default, or ``POSTGRES_HOST_PORT``) and must never touch the default ``news`` database -- not for
data, not for migrations, not even for an availability probe. The maintenance ``postgres``
database is used *only* to CREATE, DROP and leak-check the disposables.

This module deliberately does not import ``db.base`` (whose engine/``SessionLocal`` are bound to
whatever ``DATABASE_URL`` names -- i.e. ``news``). Callers build their own engines from
:func:`url_for`, so no code path here can target the default database by accident.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text

HOST = "localhost"
PORT = int(os.environ.get("POSTGRES_HOST_PORT", "55432"))
USER = "news"
PASSWORD = "news"
#: The maintenance database. May be connected to only for CREATE/DROP and leak checks.
MAINTENANCE_DB = "postgres"
#: The default operational database. Structurally off-limits to every Stage 6 live test.
FORBIDDEN_DB = "news"


def url_for(database: str) -> str:
    """A SQLAlchemy URL for ``database`` on the disposable host."""
    return f"postgresql+psycopg2://{USER}:{PASSWORD}@{HOST}:{PORT}/{database}"


def admin_engine() -> Engine:
    """AUTOCOMMIT engine on the maintenance ``postgres`` db -- for CREATE/DROP/leak checks only."""
    return create_engine(url_for(MAINTENANCE_DB), isolation_level="AUTOCOMMIT")


def require_disposable_postgres() -> None:
    """Gate the Stage 6 live tests without ever connecting to ``news``.

    ``REQUIRE_POSTGRES`` unset/not ``1`` -> skip. Set but the maintenance database unreachable ->
    fail loudly, so a mandatory-coverage run never silently skips. The probe is against
    ``postgres``, not ``news``.
    """
    if os.environ.get("REQUIRE_POSTGRES") != "1":
        pytest.skip("integration tests skipped; set REQUIRE_POSTGRES=1 to run")
    engine = admin_engine()
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        pytest.fail(f"REQUIRE_POSTGRES=1 but Postgres is not reachable at {HOST}:{PORT}")
    finally:
        engine.dispose()


def unique_database_name(prefix: str) -> str:
    """A unique disposable name that, by construction, can never be the default database."""
    name = f"{prefix}{uuid.uuid4().hex}"
    assert name.startswith(prefix)
    assert name != FORBIDDEN_DB and FORBIDDEN_DB not in name
    return name


@contextlib.contextmanager
def disposable_database(prefix: str) -> Iterator[str]:
    """CREATE a uniquely named disposable database, then DROP it and assert no leak on exit.

    The DROP terminates any lingering backend first, then verifies the row is gone from
    ``pg_database`` -- so a crashed test cannot leave a database behind unnoticed.
    """
    name = unique_database_name(prefix)
    admin = admin_engine()
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    except Exception as exc:  # mandatory coverage: fail loudly, never skip
        admin.dispose()
        pytest.fail(
            f"could not CREATE disposable Stage 6 database on {HOST}:{PORT} "
            f"({type(exc).__name__}); mandatory integration coverage fails rather than skips"
        )

    try:
        yield name
    finally:
        with admin.connect() as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :name AND pid <> pg_backend_pid()"
                ),
                {"name": name},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
            leaked = conn.execute(
                text("SELECT count(*) FROM pg_database WHERE datname = :name"), {"name": name}
            ).scalar_one()
        admin.dispose()
        assert leaked == 0, f"disposable database {name} leaked"
