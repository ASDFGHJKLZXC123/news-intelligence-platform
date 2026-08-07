# Database Operations Runbook

This runbook covers the local and production database controls required by the
database setup plan. Alembic migrations remain the source of truth for schema changes.

## Backup

- Local logical backup: `make db-backup`
- Required environment: `DATABASE_URL`
- Output location: `BACKUP_DIR` or `backups/postgres`
- Format: custom `pg_dump` archive, no owner, no ACL
- Every backup is written atomically with a sibling `.sha256` checksum. Both files are
  mode `0600` and excluded from Git and Docker build contexts.
- Application URLs such as `postgresql+psycopg2://...` are normalized to the native
  `postgresql://...` form before `pg_dump`, `psql`, or `pg_restore` runs.

Production should use daily logical backups for small deployments and PITR/WAL
archiving when the database stores user or revenue-critical data.

## Restore Drill

- Automated drill: `make db-restore-drill`
- The drill creates separate source and restore-target Compose projects from the same
  PostgreSQL image, migrates the source, backs up sentinel data, destroys the source
  volume, restores to the target, and verifies the archive and data checksums, Alembic
  revision `0019`, pgvector, PostGIS, and complete cleanup.
- Manual restore is intentionally fail-closed. It requires both an exact target name and
  an explicit disposable acknowledgement:

  ```sh
  make db-restore \
    file=backups/postgres/<backup>.dump \
    disposable=1 \
    database=<exact-disposable-database-name>
  ```

- The restore script verifies the sibling `.sha256` file and the connected database name
  before it validates or applies the archive. It always refuses the `postgres`, `template0`,
  and `template1` databases. The restore is applied in one transaction and exits on the
  first SQL error.
- Run one restore drill before every release that changes database structure.

## Security

- Do not expose the PostgreSQL port publicly in production.
- Use a separate migration role and runtime application role.
- Runtime role should not own tables and should only receive required DML privileges.
- Keep `DATABASE_URL`, API keys, and provider credentials in environment variables or a
  secret manager.
- Avoid storing unnecessary PII; isolate user/workspace data in the application tables.

## Retention

- Raw provider payloads: keep 90 days locally unless object-storage retention requires more.
- `raw_document_assets`: keep database pointers while the object exists; expire bulky raw
  assets by provider policy.
- Source health snapshots: aggregate or archive after 90 days.
- Provider runs: keep detailed rows for one year, then aggregate operational metrics.
- Risk score observations and model outputs: retain indefinitely when storage permits.

Run `make db-retention-preview` to inspect candidate row counts. It is intentionally
read-only and must be reviewed before any destructive cleanup job is introduced.

## Monitoring

Track these metrics before production cutover:

- query latency by endpoint;
- slow query log;
- table and index bloat;
- Alembic migration duration;
- provider ingestion volume and failure rate;
- worker queue depth;
- database connection pool saturation.

Use `EXPLAIN ANALYZE` before adding complex indexes. Pagination should remain enforced
for event, article, company, evidence, alert, and report lists.
