# Stage 10 Operations Runbook

This runbook is the operator handoff for the locally implemented Stage 10 controls. It covers
the descriptive, manual daily pipeline only. It does **not** approve a production launch, open
Gate G, complete the Stage 9 human reviews, or prove a production SLO, TLS path, monitoring
integration, point-in-time recovery, or vendor data-governance decision.

## Release boundary

Before deploying, retain the reports from these checks with the release evidence:

```bash
make check
make degraded-path-drills
make production-config-check
make db-restore-drill
```

`make check` validates Compose in quiet mode so expanded environment values are never rendered,
then runs lint, the unit suite, a disposable PostgreSQL migrated through Alembic `0019`, the
integration suite, cleanup, and the dependency audit. The stage-kernel
benchmark gates deterministic work amplification, not elapsed time. The configuration preflight
performs no external I/O and reports booleans only. Run the restore drill with the same PostgreSQL
major version intended for the release.

A passing local gate is necessary but not sufficient. Production still needs approved secrets,
network and TLS verification, capacity/SLO tests, centralized monitoring and alerting, a managed
backup/PITR policy, a live recovery procedure, provider review, and the human evaluation evidence
listed under [Escalation and open gates](#escalation-and-open-gates).

The final 2026-08-06 local aggregate run passed quiet Compose validation, Ruff, 4,022 unit tests,
all four structural kernel checks, 141 disposable PostgreSQL integration tests through Alembic
`0019`, verified resource cleanup, and the dependency audit with no known vulnerabilities.

## Readiness and health

After starting the API, Redis, PostgreSQL, and at least one worker that consumes every configured
queue, check:

```bash
curl --fail-with-body http://localhost:8000/health
curl --fail-with-body http://localhost:8000/metrics
```

`/health` returns 200 only when configuration, PostgreSQL, Redis, and a bounded live Celery
worker/queue probe all pass. A failed dependency returns 503 through the standard error envelope;
do not trigger the pipeline while it is degraded. This endpoint includes dependencies and is a
readiness check, not a dependency-free process-liveness check.

The default database ceilings are three seconds for connect and pool checkout and five seconds
for a statement. Redis uses two-second connect and response timeouts with timeout retry disabled.
The worker probe allows one second for broker connect/socket I/O and reply collection, with zero
publication retries, at most 100 worker replies, and at most 64 queues per reply. Treat a timeout
as degraded; the health request is a snapshot and must not inherit production reconnect loops.

`/metrics` exposes process-local request and job counters. They reset when a process restarts and
are not a fleet-wide monitoring or SLO system. Use structured API/worker logs and the request/job
correlation fields during local diagnosis; production log aggregation and alert routing remain a
deployment requirement.

## Trigger and inspect the daily pipeline

Confirm the process date, active sources, Alembic head `0019`, the registered embedding snapshot,
and healthy dependencies before triggering. `process_date` identifies the daily brief; upstream
stages reconcile the backlog visible at run time and do not recreate a historical feed snapshot.
Future dates are rejected.

```bash
curl --request POST http://localhost:8000/api/v1/internal/jobs/process \
  --header 'Content-Type: application/json' \
  --header 'X-API-Key: <configured-key>' \
  --data '{"process_date":"2026-08-06"}'

curl --header 'X-API-Key: <configured-key>' \
  http://localhost:8000/api/v1/internal/jobs/process/2026-08-06
```

Omit the API-key header only for an intentionally local loopback deployment with no configured
`API_KEY`. A fresh request returns HTTP 202, `enqueued: true`, and durable queued state before
broker publication. Repeating an active or succeeded date is idempotent. A failed or partially
failed date remains unchanged until an operator inspects its complete result and explicitly
retries it:

```bash
curl --request POST \
  --header 'X-API-Key: <configured-key>' \
  http://localhost:8000/api/v1/internal/jobs/process/2026-08-06/retry
```

Do not loop on the retry endpoint. It accepts only an inspected failed/partially-failed run or an
expired active lease, and the durable attempt ceiling is three. A broker-publication failure is
recorded as retryable and returned as HTTP 503.

## LLM active-route canary and degradation

Plan the fixed current-route profile first. Planning constructs no clients, performs no network or
database I/O, and writes no files:

```bash
.venv/bin/python scripts/run-llm-quality-canary.py --active-route --pretty
```

Only after the plan resolves the intended Gemini T1 primary and OpenAI `gpt-4.1` T2 primary, the
synthetic fixture is unchanged, the required keys are present, and paid calls are authorized, run:

```bash
.venv/bin/python scripts/run-llm-quality-canary.py --active-route --live --pretty
```

The active-route profile covers entity adjudication, claim grounding, analogy reranking, and
report composition. It disables cache and fallbacks, makes at most six provider calls, reserves at
most $0.25, touches no database or broker, and emits a reduced report without prompts or provider
prose. `decision: pass` validates only this synthetic canary; it is not provider promotion or
approval to send production news.

The normal runtime may use its configured cross-vendor fallback. In the current T2 route, an
OpenAI rate limit or retryable provider failure proceeds to DeepSeek without blindly retrying the
rate-limited primary. The failed primary audit row retains a fixed diagnostic code, and a
successful fallback row records `degraded_provider: deepseek` and the later attempt. Exercise this
behavior offline, not by forcing a live outage:

```bash
.venv/bin/python -m pytest tests/unit/test_llm_orchestrator.py -q \
  -k current_t2_openai_rate_limit_falls_back_to_deepseek_with_safe_audit
```

A fallback success is degraded service, not evidence that the primary is healthy. A canary hold,
provider exhaustion, contract/safety failure, or sustained fallback use blocks a route change and
requires escalation. Do not print or copy prompt/output columns from `llm_runs` into tickets.

The 2026-08-06 local active-route run passed all four workloads on their first attempt with four
calls and an estimated cost of $0.0087115. Preserve each later run's reduced JSON report; do not
assume this dated result proves that a future model alias, key, quota, or route is healthy.

## Leases, recovery, and shutdown

The queue-handoff lease is two minutes. The running lease is 35 minutes, which stays beyond the
pipeline task's 25-minute soft and 30-minute hard limits. A replacement claim rotates ownership,
so a late worker cannot overwrite the replacement attempt. Previously accumulated event IDs are
reloaded on retry.

Run the bounded local failure inventory after lifecycle changes:

```bash
make degraded-path-drills
```

It checks fail-closed PostgreSQL, Redis, and worker/broker readiness against loopback refusal
targets, plus broker-publication failure, expired-lease takeover, stale-owner rejection, and soft
time-limit propagation. It writes no database, Redis, or files and does not serialize subprocess
output.

- If a queued or running job is within its lease, inspect it and wait; do not send a duplicate.
- If an API process dies before broker publication, trigger the same date after the queue lease
  expires, or use the explicit retry endpoint after inspection.
- If a worker is lost, wait until the reported running lease expires, inspect the terminal/active
  state, and use the explicit retry endpoint. Never edit job or lease rows by hand.
- If the attempt budget is exhausted or a failure is non-retryable, stop and escalate.

For planned maintenance, stop accepting manual triggers, inspect active dates, and let work finish.
Give the worker enough time for its 30-minute task ceiling when stopping the local Compose stack:

```bash
docker compose stop --timeout 1860 worker
```

A soft time limit propagates out of per-item and per-stage handlers so the worker can persist a
retryable failure during the five-minute graceful window. A hard kill or host loss may leave only
the lease; recover through the status and retry endpoints after expiry rather than treating the
broker redelivery as a new run.

## Database privilege split

The repository includes `scripts/provision-postgres-roles.sh` for distinct migration-owner and
runtime roles. Inject `DATABASE_ADMIN_URL`, `APP_DATABASE_NAME`, optional
`APP_DATABASE_SCHEMA` (default `public`), and distinct migration/runtime role names and passwords.
Run the provisioner once before Alembic, use the migration-role URL for `alembic upgrade head`,
then run the provisioner again before starting API or worker processes with the runtime-role URL.
The second pass reconciles objects created by the migration; skipping it is a release blocker.

The provisioner rejects weak, reserved, privileged, or membership-linked role setup; makes the
migrator the schema owner; removes stale direct grants, grant options, and delegated privileges;
and limits runtime access to schema usage, application-table CRUD, and sequence use. It excludes
`alembic_version` and extension/admin-owned relations from application grants. Existing
migrator-owned invoker-rights routines receive execution only after a clean provisioning pass.
Function execution is never granted by default: a later migration that creates a routine requires
another provisioning pass, and any migration-owned `SECURITY DEFINER` routine is quarantined and
causes a nonzero review hold. The fresh integration gate proves runtime CRUD succeeds while
`CREATE`, `ALTER`, `DROP`, `TRUNCATE`, temporary-table creation, migration metadata access, and
PostGIS relation writes fail.

The 2026-08-06 disposable PostgreSQL 16 proof passed. It reconciled deliberately excessive
current/default/delegated grants, proved narrow privileges for existing and future tables and
sequences, held future routines until reprovisioning, preserved a committed `SECURITY DEFINER`
quarantine across a refused run, denied protected/extension writes with SQLSTATE `42501`, and
cleaned up the temporary database, roles, containers, volumes, and network.

This does not guarantee compatibility with a managed service. The database administrator must be
allowed to create roles and transfer schema ownership, and pgvector/PostGIS may need privileged
preinstallation. Validate those capabilities and inject/rotate the credentials in the target
platform before calling the role split production-ready.

## Backup and restore

Create a logical backup with the intended database URL:

```bash
DATABASE_URL='<postgresql URL>' make db-backup
```

The command writes an atomic custom-format archive and sibling SHA-256 file, both mode `0600`, and
prints the archive path. Move both artifacts to approved encrypted storage; the repository does
not implement off-host retention.

Exercise recovery before every release that changes schema:

```bash
make db-restore-drill
```

The drill uses separate disposable source and target Compose projects, migrates the source through
`0019`, writes sentinel data, creates and verifies a checksummed archive with a server-matched
`pg_dump`, destroys the source volume, restores into a version-matched target, then verifies the
sentinel checksum, Alembic head, pgvector, PostGIS, and resource cleanup.

The 2026-08-06 local drill passed on PostgreSQL 16 at Alembic `0019`. If Docker storage cannot
build another identical image, reuse a compatible local repository image while retaining the two
isolated projects and cleanup checks:

```bash
RESTORE_DRILL_POSTGRES_IMAGE=<version-matched-local-image> make db-restore-drill
```

Manual restore is intentionally limited to an explicitly named disposable database:

```bash
make db-restore \
  file=backups/postgres/<backup>.dump \
  disposable=1 \
  database=<exact-disposable-database-name>
```

It verifies the checksum, archive readability, and connected database name before a
single-transaction restore, and refuses `postgres`, `template0`, and `template1`. See the
[database operations runbook](database-runbook.md) for retention and database security guidance.
Production restore, PITR/WAL archiving, recovery objectives, and destructive cutover approval are
not implemented by this command. The SHA-256 sidecar detects accidental or malicious content
changes but is not a signature, and `disposable=1` remains an operator assertion; neither is a
substitute for authenticated backup storage or production restore authorization.

## Rollback

Prefer a forward application fix while preserving the current schema. For an application rollback:

1. Stop new manual triggers and allow or recover the active run as described above.
2. Capture a backup and verify both archive and checksum.
3. Deploy the last application image known to be compatible with Alembic `0019`.
4. Recheck `/health`, inspect the affected pipeline date, and run a synthetic active-route canary
   before resuming LLM-backed work.

Do not issue a generic production `alembic downgrade`. Downgrading `0019` removes report content
policy data, downgrading `0018` removes durable pipeline lifecycle details, and `0017` refuses to
downgrade when snapshot/revision rows cannot be represented by the legacy schema. If a schema
rollback is unavoidable, restore a pre-change backup into a separate target, validate it with the
matching application image, and require database-owner approval before traffic cutover.

## Performance benchmarks

Use both local baselines:

```bash
make benchmark-pipeline
make benchmark-stage-kernels
```

The first measures deterministic coordinator control-plane overhead. The second runs fixed
in-process ingestion, embedding, clustering, and production stage-adapter kernels and fails when
their expected batch/call/pair structure changes. Neither includes real database, broker, network,
or provider latency; timing is comparable only on equivalent hosts. Establish production data
volumes, latency/capacity tests, and SLO thresholds in the target environment before launch.

The 2026-08-06 local structural run passed all four fixed workloads. Retain the structural result,
but do not treat its host-specific p50/p95 values as production targets.

The fresh integration gate can likewise reuse an explicitly selected compatible local image when
a rebuild is unavailable:

```bash
EPHEMERAL_POSTGRES_IMAGE=<version-matched-local-image> make test-integration-fresh
```

## Production-configuration preflight

Run the preflight with the intended deployment environment and secrets injected by the deployment
system:

```bash
make production-config-check
```

It reports only named booleans and exits nonzero unless all local policy checks pass: staging/prod
mode, debug off, strong API key, HTTPS non-local CORS, TLS/non-local PostgreSQL and Redis URLs,
enforced LLM budget, complete priced/rate-limited provider routes and keys, an active registered
embedding snapshot, and closed crisis-prediction reads. It performs no network, database, Redis,
or broker calls and therefore does not prove reachability, certificates, credentials, privileges,
provider quota, or data residency. Treat `ready: true` as a configuration gate, not production
approval.

The 2026-08-06 developer configuration correctly returned `ready: false` for the production-only
environment, API-key strength, HTTPS CORS, PostgreSQL TLS/non-local, and Redis TLS/non-local
checks. Supply those values through the target deployment system; do not weaken the preflight to
make a local `.env` pass.

## Escalation and open gates

Stop the release or run and retain the secret-safe report when any of these occurs:

| Condition | Immediate action | Owner/gate |
| --- | --- | --- |
| `/health` returns 503 | Do not trigger; identify the named dependency and inspect correlated logs | Platform operator |
| Active-route canary holds | Keep the prior route; do not promote or send production content | LLM/provider owner |
| Repeated fallback or provider exhaustion | Pause affected LLM work and preserve fixed diagnostic codes/costs only | LLM/provider owner |
| Pipeline lease is active | Wait and inspect; do not duplicate work | Pipeline operator |
| Retry budget exhausted/non-retryable failure | Preserve the terminal result; do not edit lifecycle rows | Application owner |
| Backup/restore drill fails | Block release; preserve logs and both checksum artifacts | Database owner |
| Configuration preflight fails | Fix the named control without copying secrets into the report | Security/platform owner |

The first unavoidable non-local gates are: two independent Stage 9 reviewers plus a separate
adjudicator (including the required clustering labels); Gemini/DeepSeek retention, residency, and
vendor approval; target-environment network/TLS and least-privilege validation; centralized
monitoring/alerting; representative load and SLO acceptance; and a managed backup, PITR, retention,
and production restore/cutover exercise. Gate G remains closed until a separate product and
model-risk decision authorizes predictive reads and writes.
