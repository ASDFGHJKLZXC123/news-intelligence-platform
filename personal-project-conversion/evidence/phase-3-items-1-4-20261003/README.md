# Phase 3 remaining-work items 1 and 4

Date: October 3, 2026, America/Los_Angeles. **Both scoped implementation items are complete and verified with synthetic transport and disposable PostgreSQL. Real paid execution remains inactive.** This record implements the human's fresh assignment, “Do the same thing for item 1 and item 4.” It supersedes the implementation blockers for these two items without rewriting the [historical worker rejection](../phase-3-20260920/spending-implementation.md) or [historical migration-test rejection](../phase-3-20260920/test-isolation-blocker.md). No new automatic approval rejection occurred during this assignment.

## Item 1: ordinary registered worker integration

The ordinary `personal-profile.v2` assisted worker now composes the existing production coordinator with the existing durable `SpendingLedger` and guarded embedding/generation adapters. One ledger is bound to the frozen run, workspace and ownership token and passed to both adapters. All generation tiers share the exact configured provider/model and have no fallback. Every physical embedding, composition, schema correction, grounding/checking or retry POST passes through the same durable reservation/dispatch/reconciliation boundary. No independent billable probe or fallback path was introduced. [Scoped worker diff](worker-change.diff) is against the captured pre-edit file, rather than tracked HEAD, which lacks the extensive preexisting worker implementation.

The separate server setting `PERSONAL_PAID_RUNTIME_ENABLED` defaults to false. Both the current process environment and `.env` leave it unset. Route configuration, credentials, saved preferences, and a historical single-verification allowance cannot activate this gate. No real environment/configuration/preferences or application database settings were changed. Tests open the gate only on isolated synthetic `Settings` objects with placeholder credentials and a scripted `httpx.MockTransport`; production payload serialization, deadline transport, providers, coordinator, report machinery and PostgreSQL ledger remain in use.

V2 readiness uses the original exact stored route and distinguishes `paid_runtime_disabled` from missing configuration or allowance blocking. Raw collection/readability and existing saved/brief readiness remain available. Legacy v1 `execution_route_unavailable` remains intentionally scoped to the dedicated Phase 2 smoke boundary. The existing API already consumes per-feature readiness, so its legacy-only branches required no change. [Activation/readiness changes and test-first evidence](readiness-README.md) retain the configuration details and results.

The worker validates immutable model/version/price/token/deadline identities before selecting paid adapters; snapshot orchestration checks equality with the frozen route. The existing ledger rechecks ownership and frozen/live lower ceilings before each dispatch, requires frozen scopes, shares monthly/run credit across dates and API-worker entry points, and retains uncertain obligations. Generation allowance blocking leaves the report failed and unpublished with an explicit paid-work reason. Frozen explicit retries keep the snapshot, source scope, route and price revision even after active-profile edits, and do not refetch the feed. Existing component machinery was reused rather than rebuilt.

Independent review found one new composition issue: the new `paid_runtime_disabled` capture state was missing from the existing raw-backlog transfer whitelist. A fresh failing regression reproduced zero transferred events. The narrow coordinator correction adds that disabled reason to transfer eligibility and stage classification. The later assisted run now groups the retained article without another admission charge. [Independent review](worker-independent-review.md) closed this finding after its own PostgreSQL rerun; no scoped findings remain.

## Item 4: historical migration-test isolation

The two named Stage 6 historical hotness tests now use separate uniquely owned disposable databases restricted to the real `0016 → 0015 → 0016` boundary. The fixed-argument migration helper supplies explicit `APP_ENV=test` and owned `DATABASE_URL`, validates database/revision identity, and keeps a finite subprocess timeout. Meaningful preservation, column/index reversibility and other selection/constraint tests remain intact. Production migrations and their retained-ledger downgrade refusal are unchanged. [Implementation, baseline failures, source preservation, independent review and migration cleanup](migration-isolation.md) retain the complete scoped record.

## Fresh verification

All database checks used the new owned PostgreSQL server on loopback port 64707 and uniquely generated disposable databases. Every process used `APP_ENV=test` and cleared provider environment credentials. No external feed/provider request, paid probe, article export, real runtime activation or shared development database access occurred.

| Check | Expected | Actual | Evidence |
| --- | --- | --- | --- |
| New ordinary worker baseline before wiring | Fail because ordinary v2 worker cannot publish through durable adapters | 1 failed at `published == False` | [Baseline](worker-before.log) |
| Gate-off raw backlog before review correction | Expose ineligible retained article | 1 failed, zero observed events after later assisted run | [Regression baseline](worker-transfer-before.log) |
| New ordinary registered worker cases | Real composition works with synthetic transport; fallback, retries, uncertainty, API-run competition and frozen snapshot remain honest | 12 passed in 27.28 seconds | [Focused result](worker-final.log) |
| Final directly related integration regressions after formatting | New worker plus existing worker, spending/fencing, settings API, coordinator, ownership/scope and exact brief API checks pass | 50 passed in 97.02 seconds | [Final command/result](worker-related-regressions.log) |
| Readiness/settings/spending units | Default-off activation and exact route/deadline/accounting boundaries pass offline | 92 passed in 0.94 seconds | [Result](readiness-final-unit.log) |
| Stage 6 selection file | Both repaired historical tests and the other five tests pass | 7 passed in 12.43 seconds | [Result](migration-after.log) |
| Existing retained-state downgrade guards | Both existing guards still refuse retained-state loss | 2 passed in 4.87 seconds | [Result](migration-retention-guard.log) |
| Independent worker review checks | Reviewer closes the transfer finding and checks activation/accounting independently | 76 unit checks passed; 3 worker recovery/competition integrations passed in 7.15 seconds | [Review](worker-independent-review.md), [Integration log](worker-independent-integration.log), [Identified DB cleanup](worker-independent-database-cleanup.json) |
| Scoped lint, formatting and repository whitespace | Clean | Ruff passed; eight changed Python files already formatted; `git diff --check` passed | [Final tooling log](final-tooling.log) |

The final distinct integration targets total **59**: 50 related worker regressions, 7 Stage 6 checks and 2 retained-state guard cases. Independent reruns overlap those targets and are recorded separately. Intermediate `worker-first.log` and `worker-extended.log` retain test-harness corrections (reservation-size choice, spending field name, repeated monkeypatch subclass); they are not the final verification gate. Historical September 20 and completed Phase 2 totals were inspected, not rerun or included in these totals.

## Preservation, cleanup and remaining scope

[Preservation before](preservation-before.json) and [after](preservation-after.json) match: sibling-owned `services/personal/spending.py`, sibling-owned daily-pipeline fixture, production migration 0022, inactive saved preferences, staged index tree, and frozen evaluation artifact. No reset, clean, stash, commit, push or publish was performed. No items 2–3 implementation was overwritten. Historical rejection records remain unchanged.

[Final resource cleanup](resource-cleanup.json) and [full owned-database lifecycle](resource-database-lifecycle.log) retain exact container identity, disposable database CREATE/DROP evidence, absence verification and preservation of preexisting containers. Only the owned container and its uniquely named test resources were eligible for removal.

This closes remaining-work items **1 and 4**, including focused independent review. It does not audit or close the stale global P3-01–P3-14 acceptance matrix, items 5–7, live feeds/provider behavior, browser/runtime activation, recurring spending approval, or Phase 4/5. Phases 1–2 and CORE-01 remain complete; Phase 3 remains in progress. Ordinary runtime preferences and paid operation remain unapplied/inactive.
