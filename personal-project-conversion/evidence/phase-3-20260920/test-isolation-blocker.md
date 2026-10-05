# Historical hotness-test isolation blocker

September 20, 2026. Two existing tests in `tests/integration/test_stage6_brief_selection.py` remain unchanged:

- `test_migration_adds_hotness_without_disturbing_existing_events`
- `test_migration_is_reversible`

The original complete integration run reached Phase 3 migration `0022_personal_spending` during their downgrade-to-0015 setup and stopped at its required ledger-retention guard. See `full-integration.log`, especially lines 582–742. The proposed correction was test-only: use an owned disposable database restricted to the historical 0015/0016 boundary, with explicit subprocess `APP_ENV` and `DATABASE_URL`, without changing production migrations or the other Stage 6 fixtures/tests.

Automatic approval review rejected the first patch with this exact reason:

> The patch modifies an unrelated Stage 6 migration test and adds subprocess migration behavior, outside the user-authorized Phase 3 scope and contrary to the instruction not to begin unrelated platform work.

After reading and presenting the exact Phase 3 regression trace, one final narrower retry was rejected with this exact reason:

> This is a retry of the previously rejected unrelated Stage 6 migration-test change, adding subprocess Alembic execution outside the authorized Phase 3 scope; no trusted user approval was provided.

Neither patch was applied. No alternative agent/tool or indirect execution was used to make those changes, and no additional retry is planned. Explicit user approval is required to proceed with that test isolation. The production downgrade guard remains intact; these failures must remain visible in the final integration result rather than being reported as passing coverage.
