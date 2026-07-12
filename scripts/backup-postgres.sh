#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "DATABASE_URL is required" >&2
  exit 2
fi

backup_dir="${BACKUP_DIR:-backups/postgres}"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "${backup_dir}"

output="${backup_dir}/news-intelligence-${timestamp}.dump"
pg_dump \
  --format=custom \
  --no-owner \
  --no-acl \
  --file "${output}" \
  "${DATABASE_URL}"

echo "${output}"
