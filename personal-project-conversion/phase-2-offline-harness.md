# Phase 2 offline workflow harness

This harness runs the personal update workflow against the checked-in synthetic fixture. It starts a disposable PostgreSQL database, Redis, API, and personal Celery worker on loopback ports. It blanks supported provider API keys, loads no `.env` file because `APP_ENV=test`, and does not authorize live spending or contact an external feed.

## Prerequisites

- The repository virtual environment and dependencies are installed.
- Docker is running.
- The local images `news-intelligence-platform-postgres:latest` and `redis:7-alpine` exist. The PostgreSQL image can be built from `infra/docker/postgres.Dockerfile` if needed.
- The frontend port is free if you want to use the browser UI.

From any terminal, set the repository path with quotes because it contains a space:

```bash
NIP_PROJECT_ROOT="/Users/f8fq/coding projects/Unfinished/news-intelligence-platform"
cd "$NIP_PROJECT_ROOT"
```

If the local database image is absent, build it once:

```bash
docker build -f infra/docker/postgres.Dockerfile -t news-intelligence-platform-postgres:latest .
```

## Start

Start the static frontend in one terminal if it is not already running:

```bash
python3 -m http.server 3000 --bind 127.0.0.1 --directory frontend
```

Start a fresh harness in another terminal. Use a new 1-24 character identifier for every run:

```bash
NIP_PROJECT_ROOT="/Users/f8fq/coding projects/Unfinished/news-intelligence-platform"
PERSONAL_HARNESS_ID=manual_phase2_01 \
PERSONAL_HARNESS_FRONTEND_ORIGIN=http://127.0.0.1:3000 \
"$NIP_PROJECT_ROOT/scripts/start-personal-phase2-offline.sh"
```

The command prints:

- its private state directory and ownership manifest;
- the random loopback API port;
- a ready-to-open frontend URL containing a loopback-only `?api=` value;
- a newly generated throwaway API key for protected actions; and
- the exact quoted stop command.

Open the printed frontend URL. Under **Local access**, enter the printed throwaway key. The value remains only in that browser tab. Use **Request today’s update** once, wait for the run to finish, and inspect Today, sources, Saved, the exact brief, citations, and both exports.

The `?api=` page option accepts credential-free `http://localhost`, `http://127.0.0.1`, or `http://[::1]` origins only. It is ignored for remote hosts, HTTPS, or URLs containing credentials.

## Stop

Run the exact stop command printed by the launcher. Its form is:

```bash
NIP_PROJECT_ROOT="/Users/f8fq/coding projects/Unfinished/news-intelligence-platform"
"$NIP_PROJECT_ROOT/scripts/stop-personal-phase2-offline.sh" "/tmp/nip-personal-phase2-offline-manual_phase2_01"
```

Cleanup verifies the recorded process PID, start time, and command marker before sending a signal. It verifies each Docker container ID before removal and verifies the database ownership marker before dropping the database. If an identity or database check fails, cleanup stops and preserves the manifest and remaining resources for inspection.

The state directory remains after successful cleanup so its mode-`0600` manifest and API/worker logs can be reviewed. Choose a new harness identifier for the next run.

## Optional local PostgreSQL server

The default is safest: the harness owns a new PostgreSQL container on a random loopback port. An existing server can be used only through an explicit loopback admin URL on a nonstandard port. The role must be allowed to create and drop its uniquely named disposable database.

```bash
PERSONAL_HARNESS_ID=manual_phase2_02 \
PERSONAL_HARNESS_POSTGRES_ADMIN_URL='postgresql+psycopg2://news:news@127.0.0.1:55433/postgres' \
"$NIP_PROJECT_ROOT/scripts/start-personal-phase2-offline.sh"
```

The launcher rejects non-loopback hosts, the default PostgreSQL port `5432`, missing ports, existing database names, existing state directories, and harness identifiers outside the reserved format.

## Boundaries

This is local synthetic execution evidence. It does not prove live feed access, paid-provider routing, production deployment, or real-world daily reliability. Those gates stay pending until they receive separate authorization and evidence.
