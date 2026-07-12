# News Intelligence Platform — Backend

Backend-first AI news intelligence platform. This repository currently contains the
**Stage 1 foundation**: application shell, configuration, logging, metrics, security
baseline, provider contracts, service wiring, and the early database foundation
(see `BUILDING_PLAN.md` for the staged roadmap).

> The `frontend/` directory is managed by a separate plan and is intentionally untouched
> by backend work.

## Architecture (Stage 1)

```text
apps/api/          FastAPI shell: /health, /metrics, CORS, request-id + API-key middleware
packages/config/   settings (env/.env), JSON logging, metrics counters
packages/providers/ typed RSS / embedding / LLM / clock interfaces + deterministic fakes
workers/           Celery app config + no-op task and no-op scheduled beat task
db/                SQLAlchemy base, Alembic migrations (pgvector/PostGIS), seed, schema.sql
infra/docker/      shared Dockerfile for api / worker / beat plus Postgres image
docs/adr/          architecture decision records
tests/             unit (network-free) + integration (needs Postgres) suites
```

## Prerequisites

- Python 3.11+
- Docker + Docker Compose (for the full stack)

## Quick start

```bash
cp .env.example .env            # adjust as needed; never commit real secrets
make install                    # install runtime + dev dependencies (into a venv)
make up                         # start api, worker, beat, postgres (pgvector + PostGIS), redis
make migrate                    # apply migrations (creates pgvector and PostGIS extensions)
curl localhost:8000/health      # API + Postgres + Redis + worker config status
```

## Commands

| Command | Description |
| --- | --- |
| `make install` | Install runtime + dev dependencies |
| `make up` / `make down` | Start / stop the Docker Compose stack |
| `make compose-config` | Validate the Docker Compose configuration |
| `make migrate` | Apply Alembic migrations to head |
| `make seed` | Run the seed script (no Stage 1 tables yet) |
| `make test` | Run the full pytest suite (integration auto-skips unless `REQUIRE_POSTGRES=1`) |
| `make test-unit` | Run unit tests only (no Postgres/Redis required) |
| `make test-integration` | Run integration smoke tests with `REQUIRE_POSTGRES=1` (fails if Postgres is unreachable) |
| `make lint` / `make fmt` | Lint / format with ruff |
| `make audit` | **Dependency vulnerability scan** via `pip-audit -r requirements-dev.txt` (runtime + dev) |
| `make check` | CI gate: compose validation + lint + unit + **mandatory** integration smoke + audit |

## Testing

- **Unit tests** (`tests/unit`) run with no network, database, or broker. They cover
  config, logging, metrics, provider fakes, Celery config, the no-op task, the health
  endpoint, and the API-key middleware. Run them with `make test-unit`.
- **Integration tests** (`tests/integration`, marked `integration`) cover pgvector,
  PostGIS, schema smoke tests, and the Alembic upgrade/downgrade smoke test, and require a reachable
  PostgreSQL. They are gated on the `REQUIRE_POSTGRES` flag:
  - **`REQUIRE_POSTGRES` unset / not `1`** — integration tests are **skipped by default**,
    so a plain `pytest` / `make test-unit` stays network-free.
  - **`REQUIRE_POSTGRES=1` with Postgres reachable** — integration tests run.
  - **`REQUIRE_POSTGRES=1` with Postgres unreachable** — integration tests **fail**
    (never silently skip), so migration/pgvector coverage can't quietly disappear in CI.

```bash
make test-unit                 # unit only (no Postgres/Redis)
make test-integration          # integration smoke with REQUIRE_POSTGRES=1 (needs Postgres)
pytest                         # full suite (integration auto-skips unless REQUIRE_POSTGRES=1)
REQUIRE_POSTGRES=1 pytest -m integration   # integration only, fails if Postgres is down
```

Start Postgres (and the rest of the stack) for integration tests with `make up`, then
apply migrations with `make migrate`. `make check` runs the unit suite
**and** the mandatory integration smoke, so the full gate needs a running PostgreSQL.
If local port `5432` is already in use, start Postgres on another host port and point
the host-side tests at it:

```bash
POSTGRES_HOST_PORT=55432 docker compose up -d postgres
REQUIRE_POSTGRES=1 DATABASE_URL=postgresql+psycopg2://news:news@localhost:55432/news pytest -m integration
```

## Docker Compose

The stack (`api`, `worker`, `beat`, `postgres`, `redis`) is defined in
`docker-compose.yml` and validates in a clean checkout — `make compose-config` (i.e.
`docker compose config`) succeeds **without** a `.env` file. `.env` is loaded when present
but is optional (`required: false`). Compose intentionally forces in-network service
URLs for `DATABASE_URL`, `REDIS_URL`, and the Celery broker/result URLs inside the API,
worker, and beat containers, while `POSTGRES_USER`, `POSTGRES_PASSWORD`, and
`POSTGRES_DB` can be overridden through `.env`. The Postgres service builds from
`infra/docker/postgres.Dockerfile` so local development has both pgvector and PostGIS.
Host-run processes can use the localhost URLs from `.env.example`. Postgres and Redis
publish only on `127.0.0.1`. The Postgres host port defaults to `5432`; set
`POSTGRES_HOST_PORT` when another local service already owns that port.

## Security baseline

- Secrets are read from the environment / `.env` only and are never logged.
- CORS defaults to an explicit localhost allow-list (no wildcard).
- Mutating endpoints are local-only until `API_KEY` is set, after which the API-key
  middleware stub requires a matching `X-API-Key` header.
- `make audit` scans dependencies for known vulnerabilities.
