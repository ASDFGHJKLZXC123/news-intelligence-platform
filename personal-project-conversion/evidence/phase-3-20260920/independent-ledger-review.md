# Independent bounded ledger review — 2026-09-20

Reviewer: `capture_bounds` agent, independently reviewing code implemented by the root and spending agents. The reviewer implemented the separate RSS capture adapter and intake tests, not the ledger/runtime/migration reviewed here.

Scope: `services/personal/spending.py`, `services/personal/paid_runtime.py`, `db/models/personal_spending.py`, migration `0022_personal_spending`, and the relevant settings/run lifecycle interactions. Reviewed against Phase 3's shared paid-request ledger and P3-08 through P3-11 obligations. No provider credentials, paid requests, external feed requests, or shared development database were used.

**Final disposition after correction review:** all three findings below are addressed within this review's bounded scope. The reviewer inspected the final ownership and legacy changes, inspected retained results for 13 PostgreSQL and 39 unit tests, and independently reran the absolute-deadline behavior against a real owned loopback server. Ordinary live worker execution remains unavailable and is not certified by this review.

## Findings

### R1 — P1: Ownership replacement could race the durable dispatch handoff

**Reported location:** `SpendingLedger._context`, initially `services/personal/spending.py:343–357`; related lifecycle functions `retry_run`, `acquire_run`, and `finish_run` in `services/personal/runs.py`.

The initial implementation held the workspace spending advisory lock but read the run's token/state without a row fence. Retry and lifecycle transitions did not take that advisory lock. A dispatch could read an eligible token, pause before committing `dispatching`, and then resume after another transaction had replaced the owner or terminalized the run. Rechecking at the start of the transaction alone did not prevent that interleaving.

**Proposed reproducer:** reserve one request; pause dispatch immediately after `_context`; in another connection obtain `FOR UPDATE` on the run and replace its token/attempt; release dispatch. Before correction the replacement could commit before the old dispatcher committed its handoff. Also exercise terminalization during that same barrier.

**Correction inspected:** `_context` now selects the run with `.with_for_update(read=True, key_share=True)` (`FOR KEY SHARE`). This remains compatible with the coordinator's `FOR NO KEY UPDATE` business transaction and conflicts with the full `FOR UPDATE` used for ownership replacement. `finish_run` now upgrades its run lock to full `FOR UPDATE` before terminal state changes. The independent ledger transaction commits before its caller terminalizes the run, so the chosen lock mode preserves the intended separate accounting commit.

**Final status:** addressed. Independently inspected the final source: `_context` uses real `FOR KEY SHARE` (`spending.py:371–375`) and `finish_run` escalates to full `FOR UPDATE` (`runs.py:310–314`). The final regression `test_dispatch_handoff_fences_concurrent_ownership_changes` exercises both production `retry_run` and production `finish_run` behind a deterministic dispatch barrier. Its retained PostgreSQL result is included in [spending-reviewed-fixed-integration.log](spending-reviewed-fixed-integration.log): 13 passed in 26.40 seconds. This reviewer inspected that result rather than independently rerunning those PostgreSQL tests.

### R2 — P1: Historical synthetic zero costs were imported as confirmed free usage

**Reported locations:** initial `import_legacy_usage` classification in `services/personal/spending.py:232–244`, migration `db/migrations/versions/0022_personal_spending.py:89–96`, and the unimported legacy read-summary path.

Non-null `LLMRun.cost_usd` was initially treated as established usage. The existing orchestrator explicitly constructs a zero-token response when a provider exception has no trustworthy usage (`services/llm/orchestrator.py:638–672`), and the existing pricing helper returns zero for missing model pricing (`services/llm/pricing.py:28–30`). Those zero values do not establish that a real request was free. Importing them as finalized zero bypassed the unresolved current-month usage gate.

**Proposed reproducer:** seed a current-month failed OpenAI LLM audit with `error_message="provider invocation failure"`, zero input/output tokens and `cost_usd=0`; import it. Before correction its `actual_usd` became zero, unknown count remained zero, and the legacy paid-enablement gate passed. A successful positive-token audit with missing pricing could produce the same false result.

**Correction inspected:** `established_legacy_cost` now accepts positive recorded cost, or an explicit cache-hit/pre-dispatch token-bucket/offline-adapter fact for a zero-cost record. Otherwise it retains an unknown. The migration implements the same classification, and the read summary reuses the runtime classifier.

**Independent execution after correction:** instantiated the real `LLMRun` model and called `established_legacy_cost` without a database or provider. Output:

```text
provider_timeout_zero unknown
missing_price_zero unknown
known_cache_hit 0
```

**Final status:** addressed. Correction independently inspected and the pure classifier independently exercised. The reviewed final PostgreSQL log records 13 passing checks, including `test_personal_bounds_upgrade.py`'s populated migration case with an explicit historical provider-failure zero. [spending-unit-final.log](spending-unit-final.log) records 39 unit checks, including the legacy-zero classification cases. These log results were inspected, not rerun by this reviewer.

### R3 — P2: The configured request deadline is only a per-operation timeout

**Locations:** `services/personal/paid_runtime.py:135` and `_client` at approximately lines 190–199.

The adapter passes `deadline_seconds` to HTTPX as `timeout`. The installed HTTPX implementation documents and sets that value separately for connect/read/write/pool operations (`httpx/_config.py:72–130`); it does not impose a total wall-clock request deadline. A provider or proxy that keeps sending response chunks before each read timeout can extend a request beyond the configured deadline indefinitely. This does not release the monetary reservation, but it fails the Phase 3 finite request-deadline contract.

**Independent deterministic reproduction:** launched one owned `ThreadingHTTPServer` on an ephemeral `127.0.0.1` port. It returned a synthetic JSON usage body in ten-byte chunks, separated by 0.3 seconds. Called the production `DurablePaidHTTPClient` through a real HTTPX client with route `deadline_seconds=1` and a mock ledger. The request returned success and reconciled after the deadline:

```json
{"configured_deadline_seconds": 1, "elapsed_seconds": 1.522, "http_status": 200, "reconciled_calls": 1, "network": "ephemeral-loopback-only", "provider_calls": 0}
```

The server was shut down, its socket closed, and its owned thread joined in `finally`. No shared process was stopped. No paid provider, external endpoint, or PostgreSQL connection was involved.

**Correction inspected:** the production factory now returns `DeadlineHTTPClient` (`paid_runtime.py:240–245`). That transport owns an async HTTPX client inside `asyncio.timeout`, covering connection, response headers, and body consumption; the synchronous facade owns its event loop. Cancellation closes the request transport instead of leaving an executor/request thread active. The durable guard marks a timed-out dispatched request uncertain without cancelling its reservation.

**Independent execution after correction:** reran the real loopback reproduction with the final production `DeadlineHTTPClient` through `DurablePaidHTTPClient`. Provider keys were blank. The ledger was mocked so this transport check could not access any application database. Retained complete output: [independent-ledger-deadline-recheck.json](independent-ledger-deadline-recheck.json).

| Synthetic response | Deadline | Observed elapsed | Outcome | Uncertain calls | Reconciliation calls | Reservation cancellations |
| --- | --- | --- | --- | --- | --- | --- |
| Body chunks every 0.3 seconds | 1 second | 1.004 seconds | Total deadline timeout | 1 | 0 | 0 |
| Headers delayed 1.3 seconds | 1 second | 1.002 seconds | Total deadline timeout | 1 | 0 | 0 |
| Timely complete response | 1 second | 0.007 seconds | HTTP 200 | 0 | 1 | 0 |

The millisecond excess is normal timeout cancellation/return overhead. Both delayed cases terminated at the absolute deadline rather than continuing to consume a progressing response. The owned server was stopped, its socket closed, and all owned server threads joined; `owned_server_stopped=true`. Provider calls remained zero.

**Final status:** addressed and independently rechecked. The ordinary paid worker remains disabled; the recheck proves this transport/accounting callback boundary, not ordinary end-to-end live paid execution.

## Other reviewed boundaries and limitations

- Monetary allowance decisions use fixed-precision Decimal and the workspace spending lock; run usage includes all attempts, while month usage includes actual costs and retained unresolved reservations.
- Dispatch revalidates the current UTC spending bucket; reconciliation retains that dispatch bucket. Confirmed pre-dispatch cancellation checks that no dispatch handoff was recorded.
- An ambiguous dispatched result is retained rather than automatically expired into available credit. Provider retry calls pass through the HTTP guard and require separate request identities.
- Known route identity, prices, token limits and accounting are persisted. This review did not verify current external provider prices or activate any live route.
- The ordinary paid-worker integration intentionally remains unavailable. Its disabled state must remain disclosed and is not replaced by claiming that direct ledger/adapter tests prove ordinary end-to-end paid operation.
- This is a bounded source/concurrency review plus the independent classifier and before/after loopback executions recorded above. The final retained 13 PostgreSQL and 39 unit results were inspected. This review is not a new full-suite pass, live-provider proof, or authorization to spend.
