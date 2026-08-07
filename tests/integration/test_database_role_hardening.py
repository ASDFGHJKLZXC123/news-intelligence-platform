"""Disposable proof of the migration-owner/runtime PostgreSQL privilege split.

The fresh integration harness opts into this test and provides its disposable
PostgreSQL container identifier. This test creates a second random database and two
random cluster roles, migrates that database through the migration owner, verifies
runtime CRUD and DDL denial, then drops the database and both roles in ``finally``.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.exc import DBAPIError

from packages.config.settings import get_settings

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
PROVISION_SCRIPT = ROOT / "scripts" / "provision-postgres-roles.sh"
INSUFFICIENT_PRIVILEGE = "42501"


def _quoted(identifier: str) -> str:
    assert identifier.replace("_", "").isalnum()
    return f'"{identifier}"'


def _url_for(base: URL, *, database: str, role: str, password: str) -> str:
    return base.set(username=role, password=password, database=database).render_as_string(
        hide_password=False
    )


def _admin_url_for_provisioner(base: URL, database: str) -> str:
    if os.environ.get("POSTGRES_TOOL_CONTAINER"):
        base = base.set(drivername="postgresql", host="127.0.0.1", port=5432)
    return base.set(database=database).render_as_string(hide_password=False)


def _provision(
    *,
    base_url: URL,
    database: str,
    migration_role: str,
    migration_password: str,
    runtime_role: str,
    runtime_password: str,
) -> None:
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_ADMIN_URL": _admin_url_for_provisioner(base_url, database),
            "APP_DATABASE_NAME": database,
            "APP_DATABASE_SCHEMA": "public",
            "MIGRATION_DATABASE_ROLE": migration_role,
            "MIGRATION_DATABASE_PASSWORD": migration_password,
            "RUNTIME_DATABASE_ROLE": runtime_role,
            "RUNTIME_DATABASE_PASSWORD": runtime_password,
        }
    )
    subprocess.run([str(PROVISION_SCRIPT)], cwd=ROOT, env=env, check=True)


def _migrate(database_url: str) -> None:
    env = os.environ.copy()
    env.update({"APP_ENV": "test", "DATABASE_URL": database_url})
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=env,
        check=True,
    )


def _assert_ddl_denied(runtime_engine: Engine, statement: str) -> None:
    with pytest.raises(DBAPIError) as exc_info, runtime_engine.begin() as connection:
        connection.execute(text(statement))
    assert getattr(exc_info.value.orig, "pgcode", None) == INSUFFICIENT_PRIVILEGE


def test_runtime_role_can_do_application_dml_but_not_ddl(require_postgres: None) -> None:
    if os.environ.get("REQUIRE_DATABASE_ROLE_HARDENING") != "1":
        pytest.skip("run through scripts/test-integration-fresh.sh for isolated role provisioning")
    if not os.environ.get("POSTGRES_TOOL_CONTAINER"):
        pytest.fail("fresh role-hardening test requires POSTGRES_TOOL_CONTAINER")

    base_url = make_url(get_settings().database_url)
    suffix = uuid.uuid4().hex[:12]
    database = f"nip_roles_{suffix}"
    migration_role = f"nip_migrator_{suffix}"
    runtime_role = f"nip_runtime_{suffix}"
    delegate_role = f"nip_delegate_{suffix}"
    migration_password = secrets.token_hex(24)
    runtime_password = secrets.token_hex(24)
    existing_table = f"privilege_existing_{suffix}"
    existing_sequence = f"privilege_existing_seq_{suffix}"
    existing_function = f"privilege_existing_fn_{suffix}"
    future_table = f"privilege_future_{suffix}"
    future_sequence = f"privilege_future_seq_{suffix}"
    future_function = f"privilege_future_fn_{suffix}"
    security_definer_function = f"security_definer_fn_{suffix}"
    admin_function = f"admin_only_fn_{suffix}"
    admin_engine = create_engine(base_url, future=True)
    target_admin_engine: Engine | None = None
    migration_engine: Engine | None = None
    runtime_engine: Engine | None = None

    try:
        with admin_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.exec_driver_sql(f"CREATE DATABASE {_quoted(database)}")
            connection.exec_driver_sql(
                f"CREATE ROLE {_quoted(delegate_role)} "
                "NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION"
            )

        target_admin_engine = create_engine(base_url.set(database=database), future=True)
        with target_admin_engine.begin() as connection:
            # Managed services commonly require these to be preinstalled by their
            # privileged control plane. Migrations keep IF NOT EXISTS for portability.
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))

        _provision(
            base_url=base_url,
            database=database,
            migration_role=migration_role,
            migration_password=migration_password,
            runtime_role=runtime_role,
            runtime_password=runtime_password,
        )

        migration_url = _url_for(
            base_url,
            database=database,
            role=migration_role,
            password=migration_password,
        )
        runtime_url = _url_for(
            base_url,
            database=database,
            role=runtime_role,
            password=runtime_password,
        )
        _migrate(migration_url)

        migration_engine = create_engine(migration_url, future=True)
        with migration_engine.begin() as connection:
            connection.execute(
                text(f"CREATE TABLE public.{_quoted(existing_table)} (id integer PRIMARY KEY)")
            )
            connection.execute(text(f"CREATE SEQUENCE public.{_quoted(existing_sequence)}"))
            connection.execute(
                text(
                    f"CREATE FUNCTION public.{_quoted(existing_function)}() "
                    "RETURNS integer LANGUAGE sql IMMUTABLE AS 'SELECT 11'"
                )
            )

        # Either direction in pg_auth_members is unsafe: a managed role could
        # SET ROLE into another principal, or another principal could SET ROLE
        # into a managed role. The provisioner must fail closed on both.
        with target_admin_engine.begin() as connection:
            connection.exec_driver_sql(f"GRANT {_quoted(runtime_role)} TO {_quoted(delegate_role)}")
        with pytest.raises(subprocess.CalledProcessError):
            _provision(
                base_url=base_url,
                database=database,
                migration_role=migration_role,
                migration_password=migration_password,
                runtime_role=runtime_role,
                runtime_password=runtime_password,
            )
        with target_admin_engine.begin() as connection:
            connection.exec_driver_sql(
                f"REVOKE {_quoted(runtime_role)} FROM {_quoted(delegate_role)}"
            )
            connection.exec_driver_sql(f"GRANT {_quoted(delegate_role)} TO {_quoted(runtime_role)}")
        with pytest.raises(subprocess.CalledProcessError):
            _provision(
                base_url=base_url,
                database=database,
                migration_role=migration_role,
                migration_password=migration_password,
                runtime_role=runtime_role,
                runtime_password=runtime_password,
            )
        with target_admin_engine.begin() as connection:
            connection.exec_driver_sql(
                f"REVOKE {_quoted(delegate_role)} FROM {_quoted(runtime_role)}"
            )

        # Seed a deliberately over-privileged legacy state, including grant
        # options and future-object defaults. The second provisioning pass must
        # erase it before rebuilding the narrow runtime allow-list.
        runtime_engine = create_engine(runtime_url, future=True)
        with target_admin_engine.begin() as connection:
            connection.execute(
                text(
                    f"CREATE FUNCTION public.{_quoted(admin_function)}() "
                    "RETURNS integer LANGUAGE sql IMMUTABLE AS 'SELECT 99'"
                )
            )
            connection.exec_driver_sql(
                f"REVOKE ALL PRIVILEGES ON FUNCTION public.{_quoted(admin_function)}() FROM PUBLIC"
            )
            connection.exec_driver_sql(
                f"GRANT ALL PRIVILEGES ON TABLE public.{_quoted(existing_table)} "
                f"TO {_quoted(runtime_role)} WITH GRANT OPTION"
            )
            connection.exec_driver_sql(
                f"GRANT ALL PRIVILEGES ON SEQUENCE public.{_quoted(existing_sequence)} "
                f"TO {_quoted(runtime_role)} WITH GRANT OPTION"
            )
            connection.exec_driver_sql(
                f"GRANT EXECUTE ON FUNCTION public.{_quoted(existing_function)}() "
                f"TO {_quoted(runtime_role)} WITH GRANT OPTION"
            )
            connection.exec_driver_sql(
                f"GRANT CREATE, TEMPORARY ON DATABASE {_quoted(database)} "
                f"TO {_quoted(runtime_role)} WITH GRANT OPTION"
            )
            connection.exec_driver_sql(
                f"GRANT CREATE ON SCHEMA public TO {_quoted(runtime_role)} WITH GRANT OPTION"
            )
            connection.exec_driver_sql(
                f"GRANT TRUNCATE, REFERENCES, TRIGGER ON TABLE "
                f"public.{_quoted(existing_table)} TO PUBLIC"
            )
            connection.exec_driver_sql(
                f"ALTER DEFAULT PRIVILEGES FOR ROLE {_quoted(migration_role)} "
                f"IN SCHEMA public GRANT ALL PRIVILEGES ON TABLES "
                f"TO {_quoted(runtime_role)} WITH GRANT OPTION"
            )
            connection.exec_driver_sql(
                f"ALTER DEFAULT PRIVILEGES FOR ROLE {_quoted(migration_role)} "
                f"IN SCHEMA public GRANT ALL PRIVILEGES ON SEQUENCES "
                f"TO {_quoted(runtime_role)} WITH GRANT OPTION"
            )
            connection.exec_driver_sql(
                f"ALTER DEFAULT PRIVILEGES FOR ROLE {_quoted(migration_role)} "
                f"IN SCHEMA public GRANT ALL PRIVILEGES ON FUNCTIONS "
                f"TO {_quoted(runtime_role)} WITH GRANT OPTION"
            )
            connection.exec_driver_sql(
                f"ALTER DEFAULT PRIVILEGES FOR ROLE {_quoted(migration_role)} "
                "IN SCHEMA public GRANT ALL PRIVILEGES ON TABLES TO PUBLIC"
            )

        # Exercise CASCADE by delegating each stale grant through runtime. A
        # plain REVOKE ... RESTRICT would fail and leave the database ambiguous.
        with runtime_engine.begin() as connection:
            connection.exec_driver_sql(
                f"GRANT CREATE, TEMPORARY ON DATABASE {_quoted(database)} "
                f"TO {_quoted(delegate_role)}"
            )
            connection.exec_driver_sql(f"GRANT CREATE ON SCHEMA public TO {_quoted(delegate_role)}")
            connection.exec_driver_sql(
                f"GRANT SELECT ON TABLE public.{_quoted(existing_table)} "
                f"TO {_quoted(delegate_role)}"
            )
            connection.exec_driver_sql(
                f"GRANT USAGE ON SEQUENCE public.{_quoted(existing_sequence)} "
                f"TO {_quoted(delegate_role)}"
            )
            connection.exec_driver_sql(
                f"GRANT EXECUTE ON FUNCTION public.{_quoted(existing_function)}() "
                f"TO {_quoted(delegate_role)}"
            )

        _provision(
            base_url=base_url,
            database=database,
            migration_role=migration_role,
            migration_password=migration_password,
            runtime_role=runtime_role,
            runtime_password=runtime_password,
        )

        # These objects are created *after* the final provisioning pass. Their
        # privileges therefore prove the default ACLs without reconciliation.
        with migration_engine.begin() as connection:
            connection.execute(
                text(f"CREATE TABLE public.{_quoted(future_table)} (id integer PRIMARY KEY)")
            )
            connection.execute(text(f"CREATE SEQUENCE public.{_quoted(future_sequence)}"))
            connection.execute(
                text(
                    f"CREATE FUNCTION public.{_quoted(future_function)}() "
                    "RETURNS integer LANGUAGE sql IMMUTABLE AS 'SELECT 22'"
                )
            )
            connection.execute(
                text(
                    f"CREATE FUNCTION public.{_quoted(security_definer_function)}() "
                    "RETURNS integer LANGUAGE sql SECURITY DEFINER "
                    "SET search_path = pg_catalog AS 'SELECT 33'"
                )
            )

        # Functions intentionally have no runtime default grant. A SECURITY
        # DEFINER routine therefore starts quarantined, and its presence makes
        # reprovisioning fail without rolling that quarantine back.
        with target_admin_engine.connect() as connection:
            pre_reprovision_function_privileges = connection.execute(
                text(
                    """
                    SELECT
                        has_function_privilege(:runtime_role, :safe_function, 'EXECUTE'),
                        has_function_privilege(:runtime_role, :security_function, 'EXECUTE')
                    """
                ),
                {
                    "runtime_role": runtime_role,
                    "safe_function": f"public.{future_function}()",
                    "security_function": f"public.{security_definer_function}()",
                },
            ).one()
        assert tuple(pre_reprovision_function_privileges) == (False, False)
        _assert_ddl_denied(runtime_engine, f"SELECT public.{_quoted(future_function)}()")
        _assert_ddl_denied(runtime_engine, f"SELECT public.{_quoted(security_definer_function)}()")

        with pytest.raises(subprocess.CalledProcessError):
            _provision(
                base_url=base_url,
                database=database,
                migration_role=migration_role,
                migration_password=migration_password,
                runtime_role=runtime_role,
                runtime_password=runtime_password,
            )

        with target_admin_engine.connect() as connection:
            post_refusal_function_privileges = connection.execute(
                text(
                    """
                    SELECT
                        has_function_privilege(:runtime_role, :safe_function, 'EXECUTE'),
                        has_function_privilege(:runtime_role, :security_function, 'EXECUTE')
                    """
                ),
                {
                    "runtime_role": runtime_role,
                    "safe_function": f"public.{future_function}()",
                    "security_function": f"public.{security_definer_function}()",
                },
            ).one()
        assert tuple(post_refusal_function_privileges) == (False, False)

        with migration_engine.begin() as connection:
            connection.execute(text(f"DROP FUNCTION public.{_quoted(security_definer_function)}()"))
        _provision(
            base_url=base_url,
            database=database,
            migration_role=migration_role,
            migration_password=migration_password,
            runtime_role=runtime_role,
            runtime_password=runtime_password,
        )

        with target_admin_engine.connect() as connection:
            non_migrator_application_relations = connection.execute(
                text(
                    """
                    SELECT count(*)
                    FROM pg_class objects
                    JOIN pg_namespace namespaces ON namespaces.oid = objects.relnamespace
                    JOIN pg_roles owners ON owners.oid = objects.relowner
                    WHERE namespaces.nspname = 'public'
                      AND objects.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
                      AND owners.rolname <> :migration_role
                      AND NOT EXISTS (
                          SELECT 1
                          FROM pg_depend dependencies
                          WHERE dependencies.classid = 'pg_class'::regclass
                            AND dependencies.objid = objects.oid
                            AND dependencies.deptype = 'e'
                      )
                    """
                ),
                {"migration_role": migration_role},
            ).scalar_one()
        assert non_migrator_application_relations == 0

        with target_admin_engine.connect() as connection:
            existing_privileges = connection.execute(
                text(
                    """
                    SELECT
                        has_table_privilege(:runtime_role, :table_name, 'SELECT'),
                        has_table_privilege(:runtime_role, :table_name, 'INSERT'),
                        has_table_privilege(:runtime_role, :table_name, 'UPDATE'),
                        has_table_privilege(:runtime_role, :table_name, 'DELETE'),
                        has_table_privilege(:runtime_role, :table_name, 'TRUNCATE'),
                        has_table_privilege(:runtime_role, :table_name, 'REFERENCES'),
                        has_table_privilege(:runtime_role, :table_name, 'TRIGGER'),
                        has_table_privilege(
                            :runtime_role, :table_name, 'SELECT WITH GRANT OPTION'
                        ),
                        has_sequence_privilege(:runtime_role, :sequence_name, 'USAGE'),
                        has_sequence_privilege(:runtime_role, :sequence_name, 'SELECT'),
                        has_sequence_privilege(:runtime_role, :sequence_name, 'UPDATE'),
                        has_sequence_privilege(
                            :runtime_role, :sequence_name, 'USAGE WITH GRANT OPTION'
                        ),
                        has_function_privilege(:runtime_role, :function_name, 'EXECUTE'),
                        has_function_privilege(
                            :runtime_role, :function_name, 'EXECUTE WITH GRANT OPTION'
                        ),
                        has_function_privilege(:runtime_role, :admin_function, 'EXECUTE')
                    """
                ),
                {
                    "runtime_role": runtime_role,
                    "table_name": f"public.{existing_table}",
                    "sequence_name": f"public.{existing_sequence}",
                    "function_name": f"public.{existing_function}()",
                    "admin_function": f"public.{admin_function}()",
                },
            ).one()
            future_privileges = connection.execute(
                text(
                    """
                    SELECT
                        has_table_privilege(:runtime_role, :table_name, 'SELECT'),
                        has_table_privilege(:runtime_role, :table_name, 'INSERT'),
                        has_table_privilege(:runtime_role, :table_name, 'UPDATE'),
                        has_table_privilege(:runtime_role, :table_name, 'DELETE'),
                        has_table_privilege(:runtime_role, :table_name, 'TRUNCATE'),
                        has_table_privilege(:runtime_role, :table_name, 'REFERENCES'),
                        has_table_privilege(:runtime_role, :table_name, 'TRIGGER'),
                        has_table_privilege(
                            :runtime_role, :table_name, 'SELECT WITH GRANT OPTION'
                        ),
                        has_sequence_privilege(:runtime_role, :sequence_name, 'USAGE'),
                        has_sequence_privilege(:runtime_role, :sequence_name, 'SELECT'),
                        has_sequence_privilege(:runtime_role, :sequence_name, 'UPDATE'),
                        has_sequence_privilege(
                            :runtime_role, :sequence_name, 'USAGE WITH GRANT OPTION'
                        ),
                        has_function_privilege(:runtime_role, :function_name, 'EXECUTE'),
                        has_function_privilege(
                            :runtime_role, :function_name, 'EXECUTE WITH GRANT OPTION'
                        )
                    """
                ),
                {
                    "runtime_role": runtime_role,
                    "table_name": f"public.{future_table}",
                    "sequence_name": f"public.{future_sequence}",
                    "function_name": f"public.{future_function}()",
                },
            ).one()
            public_future_function_execute = connection.execute(
                text(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM pg_proc routines
                        JOIN LATERAL aclexplode(
                            COALESCE(routines.proacl, acldefault('f', routines.proowner))
                        ) privileges ON true
                        WHERE routines.oid = to_regprocedure(:function_name)
                          AND privileges.grantee = 0
                          AND privileges.privilege_type = 'EXECUTE'
                    )
                    """
                ),
                {"function_name": f"public.{future_function}()"},
            ).scalar_one()
            exposed_security_definer_count = connection.execute(
                text(
                    """
                    SELECT count(*)
                    FROM pg_proc routines
                    JOIN pg_namespace namespaces ON namespaces.oid = routines.pronamespace
                    WHERE namespaces.nspname = 'public'
                      AND routines.prosecdef
                      AND has_function_privilege(:runtime_role, routines.oid, 'EXECUTE')
                    """
                ),
                {"runtime_role": runtime_role},
            ).scalar_one()
            excessive_default_acl_count = connection.execute(
                text(
                    """
                    SELECT count(*)
                    FROM pg_default_acl defaults
                    JOIN pg_roles owners ON owners.oid = defaults.defaclrole
                    JOIN LATERAL aclexplode(defaults.defaclacl) privileges ON true
                    JOIN pg_roles grantees ON grantees.oid = privileges.grantee
                    WHERE owners.rolname = :migration_role
                      AND grantees.rolname = :runtime_role
                      AND (
                          privileges.is_grantable
                          OR NOT (
                              (
                                  defaults.defaclobjtype = 'r'
                                  AND privileges.privilege_type IN (
                                      'SELECT', 'INSERT', 'UPDATE', 'DELETE'
                                  )
                              )
                              OR (
                                  defaults.defaclobjtype = 'S'
                                  AND privileges.privilege_type IN ('USAGE', 'SELECT')
                              )
                              OR (
                                  defaults.defaclobjtype = 'f'
                                  AND privileges.privilege_type = 'EXECUTE'
                              )
                          )
                      )
                    """
                ),
                {"migration_role": migration_role, "runtime_role": runtime_role},
            ).scalar_one()
            delegate_privileges = connection.execute(
                text(
                    """
                    SELECT
                        has_database_privilege(:delegate_role, current_database(), 'CREATE'),
                        has_database_privilege(:delegate_role, current_database(), 'TEMPORARY'),
                        has_schema_privilege(:delegate_role, 'public', 'CREATE'),
                        has_table_privilege(:delegate_role, :table_name, 'SELECT'),
                        has_sequence_privilege(:delegate_role, :sequence_name, 'USAGE'),
                        has_function_privilege(:delegate_role, :function_name, 'EXECUTE')
                    """
                ),
                {
                    "delegate_role": delegate_role,
                    "table_name": f"public.{existing_table}",
                    "sequence_name": f"public.{existing_sequence}",
                    "function_name": f"public.{existing_function}()",
                },
            ).one()
            protected_relation_privileges = connection.execute(
                text(
                    """
                    SELECT
                        has_table_privilege(:runtime_role, 'public.alembic_version', 'SELECT'),
                        has_table_privilege(:runtime_role, 'public.alembic_version', 'INSERT'),
                        has_table_privilege(:runtime_role, 'public.alembic_version', 'UPDATE'),
                        has_table_privilege(:runtime_role, 'public.alembic_version', 'DELETE'),
                        has_table_privilege(:runtime_role, 'public.spatial_ref_sys', 'INSERT'),
                        has_table_privilege(:runtime_role, 'public.spatial_ref_sys', 'UPDATE'),
                        has_table_privilege(:runtime_role, 'public.spatial_ref_sys', 'DELETE')
                    """
                ),
                {"runtime_role": runtime_role},
            ).one()
            external_relation_direct_acl_count = connection.execute(
                text(
                    """
                    SELECT count(*)
                    FROM pg_class objects
                    JOIN pg_namespace namespaces ON namespaces.oid = objects.relnamespace
                    JOIN pg_roles owners ON owners.oid = objects.relowner
                    JOIN LATERAL aclexplode(objects.relacl) privileges ON true
                    JOIN pg_roles grantees ON grantees.oid = privileges.grantee
                    WHERE namespaces.nspname = 'public'
                      AND owners.rolname <> :migration_role
                      AND grantees.rolname = :runtime_role
                    """
                ),
                {"migration_role": migration_role, "runtime_role": runtime_role},
            ).scalar_one()

        expected_narrow_table_sequence_function = (
            True,
            True,
            True,
            True,
            False,
            False,
            False,
            False,
            True,
            True,
            False,
            False,
            True,
            False,
        )
        assert tuple(existing_privileges[:-1]) == expected_narrow_table_sequence_function
        assert existing_privileges[-1] is False
        assert tuple(future_privileges) == expected_narrow_table_sequence_function
        assert public_future_function_execute is False
        assert exposed_security_definer_count == 0
        assert excessive_default_acl_count == 0
        assert tuple(delegate_privileges) == (False, False, False, False, False, False)
        assert tuple(protected_relation_privileges) == (False,) * 7
        assert external_relation_direct_acl_count == 0

        with runtime_engine.begin() as connection:
            connection.execute(text(f"INSERT INTO public.{_quoted(future_table)} (id) VALUES (1)"))
            assert (
                connection.execute(
                    text(f"SELECT id FROM public.{_quoted(future_table)}")
                ).scalar_one()
                == 1
            )
            assert (
                connection.execute(text(f"SELECT nextval('public.{future_sequence}')")).scalar_one()
                == 1
            )
            assert (
                connection.execute(text(f"SELECT public.{_quoted(future_function)}()")).scalar_one()
                == 22
            )
            connection.execute(text(f"DELETE FROM public.{_quoted(future_table)} WHERE id = 1"))

        source_id = uuid.uuid4()
        feed_url = f"https://role-hardening.invalid/{source_id}"
        with runtime_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO sources (id, name, source_type, feed_url)
                    VALUES (:id, 'Runtime role probe', 'rss', :feed_url)
                    """
                ),
                {"id": source_id, "feed_url": feed_url},
            )
            assert (
                connection.execute(
                    text("SELECT name FROM sources WHERE id = :id"), {"id": source_id}
                ).scalar_one()
                == "Runtime role probe"
            )
            connection.execute(
                text("UPDATE sources SET name = 'Runtime role updated' WHERE id = :id"),
                {"id": source_id},
            )
            assert (
                connection.execute(
                    text("DELETE FROM sources WHERE id = :id RETURNING name"), {"id": source_id}
                ).scalar_one()
                == "Runtime role updated"
            )

        _assert_ddl_denied(runtime_engine, "CREATE TABLE public.runtime_forbidden (id integer)")
        _assert_ddl_denied(runtime_engine, "ALTER TABLE public.sources ADD COLUMN forbidden text")
        _assert_ddl_denied(runtime_engine, "DROP TABLE public.sources")
        _assert_ddl_denied(runtime_engine, f"TRUNCATE TABLE public.{_quoted(future_table)}")
        _assert_ddl_denied(runtime_engine, "SELECT version_num FROM public.alembic_version")
        _assert_ddl_denied(
            runtime_engine,
            "UPDATE public.spatial_ref_sys SET auth_name = auth_name WHERE srid = 4326",
        )
        _assert_ddl_denied(
            runtime_engine, "CREATE TEMPORARY TABLE runtime_temp_forbidden (id integer)"
        )

        with runtime_engine.connect() as connection:
            privileges = connection.execute(
                text(
                    """
                    SELECT
                        has_database_privilege(current_user, current_database(), 'CREATE'),
                        has_database_privilege(current_user, current_database(), 'TEMPORARY'),
                        has_schema_privilege(current_user, 'public', 'CREATE')
                    """
                )
            ).one()
        assert tuple(privileges) == (False, False, False)
    finally:
        for engine in (runtime_engine, migration_engine, target_admin_engine):
            if engine is not None:
                engine.dispose()
        with admin_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(
                text(
                    """
                    SELECT pg_terminate_backend(pid)
                    FROM pg_stat_activity
                    WHERE datname = :database AND pid <> pg_backend_pid()
                    """
                ),
                {"database": database},
            )
            connection.exec_driver_sql(f"DROP DATABASE IF EXISTS {_quoted(database)}")
            connection.exec_driver_sql(f"DROP ROLE IF EXISTS {_quoted(delegate_role)}")
            connection.exec_driver_sql(f"DROP ROLE IF EXISTS {_quoted(runtime_role)}")
            connection.exec_driver_sql(f"DROP ROLE IF EXISTS {_quoted(migration_role)}")
        admin_engine.dispose()
