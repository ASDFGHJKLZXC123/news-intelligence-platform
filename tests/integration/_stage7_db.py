"""Stage 7 integration-test database plumbing: disposable, migrated, and never ``news``.

The Stage 7 API contracts (events / dashboard / risk-radar / evidence / industry dedup) are proven
DB-free in tests/unit/test_intelligence_api.py, test_pagination_queries.py and test_http_wire.py
against fakes and a recording session. What only a real PostgreSQL can prove -- that the emitted SQL
compiles and executes, that the correlated-subquery / window / DISTINCT-ON / cast-join semantics are
right, and that the serializers agree with seeded facts -- lives in test_stage7_api_contract.py and
runs against a throwaway database this module owns.

Database hygiene (per the folder workflow rules), identical in spirit to ``_stage6_db``:

* Never the default ``news`` database -- not for data, migrations, or an availability probe. The
  maintenance ``postgres`` database is connected to *only* to CREATE / DROP / leak-check the
  disposables. This module deliberately never imports ``db.base`` (whose engine is bound to
  whatever ``DATABASE_URL`` names, i.e. ``news``); callers build engines from :func:`url_for`.
* Every owned database is ``nip_stage7_<hex>`` -- a unique name that, by construction, can never be
  ``news``.
* The disposable is migrated with a real ``alembic upgrade head`` so the SQL under test runs against
  the *accepted* schema, not an ORM ``create_all`` approximation. The migration runs in a
  subprocess whose ``DATABASE_URL`` is the disposable URL, so the parent process's Settings cache /
  ``news`` engine are never touched and cannot be pointed at ``news`` by a bug here.
* The database is dropped -- open backends terminated first -- and its absence asserted, in
  ``finally``. A crashed test cannot leave a database behind unnoticed.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
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
#: The default operational database. Structurally off-limits to every Stage 7 live test.
FORBIDDEN_DB = "news"
#: Every disposable database this item owns starts with exactly this prefix.
STAGE7_DB_PREFIX = "nip_stage7_"


def url_for(database: str) -> str:
    """A SQLAlchemy URL for ``database`` on the disposable host."""
    return f"postgresql+psycopg2://{USER}:{PASSWORD}@{HOST}:{PORT}/{database}"


def admin_engine() -> Engine:
    """AUTOCOMMIT engine on the maintenance ``postgres`` db -- for CREATE/DROP/leak checks only."""
    return create_engine(url_for(MAINTENANCE_DB), isolation_level="AUTOCOMMIT")


def unique_database_name() -> str:
    """A unique ``nip_stage7_<hex>`` name that, by construction, can never be ``news``."""
    name = f"{STAGE7_DB_PREFIX}{uuid.uuid4().hex}"
    assert_owned_stage7_name(name)
    return name


def assert_owned_stage7_name(name: str) -> None:
    """Guard: an owned disposable name must carry the ``nip_stage7_`` prefix and never be ``news``.

    This is the single chokepoint every create/migrate/drop routes through, so no code path in
    this item can operate on a database that is not a freshly minted Stage 7 disposable.
    """
    assert name.startswith(STAGE7_DB_PREFIX), f"{name!r} is not a Stage 7 disposable"
    assert name != FORBIDDEN_DB and FORBIDDEN_DB not in name, f"{name!r} must never be the news db"


def require_disposable_postgres() -> None:
    """Gate the Stage 7 live tests without ever connecting to ``news``.

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


def list_leaked_stage7_databases() -> list[str]:
    """Every ``nip_stage7_*`` database currently on the server -- for before/after leak checks."""
    engine = admin_engine()
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT datname FROM pg_database WHERE datname LIKE :like ORDER BY datname"),
                {"like": f"{STAGE7_DB_PREFIX}%"},
            ).all()
    finally:
        engine.dispose()
    return [row[0] for row in rows]


def _run_alembic_upgrade_head(disposable_url: str) -> None:
    """``alembic upgrade head`` against the disposable database, in an isolated subprocess.

    The subprocess's ``DATABASE_URL`` is the disposable URL, so ``db/migrations/env.py`` (which
    reads it through Settings) migrates the disposable and nothing else. Running out-of-process
    means the parent's Settings ``lru_cache`` and ``db.base`` engine -- both bound to ``news`` --
    are never mutated and can never be the migration target. The URL is asserted to be a
    ``nip_stage7_`` database before the process is spawned.
    """
    assert f"/{STAGE7_DB_PREFIX}" in disposable_url, disposable_url
    assert not disposable_url.endswith(f"/{FORBIDDEN_DB}"), disposable_url
    env = dict(os.environ)
    env["DATABASE_URL"] = disposable_url
    env["POSTGRES_HOST_PORT"] = str(PORT)
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        env=env,
        cwd=project_root,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "alembic upgrade head failed on the disposable database\n"
            f"stdout:\n{proc.stdout[-2000:]}\nstderr:\n{proc.stderr[-2000:]}"
        )


@contextlib.contextmanager
def _disposable_database() -> Iterator[str]:
    """CREATE a uniquely named disposable database, then DROP it and assert no leak on exit.

    Before creating anything, the maintenance server is inspected for pre-existing ``nip_stage7_*``
    databases via the read-only :func:`list_leaked_stage7_databases` query. Any such leftover means
    a prior run failed to clean up, so the suite stops loudly and *neither* creates another
    disposable *nor* deletes the pre-existing one -- an operator must resolve it. A CREATE that then
    fails is a hard :func:`pytest.fail`, never a :func:`pytest.skip`: under ``REQUIRE_POSTGRES=1``
    mandatory coverage must fail loudly rather than silently pass by skipping. The DROP terminates
    any lingering backend first, then verifies the row is gone from ``pg_database`` -- so a crashed
    test cannot leave a database behind unnoticed.
    """
    name = unique_database_name()
    pre_existing = list_leaked_stage7_databases()
    if pre_existing:
        pytest.fail(
            f"pre-existing {STAGE7_DB_PREFIX}* database(s) present; refusing to create another "
            f"or delete them -- resolve manually first: {pre_existing}"
        )
    admin = admin_engine()
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    except Exception as exc:  # mandatory coverage: fail loudly, never skip
        admin.dispose()
        pytest.fail(
            f"could not CREATE disposable database {name} on {HOST}:{PORT} "
            f"({type(exc).__name__}); mandatory Stage 7 coverage fails rather than skips"
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


@contextlib.contextmanager
def migrated_disposable_engine() -> Iterator[Engine]:
    """A throwaway, ``alembic upgrade head``-migrated database, engine disposed then dropped.

    Ordering matters (folder rule "first disposes engines and terminates open sessions"): the
    engine bound to the disposable is disposed *before* the enclosing :func:`_disposable_database`
    terminates any straggler backend and drops the database with a leak check.
    """
    require_disposable_postgres()
    with _disposable_database() as name:
        assert_owned_stage7_name(name)
        _run_alembic_upgrade_head(url_for(name))
        engine = create_engine(url_for(name))
        try:
            yield engine
        finally:
            engine.dispose()
