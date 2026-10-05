# Phase 4B personal local runtime

The persistent runtime is one native FastAPI application and PostgreSQL. Each requested update has a temporary supervised Python child. The application serves the existing browser interface and API at `http://127.0.0.1:8000/`; Today, Saved, Briefs, Settings, updates, and exports use that same address. Redis, Celery workers, and Beat are not required in this mode and are not started by these commands.

This guide describes the Phase 4B configuration. Acceptance and its limitations belong in the phase evidence; setup instructions do not establish live news usefulness or Phase 5 acceptance.

## Choose the database before starting

Keep the repository's existing Python environment and dependencies. Do not overwrite `.env`. `scripts/personal-local.py` preserves an explicitly exported `DATABASE_URL`. Without one, it chooses the separate `news_personal` database on loopback port **55443**, matching `infra/docker-compose.personal.yml`; it never inherits the database URL from `.env`. To retain an existing personal workspace and its history, export the intended existing database URL before using the wrapper. A new empty database does not copy prior articles, Saved, reports, run identities, or spending.

For a dedicated new database, first inspect the chosen Compose project and port for existing resources (no listener output means no visible TCP listener at that instant):

```sh
docker compose -f infra/docker-compose.personal.yml --project-name nip-personal-local ps --all
docker volume ls --filter label=com.docker.compose.project=nip-personal-local
lsof -nP -iTCP:55443 -sTCP:LISTEN
```

If these resources belong to an unrelated runtime, choose a different project name consistently for every command. Do not stop or delete shared services to free the port. Change `PERSONAL_POSTGRES_PORT` in the current shell if needed. The defaults are `PERSONAL_POSTGRES_USER=news`, `PERSONAL_POSTGRES_PASSWORD=personal_local_only`, `PERSONAL_POSTGRES_DB=news_personal`, and `PERSONAL_POSTGRES_PORT=55443`. The default password is a local development value, not a secret. Set the four variables consistently before creating the volume if different values are needed; changing initialization variables later does not change users/passwords in an existing volume.

```sh
docker compose -f infra/docker-compose.personal.yml --project-name nip-personal-local up --detach --build postgres
```

This configuration contains only PostgreSQL. The app runs on the host so its own listener binds to loopback, without a public container listener. An existing PostgreSQL instance may be used instead; then no new database container is required.

## Upgrade a backup copy and select personal mode

Before applying additive migrations to a database with existing content, create a checksummed backup and restore it to a disposable copy as described below. Confirm the copy retains articles, events, Saved, report versions, logical runs, frozen inputs, and known/uncertain spending. Set `DATABASE_URL` explicitly for migration and direct module commands; the wrapper's defaults do not alter the environment of other shell commands.

For the dedicated default database only:

```sh
export DATABASE_URL='postgresql+psycopg2://news:personal_local_only@127.0.0.1:55443/news_personal'
python -m alembic upgrade head
python scripts/personal-local.py mode personal
```

For an existing workspace, replace that URL with its intended URL and verify the backup copy before migrating the original. Startup never runs a migration, selects a durable mode, creates a run, recovers an owner, or grants a new allowance implicitly. The mode command locks the global control record and requires no active owner or unconfirmed live child. Retained history and spending remain intact.

## Start and stop the application

From the repository with its environment active:

```sh
python scripts/personal-local.py app --port 8000
```

Open `http://127.0.0.1:8000/`. The wrapper enforces subprocess transport, configured personal mode, loopback binding, one app worker, and **paid runtime disabled**, even if `.env` or the inherited shell says otherwise. It preserves database and authentication settings. A synthetic fixture is used only when `PERSONAL_OFFLINE_FIXTURE_PATH` is explicitly exported in this shell. The wrapper replaces itself with the existing app module, leaving one app supervisor.

Health reports PostgreSQL/configuration/launcher readiness and explicit `not_required_in_personal_mode` statuses for Redis and Celery. Missing live credentials make the selected capability unavailable while stored reading remains available. Personal prompt caching and throughput limiting are process local; durable PostgreSQL processing ownership and spending reservations remain authoritative.

**Shutdown command:** press **Ctrl-C in the foreground app terminal**. The app refuses new launches, requests child cancellation, waits at most 30 seconds, then terminates its owned child process group. It retains interrupted leases, inputs, and reservations if a durable terminal write cannot be completed. The manual update command follows the same Ctrl-C cleanup. No detached background app or PID-only kill command is used.

Once the app has stopped, stop only the dedicated PostgreSQL project if desired:

```sh
docker compose -f infra/docker-compose.personal.yml --project-name nip-personal-local stop postgres
```

The named `personal_pgdata` volume remains. Start that same project again to retain its data. Do not use `down --volumes` on the working personal project as an ordinary shutdown command.

## Explicit updates and recovery

These commands use the existing CLI and shared supervisor. `update` means one requested current-date update; it can contact configured feeds. The wrapper keeps paid dispatch disabled and does not silently change saved profile preferences.

```sh
python scripts/personal-local.py status
python scripts/personal-local.py update
python scripts/personal-local.py retry RUN_UUID
```

A queued identity is not completion. Status never starts a replacement. A successful date remains once-only; failed runs retain the total three-attempt limit. The original 2/25/30/35-minute handoff/graceful/hard/ownership limits and independent child watchdog still apply, with no lease renewal.

After a hard crash, inspect retained state and use explicit recovery when eligible:

```sh
python scripts/personal-local.py reconcile-children
python scripts/personal-local.py recover RUN_UUID
python scripts/personal-local.py retry RUN_UUID
```

Reconciliation checks process birth/command identity; a PID alone is insufficient. An unconfirmed child blocks an idle mode switch. Recovery requires the original ownership lease to expire and does not launch a replacement or restore paid capacity.

## Backup and disposable restore

The existing scripts keep the full current schema and checksum the archive. For native PostgreSQL tools matching the source server's major version, export the intended source `DATABASE_URL` and choose a private backup directory:

```sh
BACKUP_DIR=backups/personal bash scripts/backup-postgres.sh
```

Retain both the returned `.dump` file and its `.sha256` sidecar. Backups can contain personal data. To use tools inside an inspected PostgreSQL container, set `POSTGRES_TOOL_CONTAINER` to its exact container ID and use that container's internal database URL (port 5432) for the script, rather than the host's port 55443.

Create a **separate disposable PostgreSQL instance/database**, with a distinct Compose project, distinct volume, database name, and loopback port. For example, in a separate terminal:

```sh
export PERSONAL_POSTGRES_DB=news_personal_restore_check
export PERSONAL_POSTGRES_PORT=55433
docker compose -f infra/docker-compose.personal.yml --project-name nip-personal-restore-check ps --all
docker volume ls --filter label=com.docker.compose.project=nip-personal-restore-check
docker compose -f infra/docker-compose.personal.yml --project-name nip-personal-restore-check up --detach --build postgres
export DATABASE_URL='postgresql+psycopg2://news:personal_local_only@127.0.0.1:55433/news_personal_restore_check'
RESTORE_TARGET_DISPOSABLE=1 RESTORE_EXPECTED_DATABASE=news_personal_restore_check \
  bash scripts/restore-postgres.sh /absolute/path/to/backup.dump
```

Proceed only after the inspection establishes that this project/port belongs to the disposable target; choose another project/port if already occupied. Use consistent custom username/password values when applicable. Restore verifies the checksum, validates a readable archive and the actual connected database name, rejects protected system names, and requires both disposable-target variables before issuing cleaning SQL. **Never point this restore command at the current working personal database.**

Inspect the restored current Alembic revision and compare populated article/event, Saved, report-version, run/snapshot, and spending records with the source. Status/read APIs should work without launching processing. Keep the validated backup. Once this distinct restore target is verified, stop its project while preserving the volume, or remove its disposable volume only after proving resource ownership. The general `scripts/test-postgres-restore.sh` additionally performs an isolated schema/extensions/checksum drill; its sentinel-only check does not replace populated personal-record comparison.

## Idle rollback to the supported legacy runtime

Stop the personal application, reconcile any retained child, and inspect status. While genuinely idle, select the durable previous supported mode:

```sh
python scripts/personal-local.py reconcile-children
python scripts/personal-local.py status
python scripts/personal-local.py mode legacy
```

An active owner, unrecovered lease, unconfirmed child, or processing transaction blocks this switch. Do not force it by deleting control records, reservations, runs, or new tables. Once the command succeeds, select `PERSONAL_PROCESSING_MODE=legacy` and `PERSONAL_PROCESSING_TRANSPORT=celery` for the existing legacy runtime and follow its documented startup. The personal wrapper intentionally selects personal execution and is not the legacy app launcher.

The schema stays upgraded. Only supported application binaries that honor the global mode/fencing guard may write to that database. Switching mode preserves Saved, frozen inputs, reports/versions, run identities, and spending; it neither grants another successful date nor clears uncertain reservations. An older unguarded binary requires a separately restored validated older-version database, not a destructive down-migration of current work.
