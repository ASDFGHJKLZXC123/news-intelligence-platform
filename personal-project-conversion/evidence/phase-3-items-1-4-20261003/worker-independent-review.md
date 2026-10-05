# Independent scoped review: ordinary personal worker

Date: October 3, 2026 (America/Los_Angeles). Reviewer: independently delegated worker reviewer. Scope: remaining-work item 1 only, with the captured `worker-baseline.py.txt` used to distinguish new worker composition from the extensive existing uncommitted implementation. No production implementation was edited by this reviewer.

## Review finding

**P2 — transfer eligibility omitted the new disabled-runtime reason: corrected and independently verified.** At initial review, `services/personal/coordinator.py:560-569` allowed transfer of terminal intentionally disabled raw captures but omitted the worker's new `paid_runtime_disabled` reason. A successful raw update with the server gate off persisted that reason and its ungrouped articles could not enter a later enabled assisted run. The reviewer reported this to the implementer before any correction. The related `_finish_raw` classification treated this intentionally disabled stage as blocked. The implementer's new regression failed before correction: the later run admitted no duplicate article but observed zero events instead of one ([red result](worker-transfer-before.log)). The narrow correction adds the new reason to transfer eligibility at current line 564 and disabled-stage classification at current lines 701-703. The reviewer inspected those bytes and independently executed the gate-off raw-to-assisted test successfully. It checks original admission ownership, explicit later processing ownership, no duplicate admission, disabled first-stage reporting and the actual guarded embedding-to-published-brief path.

No actionable findings remain in the scoped composition review. This is acceptance of the item 1 software composition, with actual runtime activation still disabled and live behavior unverified.

## Reviewed boundaries

- `Settings.personal_paid_runtime_enabled` defaults to false. Ordinary v2 worker routing first checks the frozen profile's explicit AI state and finite allowance, then the separate server activation gate and original exact stored route. Credentials and route/profile configuration alone leave the gate closed. Readiness performs configuration inspection without initializing providers or transport. Existing API hard-coded `execution_route_unavailable` branches apply only to legacy v1 profiles.
- One `SpendingLedger`, bound to the run/workspace/ownership token, is passed to both production embedding and generation/checking adapters. Every physical POST goes through `DurablePaidHTTPClient.reserve` and `dispatch`; embedding physical retries, schema corrections, composer retries and grounding calls keep that guard. The v2 route contract supports one exact generation route and one exact embedding route, without hidden fallback or independent probe route.
- The exact immutable `PaidRoute` preserves model/version, price revision/source, finite input/output bounds and deadline. Snapshot orchestration verifies route equality. The durable ledger rechecks frozen/live lower ceilings and ownership, requires frozen scopes before reservations, serializes workspace allowances and preserves uncertain obligations across rollback/retry. Its prior component implementation/evidence was inspected rather than claimed as newly implemented by this assignment.
- Ordinary raw fallback preserves bounded intake and explicit reason/status. Generation allowance blocking keeps the report failed and unpublished; known intentional allowance limitations can finish the raw update. Provider failures do not imply publication. Existing snapshot/read/saved/brief paths were inspected; no schema or historical snapshot rewriting is introduced by the worker wiring.
- Embedding closure is in the worker's `finally`; generation closure is in `generate_personal_brief`'s orchestration `finally`. Each deadline transport request owns and closes its async client, including cancellation. Test transport replacement uses `httpx.MockTransport` around the production deadline/client/provider composition.

## Independent fresh verification

Command:

```text
env APP_ENV=test PYTHONDONTWRITEBYTECODE=1 PERSONAL_PAID_RUNTIME_ENABLED=false OPENAI_API_KEY= ANTHROPIC_API_KEY= GEMINI_API_KEY= DEEPSEEK_API_KEY= .venv/bin/pytest -q -p no:cacheprovider tests/unit/test_personal_runtime_readiness.py tests/unit/test_personal_spending.py
```

Expected: offline activation, exact-route, dispatch accounting and deadline checks pass with no external provider/feed access. Actual: **76 passed in 0.72 seconds**, exit 0 ([copied tool stdout](worker-independent-unit.log)). The first read-only-sandbox attempt failed before collection because pytest could not create its temporary capture file; an ordinary reviewed escalation for the same authorized synthetic test command passed. No automatic approval-review rejection occurred.

After the implementer released a sequential test window, the reviewer independently executed these three PostgreSQL integration cases from `tests/integration/test_personal_paid_worker.py`:

- `test_gate_off_raw_articles_transfer_to_a_later_assisted_run`
- `test_api_queued_other_run_obligation_competes_with_ordinary_worker`
- `test_ordinary_failed_report_retry_reuses_snapshot_and_retains_uncertainty`

Expected: disabled raw backlog transfers once; an ordinary API-queued other-run uncertain obligation prevents further ordinary-worker generation; frozen retry keeps the original snapshot/profile/scope and retained uncertain reservations. Actual: **3 passed in 7.15 seconds**, exit 0 ([independent result](worker-independent-integration.log)). These use fake RSS and `httpx.MockTransport` around production providers/deadline transport/ledger/coordinator/report components. Synthetic Settings objects open the gate only within those disposable test processes; no real runtime settings or allowance were changed. The implementer's separate full worker result was inspected: [12 passed in 27.28 seconds](worker-final.log), not independently rerun in full by this reviewer.

The independent invocation set `APP_ENV=test`, `PYTHONDONTWRITEBYTECODE=1`, `REQUIRE_POSTGRES=1`, `POSTGRES_HOST_PORT=64707`, `DATABASE_URL=postgresql+psycopg2://news:news@localhost:64707/postgres`, `PERSONAL_PAID_RUNTIME_ENABLED=false` and the four empty provider credential variables shown above, then called `pytest.main` with `-q -p no:cacheprovider` and only the three listed node IDs. Read-only test-helper instrumentation captured identity and cleanup; no test/helper source was altered.

## Independent resource verification and cleanup

Before accessing PostgreSQL, the reviewer verified the assignment's server against [its ownership manifest](migration-postgres-ownership.json): exact container ID/name, verification label, image, running state and sole `127.0.0.1:64707` binding. The Stage 7 helper created three uniquely owned databases, all verified at `0022_personal_spending`:

| Database | PostgreSQL OID | Cleanup |
| --- | --- | --- |
| `nip_stage7_61a5f3c06c424c5db798081de65ce916` | 165617 | Removed; absence asserted |
| `nip_stage7_173c9b33890047f0b47342eef5a4d083` | 168885 | Removed; absence asserted |
| `nip_stage7_17c2f18a5d884ec7a8c074b3a5887b9d` | 172149 | Removed; absence asserted |

[Retained identity and cleanup proof](worker-independent-database-cleanup.json) records identical full database inventories before and after: only the owned server's `postgres`, `template0` and `template1`. The helper disposed engines and dropped its own disposable databases in `finally`; the reviewer asserted each absence and released the sequential window. The parent-owned test server was left running for its remaining checks; this reviewer did not create/stop containers or unrelated services. No shared development database was accessed.

No paid request, live provider/feed access, article export, real runtime activation or persistent ordinary allowance mutation was performed. No reviewer-owned databases remain. Historical September 20 accounting/rejection evidence was inspected for context and not counted as fresh verification.

This review does not declare global Phase 3 acceptance or live operation complete.
