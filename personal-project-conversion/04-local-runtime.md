# Phase 4 — Simplify the local runtime

**Status: Phase 4A and Phase 4B complete for local/synthetic engineering acceptance; all eleven 4A cases and all five 4B cases passed. Ordinary paid activation remains off.** See current [Phase 4B evidence](evidence/phase-4b.md) and [two-service setup/recovery](PHASE4B_SETUP.md), plus historical [Phase 4A evidence](evidence/phase-4a.md) and [setup/recovery](PHASE4A_SETUP.md). Read the [shared contracts](00-shared-contracts.md) and [Phase 3](03-bounded-daily-use.md) first. This phase changes execution infrastructure after the personal workflow and durable allowances are working. It preserves their observable behavior, data identities, source snapshots, and allowance rules.

## Finished outcome

At the end of **4A**, the personal workflow runs without an always-running Celery worker or scheduler; Redis still supplies temporary cache/rate-limit dependencies. At the end of **4B**, the application and PostgreSQL are the only persistent services. A temporary managed child process performs a requested update. The application serves the browser interface itself.

The existing larger service stack remains an optional, mutually exclusive processing mode. Runtime transport and report behavior are separate: a Celery-delivered personal coordinator uses the personal inputs/coverage rules, while original legacy reporting retains its New York window and selection semantics. This phase replaces the personal coordinator's execution transport; it does not make legacy reports equivalent to personal briefs. A database-engine migration, public hosting, synchronization, and automatic scheduling are outside this phase.

## Selected runtime design

Use a **managed Python subprocess**, launched with a fixed executable/module argument list and `shell=False`. Do not execute processing inside the request thread, launch arbitrary shell commands, or create an unsupervised detached worker. Extract the processing runner from Celery wrappers so the API and documented manual command invoke the same business logic and durable ownership contract.

The application is the supervisor for UI-triggered work. The manual command supervises its own child while the command is alive. Both launchers use the same supervisor implementation. A child also has an independent hard-deadline watchdog, so losing its launcher cannot produce an indefinitely running orphan. Store launcher/child identity for diagnostics only; a PID is not proof of ownership, because operating systems reuse PIDs.

Personal mode binds the existing local application to loopback and preserves existing local mutation safeguards. The app process count is one for this mode. Do not quietly expose the service publicly or add an account system.

## Ownership and state transitions

Add a durable workspace processing-control record in PostgreSQL with active mode, monotonically increasing fencing generation, active run/attempt identity, queue/running lease timestamps, and ownership token. Lock this record for every claim, takeover, and mode switch. It is the **single processing lock across all dates and all supported entry points**, not a lock per date alone.

1. A start request validates current personal date/retry eligibility and claims the global record plus logical-run record in one transaction. Repeating the same already queued/running request returns that run; a different run sees `processing_busy` with the active run identity. It does not start another child or wait invisibly in a second queue.
2. A queued claim receives a delivery token and a two-minute handoff lease. The launcher starts a child with identifiers only. The child atomically verifies the mode, delivery token, current fencing generation, and run state before it claims running ownership.
3. A running claim receives a new attempt/run token and a 35-minute fixed lease from its claim time. Persist actual start and deadline instants. The token/generation accompanies every processing transaction and provider-dispatch permission check.
4. A terminal attempt records its result and releases active ownership atomically. Published reports remain immutable, existing events remain available, and allowance/reservation records are never cleared by release.
5. A start/handoff failure records an explicit dispatch failure when possible. If the launcher dies before that write, the expired two-minute queued lease enables explicit eligible recovery. A stale child that arrives after replacement is rejected without business writes or paid dispatch.
6. An active running lease is not displaced just because a process has become quiet. If the process is confirmed dead and its current token is still owned, the supervisor may record failure/release. Otherwise explicit retry waits for the running lease to expire. Takeover increments the fencing generation and invalidates the old token in the same transaction.

Retain the existing total attempt ceiling of three for a logical run and the Phase 2 server-computed retry eligibility. A status check never silently retries. A terminal success remains once-per-date; switching runtime mode does not grant a new successful run.

## Fencing every processing write

Checking ownership only when a stage starts is insufficient. Every transaction that mutates captured/admitted scope, embeddings, clusters/events, run state, brief snapshots, reports/evidence, or paid-request permission must conditionally lock and verify the current processing-control record and run token in the **same database transaction** as its business writes. An ownership mismatch or expired lease aborts that transaction. Use one consistent lock order: workspace processing control → run/attempt → allowance/reservation → affected business rows. Keep these transactions short and never hold a database transaction across a provider HTTP call.

Before a provider dispatch, reacquire/check ownership while making the Phase 3 reservation/dispatch record durable. After the response, reacquire/check it before storing business results. A request already dispatched cannot always be canceled or uncharged; preserve/reconcile its ledger evidence even if its old owner cannot publish results. Accounting-only reconciliation uses request identity and an explicitly limited service path; it cannot mutate events, run scope, or reports after ownership loss.

A lost database connection stops new processing and paid dispatch immediately. In-flight work may finish externally, but its business response is not written until ownership can be verified in a new transaction. Reconnection alone never restores an old token's authority. An old process cannot mark a newer attempt failed or overwrite its terminal state.

All alternate mutation paths must honor this mode/ownership guard: Celery daily tasks, standalone ingestion/embedding/clustering/report tasks, legacy report schedules, and supported maintenance entry points. Read endpoints and ordinary saved-item/settings operations remain available. Any maintenance operation that changes processing-owned records must acquire the same processing control and run only while idle; an undocumented direct SQL editor is not a supported parallel writer.

## Deadlines, shutdown, and recovery

Keep the existing daily pipeline's deadline relationship as an explicit personal-mode contract:

| Limit | Selected value and behavior |
| --- | --- |
| Queued handoff lease | **2 minutes**. A child must claim before replacement invalidates its delivery token. |
| Graceful execution deadline | **25 minutes from running claim**. Stop starting calls, feed captures, fan-out items, and stages; cancel/wait for bounded in-flight work and attempt a durable failure/result write. |
| Hard execution deadline | **30 minutes from running claim**. Supervisor terminates the process group; a child watchdog independently enforces the same bound if the supervisor disappears. |
| Running ownership lease | **35 minutes from running claim**. It exceeds the hard deadline by five minutes, preserving the existing recovery margin. |
| Lease renewal | **None in the first personal runtime.** Longer execution requires a later deliberate change to limits/tests, not automatic heartbeat renewal. |
| Per-call deadlines | Every feed/provider/database operation must have a finite timeout no later than its remaining graceful budget. No dispatch is allowed with insufficient time to complete its configured request bound. |

Implement graceful cancellation with an explicit fatal cancellation/deadline exception shared by stage adapters and coordinator. Broad exception handlers must re-raise it rather than swallowing it as one item failure and starting more work. The existing Celery soft-time-limit behavior is the starting point, but personal execution cannot import a running Celery service to obtain cancellation.

On normal application shutdown, refuse new launches, request child cancellation, and wait at most 30 seconds for cleanup; then terminate the remaining child process group. Attempt the terminal write if ownership/database are still available. If completion cannot be recorded, leave the lease and reservations intact for explicit recovery. On restart, inspect durable state and show running/expired/retryable accurately. Do not automatically start a replacement process or count an interrupted attempt as successful.

For the manual command, Ctrl-C follows the same bounded cancellation path. The watchdog must use persisted absolute deadline instants as well as monotonic elapsed-time checks, so a restarted launcher or delayed child does not receive a fresh 30-minute budget.

## Phase 4A — Remove the queue requirement

Implement in this order:

1. Extract runner and supervisor modules that can be imported without constructing Celery or requiring a broker. Celery wrappers continue to delegate to the reusable runner in their allowed mode.
2. Add additive processing-control/fencing fields or tables and enforce the guard across all supported processing writes/dispatches. Test old-token rejection before enabling personal subprocess launches.
3. Add an explicit persisted processing mode and an idle-only mode-switch operation. The deployment setting must agree with the database mode or startup processing is blocked. On upgrade, preserve the current mode rather than silently activating personal processing.
4. Connect the UI start/retry dispatch path and a documented `python -m ...` command to the shared supervisor. Final module/command spelling is recorded in the phase evidence/setup guide once implemented.
5. Retain Redis cache/rate-limiter assembly temporarily. Health in 4A requires PostgreSQL, selected configuration, Redis, and a ready launcher; it must not fail merely because Celery/Beat is absent.
6. Run identical frozen personal fixtures through the Phase 2 Celery-delivered personal coordinator and the new managed-child personal runner sequentially, comparing input identities, article/event associations, terminal state, and published outputs under deterministic providers. This proves transport parity for the personal workflow. Do not compare against the original legacy New York report selector as if its coverage or outputs should match.

The API keeps Phase 2's run/status semantics and returns a durable run identity before polling. Queueing a request does not mean it succeeded. Expose runtime mode, active run, handoff/lease/deadline timestamps, and server-derived retry reason for diagnostics. Do not add a fake percentage or silently label the child “Celery.”

**4A closes only when** the UI action and manual command both complete the bounded daily workflow with Celery worker and scheduler stopped, and all concurrency/deadline tests below pass. The temporary Redis dependency must remain documented.

## Phase 4B — Make Redis optional

Assemble personal-mode prompt caching and rate limiting in process. Reuse the existing cache/limiter interfaces; choose a bounded LRU cache of **256 entries and 16 MiB total encoded key/value bytes, with a one-hour TTL** per runner. An oversize entry is not cached. Losing cache content on exit is acceptable because request cost reservations remain durable.

Use the configured per-provider request/minute and token/minute ceilings in the one active runner. Start rate buckets empty on a fresh process so repeated short processes cannot repeatedly receive an unearned full burst. Provider calls are serialized in the first personal runner. A restart may wait for bucket refill; that is preferable to pretending the previous process made no requests. Enforce provider `Retry-After` and finite request deadlines without extending the global runtime deadline. This local limiter is throughput control; PostgreSQL reservations and the global processing lock remain the authority for monetary and concurrency limits.

Adapt dependencies, health, cleanup, and API runtime assembly so disabled Redis/Celery objects are never initialized as a side effect of reads, settings, raw-mode runs, or shutdown. Health returns explicit `not_required_in_personal_mode` status for those components. A selected live capability with missing credentials remains visibly unavailable without breaking reading of stored content.

Serve the existing frontend/static assets from FastAPI in personal mode. Preserve API routes and error responses; browser-route fallback must not turn an unknown `/api/...` request into HTML success. Use one documented local address for Today, Saved, Briefs, and API traffic. Keep the existing frontend technology; this is not a frontend rewrite.

Provide a personal startup configuration with only the application and PostgreSQL as persistent services, plus the on-demand child. Provide a shutdown command, backup/restore instructions, and the explicit idle-only switch back to the legacy processing mode. Personal startup must not spawn hidden Redis/Celery/Beat processes to pass its checks.

## Mode switching, migration, and rollback

Modes are mutually exclusive **for processing writers**, even if old containers happen to be alive. A database-controlled guard rejects legacy ingestion, enrichment, scheduled briefs, and notifications while personal mode owns processing. This avoids legacy schedules bypassing personal allowances or changing brief inputs. Do not send external notifications as part of conversion verification.

Before switching, require no queued/running owner with an unexpired lease, no unconfirmed live child, and no processing transaction holding the global control. Failed/expired records remain in history; reconcile ownership conservatively rather than deleting them. Uncertain paid requests remain reserved and can be reconciled independently; switching mode does not erase them.

Apply additive migrations to a backup copy first. Confirm existing articles, events, reports/versions, saved entries, run identities, and spending records remain available. Downgrading the runtime means selecting the previous supported mode while retaining the upgraded schema/data; destructive down-migrations are not part of rollback. Old application binaries that cannot honor mode guards must not be run as writers against the upgraded shared database. A full older-version restore uses a separate validated backup database rather than discarding current work in place.

## Acceptance evidence

| ID | Input/action | Required result |
| --- | --- | --- |
| P4-01 | Establish a deterministic frozen-fixture baseline using the Phase 2 Celery-delivered personal coordinator; then stop Celery worker and scheduler, retain Redis for 4A, and invoke UI and manual command sequentially. | Managed-child execution preserves the personal baseline's input identity, article/event associations, terminal state, and published outputs or controlled partial result. No broker publish is required. Original legacy New York reports are excluded from this parity comparison. |
| P4-02 | Send two start requests for the same run simultaneously. | One logical run/attempt/child; the other response identifies the existing run. No doubled admission or paid dispatch. |
| P4-03 | Compete across different dates, API and CLI, or direct Celery and personal entry points. | One global owner; forbidden mode receives a clear rejection before mutation/paid work. Legacy scheduled ingestion cannot bypass the guard. |
| P4-04 | Delay child claim past the two-minute handoff lease, explicitly recover, then release old child. | Old delivery token is rejected. Only the replacement can acquire running ownership. |
| P4-05 | Inject a graceful deadline during an item/provider operation. | No next item/stage begins; the run unwinds and records failure where possible. Broad exception handlers cannot turn cancellation into continued processing. |
| P4-06 | Hang the runner; kill its supervisor; test with accelerated equivalents of 25/30/35 minutes. | Independent watchdog ends the child by its original hard deadline. Lease cannot be renewed silently; retry is only permitted under the explicit recovery rule. |
| P4-07 | Old attempt returns from a provider after takeover; attempt event/report writes and a failure-state write. | Each stale business transaction is rejected; newer outcome remains unchanged. Its already incurred request cost remains reconcilable. |
| P4-08 | Lose PostgreSQL mid-run and reconnect after ownership has changed. | No new paid dispatch while DB is unavailable; old token cannot resume writes. No reservation or input freeze disappears. |
| P4-09 | Normal shutdown, Ctrl-C, hard crash, then app restart. | Bounded child cleanup; durable interrupted/retry state; no automatic replacement or new allowance. Saved items and published history remain accessible. |
| P4-10 | Stop Redis as well as Celery/Beat, then launch 4B. | Only app/PostgreSQL persist; real browser reading/saving/update/brief/export work; health says Redis/Celery not required. |
| P4-11 | Repeated fresh subprocesses issue provider calls; overflow local cache; restart app. | Empty-start rate buckets respect configured pacing; cache stays bounded; spending/quotas remain durable and unchanged by cache loss. |
| P4-12 | Load a browser route and an unknown API route from the same app address. | Browser interface loads; API response retains its actual error status/content type rather than returning frontend HTML. |
| P4-13 | Switch modes during an active run, then once genuinely idle. | Active switch is rejected; idle switch is durable and invalidates stale launch tokens. Mode mismatch on startup blocks writes without deleting records. |
| P4-14 | Back up/restore a populated database to a disposable instance; switch to previous supported runtime mode. | Saved events, articles, report versions, spending, and run/snapshot identities are retained and readable. Rollback does not drop new tables or revive old unguarded writers. |

Use deterministic providers to compare equivalent runs; generative output is not required to be byte-identical for unrelated live requests. The crucial invariants are the same frozen inputs, evidence references, durable outcomes, and costs. Record process/service inventory, concurrency outcomes, deadline timings, recovery results, browser evidence, and backup/restore results before closing 4A or 4B separately.

## Existing starting points

- `workers/pipeline_tasks.py` and `pipeline_stages.py`: Celery-specific wrapper, fatal exceptions, and production stage adapters.
- `services/pipeline/coordinator.py`, `contracts.py`, and `sqlalchemy_store.py`: lifecycle, ownership, queue/running leases, and attempt ceiling.
- `db/models/pipeline.py`: current per-date run records; add workspace-wide ownership without changing legacy report meaning.
- `apps/api/pipeline.py`, `deps.py`, `health.py`, and `main.py`: dispatch, readiness, dependency assembly, and static serving.
- `services/llm/runtime.py`, `cache.py`, and `limiter.py`: current Redis production assembly and reusable interfaces/local primitives.
- `workers/celery_app.py` and standalone worker entry points: current schedules and 25/30-minute execution limits; every supported processing writer must respect active mode.
- Existing Compose/startup configuration and `README.md`: document the two-service mode and explicit lifecycle/backup operations.

Completing this phase makes operation simpler. It does not by itself establish that the news is relevant or the summaries are useful; that evidence belongs to Phase 5.
