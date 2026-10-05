# Item 4 — historical hotness migration-test isolation

Date: October 3, 2026. Scope: the two named historical migration tests in `tests/integration/test_stage6_brief_selection.py`, their directly related verification, and owned disposable PostgreSQL resources. Fresh human authorization permits this narrow repair; the [September 20 rejection record](../phase-3-20260920/test-isolation-blocker.md) remains unchanged history.

**Implementation and focused verification complete.** The new function-scoped `historical_hotness_db` uses a unique `nip_stage6_hotness_<uuid>` database and bootstraps only migration 0016. Each repaired test exercises the actual `0016 → 0015 → 0016` path. The subprocess helper permits only those revisions, supplies explicit `APP_ENV=test` and the owned `DATABASE_URL`, uses the repository-root working directory and a finite timeout, and checks the actual database identity and Alembic revision after each command.

Bootstrapping through 0016 before downgrading is intentional: migration 0002 builds from current ORM metadata, so a fresh database upgraded only to 0015 can already contain `hotness_score`. The actual 0016 downgrade constructs the historical missing-column state before preservation/upgrade assertions run. No Phase 3 ledger is installed or traversed by these two tests.

The preservation test still inserts a real legacy event at 0015 and checks its retained severity and unknown/NULL hotness at 0016. The reversibility test still checks removal and restoration of the column and index. The existing live CHECK constraint test, all four repository/selection tests, and their current-head `selection_db` fixture are unchanged. [Source verification](migration-source-verification.json) compares their function bodies against tracked HEAD and verifies that migration 0016 and the 0022 retained-ledger guard have the same SHA-256 values captured before editing. Production migrations and shared database helpers were not edited.

## Fresh checks

All database commands explicitly set `APP_ENV=test`, `PYTHONDONTWRITEBYTECODE=1`, `REQUIRE_POSTGRES=1`, `POSTGRES_HOST_PORT=64707`, `DATABASE_URL=postgresql+psycopg2://news:news@localhost:64707/postgres`, and empty `OPENAI_API_KEY`, `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, and `DEEPSEEK_API_KEY`. They used `.venv/bin/pytest -q -p no:cacheprovider` with only the targets listed below. Test fixtures created their own unique databases; the maintenance database was used only for availability and lifecycle checks. No shared development database, external feed/provider, paid request, or runtime activation was used.

| Check | Expected | Actual | Evidence |
| --- | --- | --- | --- |
| Both original historical tests, before editing | Reproduce retained-ledger downgrade rejection | 2 failed in 6.21 seconds, both at unchanged `0022_personal_spending.downgrade` | [Failing baseline](migration-before.log) |
| Entire `tests/integration/test_stage6_brief_selection.py`, after editing | Both repaired tests and the unchanged current-schema regressions pass | 7 passed in 12.43 seconds | [Result](migration-after.log) |
| `tests/integration/test_alembic_smoke.py::test_phase3_downgrade_refuses_to_remove_retained_state` | Existing 0021/0022 retention guards still refuse and preserve state | 2 passed in 4.87 seconds | [Result](migration-retention-guard.log) |
| `.venv/bin/ruff check --no-cache tests/integration/test_stage6_brief_selection.py` | No lint issues | Passed | Fresh command output |
| `.venv/bin/ruff format --check --no-cache tests/integration/test_stage6_brief_selection.py` | Formatting unchanged | 1 file already formatted | Fresh command output |
| `git diff --check -- tests/integration/test_stage6_brief_selection.py` | No whitespace errors | Passed | Fresh command output |
| Independent read-only scoped review | No actionable isolation/preservation/cleanup findings | No actionable findings | [Review](migration-independent-review.md) |

The first formatting-only check omitted `--no-cache` and could not create a sandbox cache file; the successful no-cache check above completed without modifying the source.

## Resources and cleanup

The server is newly owned by this assignment: container `nip-p3-items14-7ca9fcb920f7`, exact ID `33a5ddf17f2b38d328e9634f9071ff7a7f8d0922f83d5afc1cee7c217979c424`, label `nip.verification=7ca9fcb920f7417e99aed95bae46e54b`, random host port 64707 bound to `127.0.0.1`. It reused the already local PostgreSQL/PostGIS/pgvector image; no image pull was performed. [Ownership](migration-postgres-ownership.json) and [preexisting container inventory](migration-containers-before.json) retain its identity and preservation baseline.

All **11 migration-related disposable databases** were created uniquely and dropped: 2 failing-baseline databases, 7 Stage 6 verification databases, and 2 retained-state verification databases. The existing fixture disposes its engine, terminates only its named database sessions, drops that database, and verifies no leak even when a test fails. [DDL lifecycle log](migration-database-lifecycle.log) records every CREATE/DROP; an identity-checked maintenance query found no retained test database names. [Cleanup evidence](migration-database-cleanup.json) retains the exact names and result.

**Final container cleanup complete.** After the parent worker/regression and independent-review checks finished, the exact container ID, name, verification label, image, creation time, and loopback port were verified again. The complete assignment created and dropped **97 uniquely named databases**, including this record's 11 migration databases; the maintenance query found zero remaining assignment databases. The exact owned container was removed and verified absent. All **17 preexisting containers** retained their IDs, names, creation/start times, running states, and port mappings. [Final cleanup and inventory comparison](resource-cleanup.json), [all-assignment database lifecycle log](resource-database-lifecycle.log), and [complete retained PostgreSQL DDL log](resource-postgres-full-ddl.log) provide the combined proof. The earlier migration-only cleanup JSON remains a snapshot taken while the server was still temporarily available to the parent.

This record closes checklist item 4's test-isolation repair and focused checks only. It does not claim global Phase 3 acceptance, live runtime/provider verification, or completion of items 5–7.
