# News Intelligence Platform

Personal conversion status: Phases 1 and 2 are complete, including their authorized bounded live proof. Ordinary paid operation remains inactive. Phases 3, 4A, and **4B are complete for local/synthetic engineering acceptance**. The personal runtime now uses only the application and PostgreSQL as persistent services, with actual browser/CLI, restart, restore, and cleanup proof. **Phase 5 is in progress: raw-reading trial preparation, with model spending off.** See the [Phase 5 trial guide](personal-project-conversion/PHASE5_TRIAL_GUIDE.md) and [evidence record](personal-project-conversion/evidence/phase-5.md). See the [Phase 4B acceptance record](personal-project-conversion/evidence/phase-4b.md) and [personal startup/recovery guide](personal-project-conversion/PHASE4B_SETUP.md). Historical [Phase 4A](personal-project-conversion/evidence/phase-4a.md), [Phase 3](personal-project-conversion/evidence/phase-3.md), and [Phase 2](personal-project-conversion/evidence/phase-2.md) evidence remains retained.

Backend-first financial-crisis early-warning platform. It combines news and event
intelligence, macro/provider data, entity resolution, historical analogies, alerting, and
evidence-grounded reports behind a FastAPI API and a browser frontend adapter.

The delivered application includes a dependency-aware, manual-first daily coordinator for
the non-crisis pipeline. Stage 9 validation is not closed:
the v2 readiness boundary authorizes no new evaluation run, entity linking inherits the v1
policy, analogy remains evaluation-only, and clustering remains not verifiable. The Stage 10
local hardening controls are implemented through Alembic `0019`; human review and
deployment-specific production approval remain open.

Gate G is closed. Crisis-prediction reads are disabled by default, and the daily coordinator
cannot run crisis prediction, predictive rollups, or composite alerts. The platform continues
to serve descriptive observations while predictive fields remain withheld.

## Architecture

```text
apps/api/          FastAPI health, intelligence, entity, analogy, risk, and provider APIs
packages/          configuration, jobs, prompts, provider contracts, and deterministic fakes
services/          ingestion, NLP, entities, LLMs, alerts, analogies, reports, risk, evaluation
workers/           independent Celery tasks and manual non-crisis pipeline stage adapters
db/                SQLAlchemy models, Alembic migrations, curated episode seed
frontend/          no-build browser UI, API adapter, data-quality layer, and sample-data demo
evaluation/        versioned gold datasets, development reports, and frozen Stage 9 artifacts
infra/docker/      shared application image and pgvector + PostGIS PostgreSQL image
tests/             network-free unit suite and disposable-PostgreSQL integration suite
```

## Prerequisites

- Python 3.11+
- Docker + Docker Compose (for the full stack and disposable integration tests)
- Node.js 18+ (only for the zero-dependency frontend tests)
- A modern browser (for the local frontend)

The current no-build browser startup loads pinned React, React DOM, and Babel scripts from
`unpkg.com`, so it requires outbound HTTPS access to that CDN. The page also references Google
Fonts and Cesium CDN assets. Python and frontend unit tests do not load these browser resources.

Live mention extraction additionally requires spaCy's `en_core_web_trf` model. It is not
needed for installation or offline tests, and the application never downloads it implicitly:

```bash
.venv/bin/python -m spacy download en_core_web_trf
```

## Quick start

For the personal runtime, use the [Phase 4B setup guide](personal-project-conversion/PHASE4B_SETUP.md): choose the intended PostgreSQL database, verify its backup/migrations and idle mode, then run `python scripts/personal-local.py app --port 8000`. Browser and API use `http://127.0.0.1:8000/`; paid operation stays disabled.

The following commands describe the larger legacy stack.

```bash
python3 -m venv .venv           # create the preferred local environment
make install                    # install runtime + dev dependencies
cp .env.example .env            # adjust as needed; never commit real secrets
make up                         # start api, worker, beat, postgres (pgvector + PostGIS), redis
make migrate                    # apply migrations (creates pgvector and PostGIS extensions)
curl localhost:8000/health      # API + Postgres + Redis + worker config status
# In a second terminal:
make frontend                   # serve the browser UI on loopback
```

Open
`http://127.0.0.1:3000/SIGNAL%20-%20Intelligence%20Platform.html` in the browser.

## Commands

| Command | Description |
| --- | --- |
| `make install` | Install runtime + dev dependencies |
| `make up` / `make down` | Start / stop the Docker Compose stack |
| `make frontend` | Serve the no-build browser frontend on loopback (default port `3000`) |
| `make compose-config` | Validate the Docker Compose configuration |
| `make migrate` | Apply Alembic migrations to head |
| `make seed` | Seed curated episodes; requires PostgreSQL, an embedding API key, and a registered snapshot |
| `make benchmark-pipeline` | Emit a deterministic, no-network coordinator timing report as JSON |
| `make benchmark-stage-kernels` | Gate fixed ingestion, embedding, clustering, and pipeline-adapter work structure |
| `make degraded-path-drills` | Exercise bounded dependency-loss, lease-recovery, and shutdown paths |
| `make production-config-check` | Run the secret-safe, no-network deployment configuration preflight |
| `make test` | Run pytest (integration auto-skips unless `REQUIRE_POSTGRES=1`) and the frontend tests |
| `make test-unit` | Run Python unit tests only (no Postgres/Redis required) |
| `make test-frontend` | Run the frontend tests with Node's built-in test runner |
| `make test-integration` | Run integration tests against an existing configured PostgreSQL |
| `make test-integration-fresh` | Create a disposable PostgreSQL, migrate it, run integration tests, and remove it |
| `make db-backup` / `make db-restore-drill` | Create a checksummed logical backup / verify disposable recovery through revision `0023_personal_processing_control` |
| `make lint` / `make fmt` | Lint / format with ruff |
| `make audit` | **Dependency vulnerability scan** via `pip-audit -r requirements-dev.txt` (runtime + dev) |
| `make check` | Full gate: Compose + lint + Python/frontend tests + structural benchmark + fresh-database integration + audit |

Validate the curated episode corpus without a database, API key, or network:

```bash
.venv/bin/python -m db.seed.seed --validate-only
```

## Operational safety boundaries

Use the [Stage 10 operations runbook](docs/operations/stage10-runbook.md) for the release
boundary, health checks, manual pipeline recovery, LLM degradation, backup/restore, rollback,
performance checks, production preflight, and escalation conditions.

### LLM provider routing

The live LLM runtime supports four canonical provider names: `anthropic`, `openai`,
`gemini`, and `deepseek`. A fresh checkout still routes T1/T2 reasoning to Anthropic and
T3 verification to OpenAI; adding `GEMINI_API_KEY` or `DEEPSEEK_API_KEY` alone does not
send either provider data.

To opt T1 into Gemini with DeepSeek as its fallback while retaining the default T2/T3
routes, configure all four keys and override the three complete routing maps:

```dotenv
ANTHROPIC_API_KEY=...
OPENAI_API_KEY=...
GEMINI_API_KEY=...
DEEPSEEK_API_KEY=...
LLM_MODELS={"T0":"text-embedding-3-small","T1":"gemini-3.6-flash","T2":"claude-sonnet-5","T3":"gpt-4.1"}
LLM_TIER_PROVIDERS={"T0":"openai","T1":"gemini","T2":"anthropic","T3":"openai"}
LLM_TIER_FALLBACKS={"T1":[{"provider":"deepseek","model":"deepseek-v4-flash"}],"T2":[{"provider":"openai","model":"gpt-4.1"}]}
```

Map-valued environment settings replace their defaults; they do not merge. The built-in
price table covers `gemini-3.6-flash`, `gemini-3.5-flash-lite`,
`deepseek-v4-flash`, and `deepseek-v4-pro`. Any other configured live model must also
receive an explicit `provider:model` entry in
`LLM_PROVIDER_TOKEN_PRICE_USD_PER_1M` or startup fails before a network call.
The configured RPM/TPM values are local safety ceilings: Gemini quotas vary by
project/model/tier, and DeepSeek documents account-level concurrency instead.

Gemini uses the stateless Interactions v1 endpoint with `store=false`. Its current
3.6 Flash and 3.5 Flash-Lite models do not support sampling temperature. A deterministic
`temperature=0` request is translated to Google's recommended explicit-instruction
strategy and the translation is recorded in the LLM audit row; unsupported nonzero
temperatures fail rather than being silently ignored. `GEMINI_MODEL_THINKING_LEVELS`
defaults Flash-Lite to `minimal` and Flash to `low`: hidden thinking and visible JSON share
the output-token ceiling, and the live composition canary exhausted the 4,000-token T2
budget at the provider's medium default. This model-keyed map also replaces its default as
a whole, and every routed Gemini model must have an entry or adapter construction fails
before a call. The selected level is recorded in the LLM audit row and included in the
prompt-cache identity. DeepSeek uses JSON Output with
V4 thinking disabled so a requested temperature remains effective. For report composition only,
its adapter adds a versioned count-before-return instruction that treats the prompt's combined
prose range as a hard condition, rejects the schema example's empty abstention as content, and
preserves required facts and citations during the one corrective retry. Local word counting and
fail-closed omission remain authoritative.

Provider HTTP failures retain a fixed, message-free diagnostic category in the audit row. A
rate-limited primary route is not blindly retried by the orchestrator; it proceeds to the configured
cross-vendor fallback, avoiding a retry storm while preserving degraded-route attribution.

Provider availability is separate from approval to send production news content. Use a
paid, reviewed Gemini project and complete the retention, residency, and vendor review
for DeepSeek before putting either name in a production route.

For the configured mixed primary routes, plan the fixed four-workload active-route canary without
network access or writes, then opt into its bounded paid calls explicitly:

```bash
.venv/bin/python scripts/run-llm-quality-canary.py --active-route --pretty
.venv/bin/python scripts/run-llm-quality-canary.py --active-route --live --pretty
```

This profile requires Gemini as the T1 primary and OpenAI `gpt-4.1` as the T2 primary. It
isolates those primaries with no cache or fallback, permits at most six calls and $0.25, and
emits no prompts or provider prose. A pass is evidence for the active synthetic routes only;
it does not promote a provider or authorize production content. The 2026-08-06 local run passed
all four workloads on their first attempt with four calls and an estimated cost of $0.0087115.

The separate Gemini/DeepSeek comparison matrix remains available when each selected provider is
configured exactly once in both T1 and T2. Plan it first, then opt into the larger live run:

```bash
.venv/bin/python scripts/run-llm-quality-canary.py --pretty
.venv/bin/python scripts/run-llm-quality-canary.py --live --pretty
```

The live matrix isolates each provider (no fallback or cache), permits at most 20 network
calls including schema and word-budget retries, and defaults to a conservative $0.50 launch
reservation. Its JSON report contains only checks, IDs, counts, latency, and estimated cost;
it never emits prompts or provider prose. A pass authorizes shadow evaluation only. Runs filtered
with `--case` are diagnostic and cannot authorize shadow evaluation without all four workloads.

After the canary passes, plan the controlled Gemini/DeepSeek shadow evaluation locally, then
start its paid provider calls only with the explicit live flag:

```bash
.venv/bin/python scripts/run-llm-shadow-evaluation.py --pretty
.venv/bin/python scripts/run-llm-shadow-evaluation.py --live --pretty
```

The shadow matrix contains 125 synthetic input cases and 250 provider-case pairs. It runs
isolated from the database, cache, broker, and publication paths, with no fallbacks, and defaults
to a hard ceiling of 700 provider calls and $5.00. Before every call it makes a rolling cost
reservation and stops before the call if that reservation would exceed the cap.

Promotion gates are enforced separately for each provider: all 125 cases must complete, contract
and safety failures must both remain at zero, at least 123 cases must pass finally, and at least
45 of 50 composition cases must meet their word budget on the first attempt. Passing these gates
only authorizes a limited-rollout review; it never promotes either provider to production
automatically.

When a shadow run holds, use the fixed failure-triage profile before paying for another full
matrix:

```bash
.venv/bin/python scripts/run-llm-shadow-evaluation.py --diagnostic-only --pretty
.venv/bin/python scripts/run-llm-shadow-evaluation.py --diagnostic-only --live --pretty
```

This profile covers nine predetermined Gemini/DeepSeek grounding and composition pairs, permits
at most 26 calls, and has a $1.00 rolling cost ceiling. Its output contains provider/workload
counts, fixed failure-check categories, audit-status counts, and route-level cost counters only.
Provider failures are further split into fixed categories such as rate limit, timeout, server error,
or unusable output. It never prints cases, prompts, generated text, evidence, provider response
bodies, exception messages, or API keys, and its decision is always `hold` with `diagnostic_only`
scope.

### Gate G: descriptive output only

`CRISIS_PREDICTION_READS_ENABLED` defaults to `false`. While it is false, risk-detail reads
do not query persisted crisis predictions, historical prediction results are hidden, and
predictive fields are returned as null or empty values. Descriptive event observations remain
available.

Setting the flag to `true` exposes already persisted prediction rows for an explicit,
controlled diagnostic. It does not authorize new crisis-model writes and does not add the
three excluded predictive stages to the daily coordinator. Opening Gate G requires a separate
reviewed product and model-risk decision.

### Embedding snapshot capture and activation

The configured `text-embedding-3-small` alias is retained, but the legacy model version
`current` is explicitly unverifiable. Existing vectors labeled `current` must never be
relabelled as a new snapshot. New live embedding writes fail closed until a real provider
response has been captured and registered.

With a real `OPENAI_API_KEY` configured:

```bash
# Capture fixed probes as an inactive candidate. The returned vectors derive the snapshot id.
.venv/bin/python scripts/capture-embedding-snapshot.py capture

# Copy snapshot_id from the JSON output, then replay the probes several times to check provider
# drift. Activation performs one final replay and changes the registry only when it succeeds.
SNAPSHOT_ID=nip-es2-YYYYMMDD-REPLACE_WITH_RETURNED_DIGEST
.venv/bin/python scripts/capture-embedding-snapshot.py verify "$SNAPSHOT_ID"
.venv/bin/python scripts/capture-embedding-snapshot.py activate "$SNAPSHOT_ID"
```

Review and commit the new manifest, probe response, and registry change under
`evaluation/embedding-snapshots/`. Only after successful activation, set
`EMBEDDING_MODEL_VERSION` to that exact snapshot id in the deployment configuration and
re-embed into the new sibling vector space. Keep the old `current` rows unchanged so rollback
and historical inspection remain possible. Production backfills replay the probes before and
after writing and abort on drift.

### Manual daily pipeline

The coordinator runs ingestion, article embedding, clustering, entity linking, event
embedding, analogy retrieval, and the descriptive daily brief in dependency order. It is
manual-only: there is no schedule, cron entry, or Celery Beat entry for the coordinator.
Compose still runs Beat for unrelated maintenance tasks.

Before a live run, confirm migrations and service health, leave snapshot enforcement enabled,
verify the configured snapshot id, and confirm the process date and active sources. If no real
snapshot has been captured, stop: do not invent an id, relabel legacy vectors, or disable
snapshot enforcement. Each date uses a deterministic `daily-pipeline:YYYY-MM-DD` identity.

`process_date` is the lifecycle identity and the date of the final daily brief. It is not a
historical-ingestion replay boundary: ingestion, article/event embedding, and clustering reconcile
the outstanding backlog visible when the worker runs. Historical dates are allowed for explicit
brief regeneration, future dates are rejected, and current feed contents are never represented as
a replay of an earlier day.

Trigger one explicit date through the durable operator endpoint:

```bash
curl --request POST http://localhost:8000/api/v1/internal/jobs/process \
  --header 'Content-Type: application/json' \
  --data '{"process_date":"2026-07-29"}'
```

When `API_KEY` is configured, add `--header 'X-API-Key: <configured-key>'`; without it,
mutating routes are restricted to local loopback requests. A fresh request returns HTTP 202
with `state: "queued"` and `enqueued: true`. Repeating a date with an active queue/running lease,
or a succeeded date, does not send a second broker message and returns `idempotent: true`.
Failed and partially failed
dates also remain unchanged on this endpoint so their complete prior result stays inspectable.
Broker delivery uses the `pipeline` queue. If delivery fails, the job is durably marked failed and
the endpoint returns HTTP 503.

Inspect without changing lifecycle state, then retry explicitly:

```bash
curl http://localhost:8000/api/v1/internal/jobs/process/2026-07-29
curl --request POST http://localhost:8000/api/v1/internal/jobs/process/2026-07-29/retry
```

Both internal routes use the same API-key/local-loopback protection as the trigger. The status
response includes the durable event scope, lease expiry, complete terminal stage result, and
item-level failures. A retry is accepted only for an inspected failed/partially-failed date or an
expired active lease, and only while its bounded attempt budget remains.

Queue handoff leases expire after two minutes, allowing a new trigger to repair an API crash before
broker publication. The pipeline task runs every stage in one worker process, including per-event
LLM calls, so it alone raises the fleet-wide Celery limits to a 25-minute soft and 30-minute hard
limit; every other task keeps 120s/180s. Running leases expire after thirty-five minutes, safely
beyond that 30-minute hard time limit. Celery's duplicate-delivery retries may finish before that
lease expires; after a worker loss, use the status route and the explicit retry route once the
reported lease is expired. A replacement claim rotates ownership, so a late old worker cannot
overwrite the newer attempt. Event IDs are accumulated durably and reloaded on retries, so a
previously failed entity-linking or analogy item is attempted again even when clustering creates
no new events.

### Stage 9 v2 human review

The generated packets under `evaluation/stage9-v2/review-packets/` are at
`human_review_status: not_started`. They contain 150 entity-linking tasks, 36 alert tasks,
40 analogy tasks, and a clustering collection template. Clustering still needs at least
100 human-labeled pairs across at least 20 groups, including at least 50 same-event and
50 different-event pairs after adjudication.

Reviewer A and Reviewer B must work independently and without automated labels. Adjudication
begins only after both reviews are final. Existing automated annotations are excluded from the
tasks, and final-holdout labels are not reused. Completing packets creates review evidence; it
does not by itself authorize a final evaluation or open Gate G.

## Testing

- **Unit tests** (`tests/unit`) run without a database or broker. They cover provider and
  LLM contracts, ingestion/NLP, entity linking, alerts, analogies, reports, API behavior,
  evaluation datasets, and Stage 9 governance.
- **Frontend tests** (`frontend/app/*.test.js`) use Node's built-in test runner and no npm
  dependencies. They cover the browser adapter, data loading, data quality, and visible-state
  source contracts.
- **Integration tests** (`tests/integration`, marked `integration`) cover clean Alembic
  bootstrap/rollback, pgvector, PostGIS, persistence and transaction durability, entity
  linking, analogy retrieval, report generation/lifecycle/export, and frontend API contracts.
  They are gated on the `REQUIRE_POSTGRES` flag:
  - **`REQUIRE_POSTGRES` unset / not `1`** — integration tests are **skipped by default**,
    so a plain `make test` stays database-free.
  - **`REQUIRE_POSTGRES=1` with Postgres reachable** — integration tests run.
  - **`REQUIRE_POSTGRES=1` with Postgres unreachable** — integration tests **fail**
    (never silently skip), so migration/pgvector coverage can't quietly disappear in CI.

```bash
make test-unit                 # unit only
make test-frontend             # no database, broker, npm install, or network
make test-integration-fresh    # recommended integration path; owns and cleans up its database
make check                     # complete local/CI-style gate
```

`make install` needs access to the configured Python package index. `make audit` may also
need package-index and vulnerability-service access when their data is not already cached.

For a lightweight post-coordinator performance baseline:

```bash
make benchmark-pipeline
BENCHMARK_ITERATIONS=5000 BENCHMARK_WARMUP=500 make benchmark-pipeline
make benchmark-stage-kernels
```

The coordinator command emits aggregate and per-stage timing for deterministic in-memory runners.
The stage-kernel command also exercises fixed production ingestion, embedding, clustering, and
stage-adapter functions and fails if their expected batch/call/pair structure changes. Neither
makes database, broker, model, or network calls. There is deliberately no hard timing budget:
compare reports from equivalent hosts as a trend signal, and establish real production stage
latency and capacity separately.

`make test-integration-fresh` uses a unique Compose project and a Docker-assigned host
port, migrates a blank database through every revision, runs the suite sequentially, and
verifies that its containers, network, volume, and per-run image were removed. It does not
touch the persistent development database created by `make up`.

When Docker storage prevents a rebuild, an operator may reuse a compatible local PostgreSQL
image without changing the disposable lifecycle:

```bash
EPHEMERAL_POSTGRES_IMAGE=<version-matched-local-image> make test-integration-fresh
```

To exercise an existing development PostgreSQL instead:

```bash
POSTGRES_HOST_PORT=55432 docker compose up -d postgres
DATABASE_URL=postgresql+psycopg2://news:news@localhost:55432/news \
  POSTGRES_HOST_PORT=55432 make test-integration
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
- A fail-closed PostgreSQL provisioner separates the migration owner from the runtime DML role.
  Run it both before and after Alembic; the post-migration pass reconciles object privileges and
  holds any `SECURITY DEFINER` routine for review. The target managed database must still validate
  role and extension capabilities.
- `make audit` scans dependencies for known vulnerabilities.
- `make production-config-check` fails closed on local/weak production settings without exposing
  secret values or contacting external services.
