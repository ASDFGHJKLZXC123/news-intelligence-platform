-- Preview rows that are candidates for lifecycle cleanup.
-- This file is read-only: it intentionally does not delete or update rows.

SELECT
  'raw_ingestion_items_older_than_90_days' AS bucket,
  count(*) AS row_count,
  min(created_at) AS oldest_at,
  max(created_at) AS newest_at
FROM raw_ingestion_items
WHERE created_at < now() - interval '90 days'

UNION ALL

SELECT
  'source_health_snapshots_older_than_90_days' AS bucket,
  count(*) AS row_count,
  min(created_at) AS oldest_at,
  max(created_at) AS newest_at
FROM source_health_snapshots
WHERE created_at < now() - interval '90 days'

UNION ALL

SELECT
  'provider_runs_older_than_365_days' AS bucket,
  count(*) AS row_count,
  min(created_at) AS oldest_at,
  max(created_at) AS newest_at
FROM provider_runs
WHERE created_at < now() - interval '365 days'

UNION ALL

SELECT
  'raw_document_assets_older_than_90_days' AS bucket,
  count(*) AS row_count,
  min(created_at) AS oldest_at,
  max(created_at) AS newest_at
FROM raw_document_assets
WHERE created_at < now() - interval '90 days';
