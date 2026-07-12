#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "DATABASE_URL is required" >&2
  exit 2
fi

if [[ $# -ne 1 ]]; then
  echo "usage: $0 <backup.dump>" >&2
  exit 2
fi

backup_file="$1"
if [[ ! -f "${backup_file}" ]]; then
  echo "backup file not found: ${backup_file}" >&2
  exit 2
fi

pg_restore \
  --clean \
  --if-exists \
  --no-owner \
  --no-acl \
  --dbname "${DATABASE_URL}" \
  "${backup_file}"
