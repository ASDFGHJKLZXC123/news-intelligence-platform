# Phase 2 bounded live smoke guide

This guide describes the separate Phase 2 live verification gate. The harness software and its
fake-provider tests can be completed without contacting an RSS feed or a model provider. A passing
dry validation is not live evidence. Do not label Phase 2 live verification complete until a user
has authorized a specific config and the resulting ledger records a published report from the
production personal coordinator.

## Boundary

One verification uses a disposable database, no Beat scheduler, no unrelated queue backlog, and
one canonical ledger bound to one `PersonalRun`. Its config lists one to three exact RSS article
URLs and source UUIDs. The RSS wrapper drops every unlisted feed item and fails if a listed item is
missing or duplicated.

Every HTTP client used by an embedding, generation, fallback, retry, or model probe must be wrapped
by `GuardedHTTPClient`. It writes an immutable reservation before each physical `POST`. The entire
verification is limited to:

- 20 paid dispatches, or a lower configured cap.
- $0.25 in conservative reservations, or a lower explicitly authorized allowance.
- 8,192 conservatively bounded input tokens and 4,096 requested output tokens per reasoning or
  probe request.
- 8,192 conservatively bounded input tokens and at most three captured articles per embedding
  request.

The full serialized provider request body supplies the input bound. The harness does not trust a
caller's token estimate. A transport failure, missing usage, or interrupted process retains the
full reservation. A retry makes and retains a new reservation. The OpenAI embedding builder forces
the production adapter to one internal attempt; a retry must pass through the ledger as another
physical dispatch.

Routes are finite. The config names the ordered generation route list, every probe route, the one
embedding route, and a dispatch allocation for each. An unknown route, model mismatch, missing
output cap, multiple generated candidates, unknown or non-finite price, oversized request, spent
allowance, or terminal run blocks before the delegate can send the request.

## Prepare the input

The user's [selected preferences](SELECTED_PREFERENCES.md) record the accepted four feeds, empty
interest filters, Gemini generation and OpenAI embeddings. The linked JSON is a non-executable
preference record with no runtime loader, not a config for this runner. The user approved a $0.25
cap for one bounded verification attempt on September 19; no recurring budget is approved. The user subsequently authorized one concrete attempt, which failed before paid work; [retained evidence](evidence/phase-2-live-20260919/README.md) records $0 spent and the verified TLS setup correction. The September 20 [authorized retry](evidence/phase-2-live-retry-20260920/README.md) used that correction, reached both providers and failed composition acceptance at $0.00362026. The user subsequently authorized “Run the corrected test”; its [fresh proof](evidence/phase-2-live-corrected-20260920/README.md) passed at $0.00224186 with no uncertainty, closing Phase 2. No further attempt is authorized, and ordinary activation remains off. Preparing any future executable config still requires exact capture identities,
current route/pricing evidence and authorization for that concrete run. Do not treat the example
config below as authorized. The four-feed preference does not enlarge the three-article proof limit.

Complete offline Phase 2 checks first. Use a new private state directory and a new disposable
database. The current runner accepts only a fresh, unbound ledger and a fresh database; it does not
resume an interrupted live run. Preserve the old ledger, database evidence, `verification_id`,
`config_hash`, and bound run UUID after a failure. Do not create a new state directory or allowance
for the same attempt. Investigate first, then use a new config and verification ID only after a new
explicit authorization.

For each chosen article, store:

- A short sanitized `capture_id`, such as `rss-1`.
- The exact selected Source UUID.
- The normalized canonical article URL.
- `url_hash(canonical_url)` from `services.ingestion.normalize`.

An empty `include_phrases` array is valid and means all selected-feed items remain eligible subject
to exclusions. Never infer an interest phrase. The config must still contain both interest arrays.

Use the official, current pricing page for each exact provider/model immediately before a live
attempt. Record that HTTPS page, a clear source version or retrieval label, and its timestamp. Old
repository examples and this guide are not price authority. Prices and allowance are decimal
strings; JSON numbers, NaN, Infinity, exponents, and excessive precision are rejected. Embedding
input price must be positive and embedding output price must be `"0"`.

The config is deliberately secret-free. Provider credentials stay in process environment variables
and must never appear in the config, ledger, console output, profile, snapshot, report, or exports.

This valid validation-only example uses non-live placeholder identities. Replace every capture and
route identity, URL hash, model, version, and price source before requesting live authorization:

```json
{
  "schema": "personal-live-smoke-config.v1",
  "verification_id": "11111111-1111-4111-8111-111111111111",
  "authorization": {
    "live_execution_authorized": false,
    "authorized_allowance_usd": "0.05",
    "authorized_at": null
  },
  "capture": {
    "articles": [
      {
        "capture_id": "rss-1",
        "source_id": "33333333-3333-4333-8333-333333333333",
        "canonical_url": "https://news.example.test/a",
        "url_hash": "7222a77722996dd632a98cd7ab48bc428727bbdc28f81114f672818a27906bcc"
      }
    ]
  },
  "interest": {
    "include_phrases": [],
    "exclude_phrases": []
  },
  "limits": {
    "max_dispatches": 10,
    "reasoning_input_tokens": 8192,
    "reasoning_output_tokens": 4096,
    "embedding_input_tokens": 8192,
    "embedding_batch_articles": 1
  },
  "plan": {
    "generation_route_ids": ["generation-primary"],
    "probe_route_ids": [],
    "embedding_route_id": "embedding-primary"
  },
  "routes": [
    {
      "route_id": "generation-primary",
      "role": "reasoning",
      "provider": "openai",
      "model": "replace-with-exact-generation-model",
      "model_version": "replace-with-exact-version",
      "max_dispatches": 8,
      "input_usd_per_million_tokens": "1.00",
      "output_usd_per_million_tokens": "1.00",
      "price_source": {
        "url": "https://provider.example.test/current-pricing",
        "version": "replace-with-retrieval-version",
        "retrieved_at": "2026-09-09T12:00:00-07:00"
      }
    },
    {
      "route_id": "embedding-primary",
      "role": "embedding",
      "provider": "openai",
      "model": "replace-with-exact-embedding-model",
      "model_version": "replace-with-exact-version",
      "max_dispatches": 2,
      "input_usd_per_million_tokens": "0.01",
      "output_usd_per_million_tokens": "0",
      "price_source": {
        "url": "https://provider.example.test/current-pricing",
        "version": "replace-with-retrieval-version",
        "retrieved_at": "2026-09-09T12:00:00-07:00"
      }
    }
  ]
}
```

Add every possible generation fallback to `routes` and the ordered generation plan. Keep a fallback
disabled when it is not listed. `probe_route_ids` must remain empty for this runner because the
Phase 2 coordinator has no separate paid probe stage. The sum of per-route dispatch allocations
cannot exceed the configured whole-run cap.

## Dry validation

From the repository root, validation performs no RSS or model calls and creates no ledger:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python \
  scripts/run-personal-phase2-live-smoke.py \
  --config "/absolute/private/path/live-smoke-config.json"
```

The output is allowlisted. It includes capture IDs, source UUIDs, URL hashes, limits, route identity,
price-source metadata, allowance, verification ID, and config hash. It omits article URLs, interest
phrases, and credentials.

## Disposable runtime setup

The execution command does not provision or remove PostgreSQL. Before authorizing live work, create
one dedicated container on a random loopback port. Do not reuse the offline harness, the development
database, port 5432, or another task's container. The database name must begin with
`nip_phase2_live_`.

Use a short new lowercase identifier and a private state directory. The `mkdir` command must fail if
that directory already exists. Run the setup, credential-entry, execution, and cleanup blocks in an
explicit Bash session (`bash`); the user's default zsh has different `read` options.

```bash
NIP_PROJECT_ROOT="/Users/f8fq/coding projects/Unfinished/news-intelligence-platform"
cd "$NIP_PROJECT_ROOT"
LIVE_SMOKE_ID="replace_with_new_id"
LIVE_SMOKE_DB="nip_phase2_live_${LIVE_SMOKE_ID}"
LIVE_SMOKE_PG="nip-phase2-live-${LIVE_SMOKE_ID}"
LIVE_SMOKE_STATE="/tmp/nip-phase2-live-${LIVE_SMOKE_ID}"
mkdir -m 700 "$LIVE_SMOKE_STATE"
printf '%s\n' "$LIVE_SMOKE_PG" >"$LIVE_SMOKE_STATE/postgres.container-name"
chmod 600 "$LIVE_SMOKE_STATE/postgres.container-name"
docker run --rm -d \
  --name "$LIVE_SMOKE_PG" \
  -e POSTGRES_USER=news \
  -e POSTGRES_PASSWORD=news \
  -e POSTGRES_DB="$LIVE_SMOKE_DB" \
  -p 127.0.0.1::5432 \
  news-intelligence-platform-postgres:latest \
  >"$LIVE_SMOKE_STATE/postgres.container-id"
chmod 600 "$LIVE_SMOKE_STATE/postgres.container-id"
LIVE_SMOKE_PORT="$(docker port "$LIVE_SMOKE_PG" 5432/tcp)"
LIVE_SMOKE_PORT="${LIVE_SMOKE_PORT##*:}"
printf '%s\n' "$LIVE_SMOKE_PORT" >"$LIVE_SMOKE_STATE/postgres.port"
chmod 600 "$LIVE_SMOKE_STATE/postgres.port"
```

Wait for `docker exec "$LIVE_SMOKE_PG" pg_isready -U news -d "$LIVE_SMOKE_DB"` to succeed. Then
set the isolated process environment and migrate that database. `APP_ENV=test` prevents `.env`
credentials and routes from being loaded:

```bash
export APP_ENV=test
# Use the existing trusted CA bundle for this process; keep TLS verification enabled.
export SSL_CERT_FILE="$(.venv/bin/python -c 'import certifi; print(certifi.where())')"
test -r "$SSL_CERT_FILE"
export DATABASE_URL="postgresql+psycopg2://news:news@127.0.0.1:${LIVE_SMOKE_PORT}/${LIVE_SMOKE_DB}"
export ANTHROPIC_API_KEY=
export OPENAI_API_KEY=
export GEMINI_API_KEY=
export DEEPSEEK_API_KEY=
export ANTHROPIC_BASE_URL="https://api.anthropic.com"
export OPENAI_BASE_URL="https://api.openai.com"
export GEMINI_BASE_URL="https://generativelanguage.googleapis.com"
export DEEPSEEK_BASE_URL="https://api.deepseek.com"
.venv/bin/alembic upgrade head
```

Insert exactly one active `sources` row for each distinct `source_id` in the config and no other
rows. Repeat this command only for additional distinct sources. The feed must be the real RSS feed
that currently contains the listed canonical article URL. The source UUID must exactly match the
config:

```bash
LIVE_SOURCE_ID="replace-with-config-source-uuid"
read -r -p "Exact RSS feed URL: " LIVE_FEED_URL
docker exec -i "$LIVE_SMOKE_PG" psql -v ON_ERROR_STOP=1 \
  -U news -d "$LIVE_SMOKE_DB" \
  -v source_id="$LIVE_SOURCE_ID" -v feed_url="$LIVE_FEED_URL" <<'SQL'
INSERT INTO sources (id, name, source_type, feed_url, active)
VALUES (:'source_id'::uuid, 'Bounded live smoke RSS', 'rss', :'feed_url', true);
SQL
unset LIVE_FEED_URL
```

Before enabling execution, perform a public RSS-only check through the production HttpRSSProvider and the config's exact FixedCaptureRSSProvider filter using this same process trust setting. Confirm each selected article appears once. This diagnostic makes no model calls or application run and is not live acceptance. It catches transport/TLS and missing-item failures before consuming a verification attempt. See the [September 19 certificate diagnosis](evidence/phase-2-live-20260919/rss-tls-diagnosis.json).

The worker preflight verifies all of the following before RSS or paid-provider construction: the
process is local/test; PostgreSQL is loopback on an explicit non-5432 port; the database name has
the reserved prefix; the schema is migrated; exactly the configured active sources exist; all
user, workspace, profile, run, capture, article, event, claim, evidence, report, LLM-run, and job
tables are empty; and writer mode remains `legacy`. Redis, Celery, Beat, and the API are not used.
The command calls the production personal coordinator directly.

## Authorized execution

The following command is intentionally not part of deterministic acceptance. It makes real RSS and
paid provider calls. Run it only after reviewing the fully populated config and receiving explicit
authorization for its exact allowance and routes. Set `live_execution_authorized` to `true`, set
`authorized_at` to that authorization timestamp, and repeat the config's UUID in the execution
command below.

Enter the OpenAI key for embeddings without placing it in shell history. Do the same for the exact
listed generation provider; leave unused provider keys blank:

```bash
read -r -s -p "OpenAI API key: " OPENAI_API_KEY
export OPENAI_API_KEY
printf '\n'
# If a different listed generation provider is used, read and export only its key here.
```

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python \
  scripts/run-personal-phase2-live-smoke.py \
  --config "/absolute/private/path/live-smoke-config.json" \
  --state-dir "$LIVE_SMOKE_STATE" \
  --execute \
  --confirm-verification-id "the-config-verification-uuid"
```

The worker must create or identify the disposable `PersonalRun`, call `ledger.bind_run(run_id)`
before the first paid dispatch, and run the production capture, grouping, claim, shared
composition, grounding, copyright, and publication path. It must not insert or return a ready-made
brief. The current worker preflights all listed credentials and official provider base URLs, rejects
probe routes because Phase 2 has no separate paid probe stage, freezes the verification/config/
ledger route into the profile and snapshot, and rejects ordinary API or Celery entry into this seam.

On success, the command returns status, verification ID, config hash, run, snapshot, and report
UUIDs, exact capture IDs, and ledger totals. A report can be published while uncertain provider
usage blocks live verification. Inspect the ledger outcome and the run's live_smoke stage; the
existence of a published report alone is not a live pass. The blocked record retains any existing
snapshot/report identities without deleting valid content.

If the process is interrupted or any bounded check fails, retain that ledger and database for
investigation. Do not rerun the command against it. Ordinary API/Celery retries are unavailable for
this dedicated live profile; a new attempt requires separately reviewed inputs and authorization.

## Cleanup

After retaining the required evidence, remove only the container whose exact ID was recorded before
startup. The identity comparison prevents the command from removing a different container that
later reused the name:

```bash
LIVE_SMOKE_STATE="/tmp/nip-phase2-live-replace_with_new_id"
LIVE_SMOKE_PG="$(cat "$LIVE_SMOKE_STATE/postgres.container-name")"
EXPECTED_CONTAINER_ID="$(cat "$LIVE_SMOKE_STATE/postgres.container-id")"
ACTUAL_CONTAINER_ID="$(docker inspect --format '{{.Id}}' "$LIVE_SMOKE_PG")"
if [[ -n "$EXPECTED_CONTAINER_ID" && "$ACTUAL_CONTAINER_ID" = "$EXPECTED_CONTAINER_ID" ]]; then
  docker rm -f "$LIVE_SMOKE_PG"
else
  printf 'Refusing cleanup: container identity does not match the recorded ID.\n' >&2
fi
```

Keep the private state directory, config, ledger, container identity, command output, and selected
source hashes as evidence. Removing this uniquely owned container removes its disposable database
and releases its random port.

## Evidence to retain

Keep the config, ledger, disposable database evidence, and source hashes. The ledger records exact
provider/model/version, price-source version, hard caps, sanitized input IDs, each physical
reservation, known usage and cost when supplied, uncertain held reservations, the fixed capture
IDs, run/snapshot/report UUIDs, and terminal outcome. A successful outcome requires every configured
capture plus a snapshot and published report UUID. A bounded failure remains a failure; do not
increase a cap or start a new ledger to turn it into a pass.

Phase 3 owns an application-wide daily or monthly spending ledger. This Phase 2 file applies only
to one explicitly identified verification run.
