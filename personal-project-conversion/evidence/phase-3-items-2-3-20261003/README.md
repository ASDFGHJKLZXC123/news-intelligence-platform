# Phase 3 remaining-work items 2 and 3

Date: October 3, 2026 (America/Los_Angeles). Scope: spending API metadata and the daily-pipeline migration-head fixture only. Both items are implemented and freshly verified. Phase 3 remains in progress; no global acceptance status was promoted by this work.

## Item 2: spending contract

`services/personal/spending.py:spending_summary` adds two fields to the existing response used by `GET /api/v1/personal/spending`:

- `current_configuration`: the active profile UUID/revision, route mode, and compact generation/embedding identifiers (role, provider, model, model version, price revision). An absent profile has explicit null metadata and an empty route list; a configured but disabled route stays visible alongside the existing disabled status.
- `accounted_routes`: deterministic, deduplicated identities from the requests underlying current-period finalized charges/reservations and all retained unresolved paid requests, including older months. Each entry identifies its source and UTC period. Distinct price revisions remain distinct even for the same provider/model/month. Imported and not-yet-imported legacy usage exposes its original audit provider/model; role, model version, and price revision are null because they were not authoritatively retained.

The response uses explicit public field projection and the existing settings sanitizer. It omits arbitrary stored extras, provider request IDs, evidence, receipts and credentials. Historical requests use their retained routes, never today's configuration. Monetary aggregation, statuses, dispatch-month semantics, old unresolved obligations, local reset timestamps and read-only GET behavior are preserved. The only additional absent-profile behavior avoids loading a null primary key. The API already returns a dictionary and the frontend reads existing named fields, so no endpoint, frontend, database schema or response-model redesign was required. The contract is documented in [03-bounded-daily-use.md](../../03-bounded-daily-use.md).

[Focused regressions](../../../tests/integration/test_personal_spending_metadata.py) cover changed/disabled current settings versus frozen charged/reserved routes; older unresolved versus older finalized/cancelled/non-billable records; duplicate identities; secrets in stored extras/receipts; explicit empty setup; legacy metadata before/after import without GET mutations; and same-month charges using two real ledger price revisions with exact aggregate costs. All provider identities, prices, usage and receipts are synthetic.

## Item 3: migration fixture

`tests/integration/test_daily_pipeline_vertical_slice.py:migrated_pipeline_db` resolves the repository's single Alembic head with `ScriptDirectory`, asserts that it exists, uses the same configuration to upgrade, and asserts exactly one applied revision equals that head with `scalar_one()`. The verified current head is `0022_personal_spending`; no revision is hard-coded in the test.

The previously modified test was preserved. Its sole baseline difference from Git was the older `0019` expectation already changed to `0020`; [baseline diff](baseline-vertical.diff) retains that work. Disposable database isolation, engine disposal, identity-specific cleanup/leak assertion, deterministic descriptive stages, predictive exclusions, enqueue and replay/idempotency assertions remain intact. No production migration/downgrade guard changed.

## Actual fresh checks

All Python tests set `APP_ENV=test` and disable bytecode/cache writes. The four paid-provider credentials were explicitly empty throughout. The final checks additionally clear FRED/EIA and application API keys; the inherited NASA FIRMS credential was verified absent, and the retained runner now explicitly clears it too. Test settings skip developer dotenv credentials. There was no provider/feed HTTP request, broker or worker-service launch, paid activation, recurring allowance or preference change.

| Check | Fresh result | Retained output |
| --- | --- | --- |
| Before-fix daily vertical slice | 1 setup error in 4.19s, reproduced `0022_personal_spending != 0020` | [baseline-vertical.log](baseline-vertical.log) |
| Before-fix spending metadata tests | 3 expected missing-key failures in 7.75s | [baseline-spending.log](baseline-spending.log) |
| Intermediate repaired integration run | 16 passed in 44.63s | [interim-integration.log](interim-integration.log) |
| Final existing spending + four metadata + repaired vertical tests | **17 passed in 34.89s**, no skips or failures | [final-integration.log](final-integration.log) |
| Spending/settings unit suites | **55 passed in 0.71s** | [unit.log](unit.log) |
| Whole repository Ruff, no fixes | **Passed** | [lint.log](lint.log) |
| Git diff whitespace check | **Passed** | [diff-check.log](diff-check.log) |

[verify.py](verify.py) retains the exact integration selections and resource checks; [checks.json](checks.json) records the unit/lint commands and cleared fields. An earlier lint run found only the new evidence runner's intentionally delayed imports; explicit E402 annotations document environment initialization before test imports. The final whole-repository check passed. The saved September 20 full-suite totals (4,192 unit, 74 frontend; 214 database passes with two historical hotness failures and one fixture setup error) were inspected as history, not rerun or promoted to current results. No broad suite or browser gate was rerun for these two backend/test changes.

## PostgreSQL identity and cleanup

The existing dedicated Phase 3 test container was verified before use: `nip-phase3-tests-02ea38048d`, exact ID `1e7c72d3ff3bd27f581ea5682e5b8a51206ddf98118be06cea933fb8bb2b55b6`, ownership label `nip.phase3.owned=nip-phase3-tests-02ea38048d`, PostgreSQL exposed only at `127.0.0.1:58553`. It was reused and left running; no container, volume, shared database, unrelated service or existing record was removed.

The existing Stage 7 harness and pipeline fixture created only their unique disposable databases. The runner observes CREATE, retains each database OID, verifies the same name/OID and maintenance connection immediately before DROP, separately checks the disposable's `current_database()` and applied migration head, and confirms the name is absent afterward. All **17 final-run databases** reached `0022_personal_spending`, were identity-checked, and were removed. The final pipeline database was `nip_pipeline_slice_acf988f731b5`, OID `1108140`. Every baseline and intermediate disposable was also removed.

Before/after database name/OID inventories match for every run. The two pre-existing `nip_phase3_*` databases and maintenance/templates remain unchanged; the shared development server on port 55432 was never a verification target. Exact names, OIDs, migration revisions, checks and cleanup results are retained in [final resource evidence](final-integration-resources.json), [baseline vertical evidence](baseline-vertical-resources.json), [baseline spending evidence](baseline-spending-resources.json) and [intermediate evidence](interim-integration-resources.json). [Preservation manifest](preservation.json) checks all 831 baseline files and restricts changes to the four scoped implementation/docs files plus the new test/evidence.

## Independent scoped reviews

Two read-only reviewers found no actionable issue:

- Spending reviewer inspected retained request/legacy schemas, safe settings, metadata projection, unchanged money expressions, API compatibility, tests and contract. It independently executed synthetic route projection cases for price revisions, duplicate removal, deterministic order, old unresolved inclusion, irrelevant-state exclusion and secret omission. Its suggestion for explicit same-month price coverage was added and passed in the final PostgreSQL run.
- Fixture reviewer verified dynamic single-head resolution, exact applied-version cardinality, current repository revision and preservation of isolation/cleanup/workflow/replay assertions. The fixture implementer also compared ASTs and verified only the fixture changed relative to the initial dirty test.

Reviewers made no database/network calls or shared-checkout edits; their review is code/synthetic evidence, while PostgreSQL proof comes from the fresh tests above.

## Remaining boundaries

No necessary action was blocked by automatic approval review in this scoped run. Ordinary paid-worker integration remains unavailable/disabled, and the previously rejected historical Stage 6 migration-isolation fixes were not attempted. The paid ledger retention/downgrade guard remains intact. Items 1 and 4–7, global P3-01–P3-14 acceptance, ordinary end-to-end paid operation and any later phases are outside this assignment and remain pending as previously recorded. No commit, reset, stash, cleanup of user work, push or publication was performed.
