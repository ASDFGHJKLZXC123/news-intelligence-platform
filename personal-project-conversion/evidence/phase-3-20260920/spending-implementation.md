# Phase 3 spending implementation and bounded verification

Date: 2026-09-20. Implementer: spending agent. All provider inputs and usage in these checks were synthetic; no additional paid request or live activation occurred. The root agent owns the PostgreSQL container on port 58553. These tests created, migrated, and identity-checked cleanup of their own `nip_stage7_*` databases; they did not touch the shared development database or stop the container.

## Delivered components

- Additive migration `0022_personal_spending` and retained request/legacy-usage records. Monetary values use `Numeric(28,12)` and `Decimal`, rounded conservatively upward to the storage quantum.
- A workspace advisory lock serializes allowance decisions with settings changes. Every reservation uses the frozen run ceiling and any lower current ceiling. Dispatch rechecks the owner and current UTC month under the same lock and commits `dispatching` before handing the request to transport.
- Independent sessions retain reservations through workflow rollback, process loss, timeout, and missing usage. A retry receives its own request identity. There is no expiry-to-credit mechanism. Receipt-backed reconciliation is idempotent and retains the original dispatch month.
- `FOR KEY SHARE` fences the ledger's run-ownership check against retry and terminalization, while remaining compatible with the coordinator's `FOR NO KEY UPDATE` transaction.
- Known prior application costs import once. Historical zero-cost audit records are unknown unless existing audit evidence proves cache reuse, a known pre-dispatch refusal, or the dedicated offline adapter. A current-month unknown blocks paid enablement. Read summaries disclose unimported legacy usage without GET-side mutations or double-counting guarded audit copies.
- Fixed-route guarded embedding/generation adapters account for each physical retry and refuse hidden fallback, missing prices, changed route identity, or unbounded token requests. The production transport uses an owned `httpx.AsyncClient` inside `asyncio.timeout`, enforcing one elapsed deadline across connection, headers, and body and closing transport on cancellation without request threads.
- The ordinary worker preserves raw intake when v2 AI configuration is disabled, absent, or unavailable. Ordinary live-assisted execution remains disabled with `execution_route_unavailable` when otherwise configured.

## Fresh checks and retained results

- `spending-unit-final.log`: **39 passed**. Includes strict route/price bounds, pre-dispatch rejection, uncertain timeout behavior, evidence-backed legacy-zero classification, and cancellation of synthetic slow headers and a continuously progressing response body.
- `spending-reviewed-fixed-integration.log`: **13 passed in 26.40 seconds**: 12 spending tests and the root's populated `0020`-to-head upgrade test. Covers two run dates competing for $0.03 with $0.02 reservations; crash/restart and idempotent receipts; dispatch-month movement and revalidation; live lowering/frozen raising behavior; independently committed accounting through a business rollback; concurrent production `retry_run` and `finish_run` fencing; known/unknown legacy costs and read-only disclosure; separately accounted retries through the real embedding adapter; retained obligations after rollover; and populated migration preservation.
- Scoped Ruff check passed for the two spending modules, models, migration, worker, and focused tests.
- Earlier logs remain retained: `spending-upgrade.log` records 10 passing spending checks plus a root upgrade assertion-text mismatch. `spending-reviewed-integration.log` records a migration SQL bind-parsing error; the correction uses `jsonb_build_object` and the later 13-test run passed. Neither earlier result is presented as the final gate.
- `independent-ledger-review.md` records the independent review and closes all three findings after correction. The reviewer independently exercised the real transport against an owned ephemeral loopback server: slow body timed out in 1.004 seconds, slow headers in 1.002 seconds, and a timely response reconciled in 0.007 seconds. Both timeout cases retained uncertainty without cancellation or reconciliation. Evidence: `independent-ledger-deadline-recheck.json`. The reviewer closed the server and joined its owned threads; no provider or database was contacted by that check. The implementer did not independently accept their own changes.

These are focused fresh checks. Any broader suite or browser results belong to the root's separately recorded gate; historical Phase 2 full-suite numbers are not reused as fresh coverage.

## Ordinary worker wiring limitation and automatic review rejection

Automatic approval review rejected an attempted patch to `workers/personal_tasks.py` that would have connected ordinary, explicitly configured v2 live runs to the new paid adapters. The patch was **not applied**. The stated reason was:

> This patch wires ordinary personal runs to real paid provider HTTP calls when configuration and API keys are present, contradicting the user's explicit instruction to keep paid processing and live-assisted activation disabled; it could export article data and incur charges.

The safe worker change only selects bounded raw fallback and communicates the unavailable route. The ledger and production adapters are implemented and tested with synthetic inputs, but those component checks do not establish ordinary end-to-end paid worker operation. No alternate path was added to bypass the rejection; no hidden paid probe or live fallback was introduced. Phase 3 acceptance must retain this implementation/activation limitation unless that wiring is separately authorized and reviewed.
