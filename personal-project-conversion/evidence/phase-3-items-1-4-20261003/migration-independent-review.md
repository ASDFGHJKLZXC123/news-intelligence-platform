# Independent scoped review — item 4

Date: October 3, 2026. Reviewer: separate read-only collaboration agent `historical_migration_isolation/migration_review`. The reviewer inspected the current test diff, migration 0016 and its historical boundary, migration 0022's retained-ledger guard, Alembic/settings targeting, and the shared disposable database helper. The reviewer made no edits and executed no database tests.

**No actionable findings.**

- The new function-scoped fixture creates a UUID-named `nip_stage6_hotness_` database via the existing disposable helper, bootstraps only 0016, and both repaired tests exercise `0016 → 0015 → 0016`.
- The fixed-argument subprocess explicitly receives `APP_ENV=test` and the owned `DATABASE_URL`, uses repository-root cwd and a finite timeout, and checks the actual target/revision after commands. Test mode bypasses dotenv loading, preventing settings from silently selecting the development database.
- Original legacy-row severity/NULL preservation and index/column reversibility assertions remain. The live CHECK test and all selection tests retain their unchanged current-head fixture.
- Cleanup nests engine disposal inside the disposable helper's `finally`; it terminates only named database sessions, drops only the uniquely owned database, and verifies a zero leak count.
- Production migrations and the shared disposable helper are unchanged. Migration 0022 still unconditionally refuses removal of retained usage/reservations.

This is structural review. Fresh PostgreSQL results and resource lifecycle/cleanup evidence belong to the [implementation record](migration-isolation.md).
