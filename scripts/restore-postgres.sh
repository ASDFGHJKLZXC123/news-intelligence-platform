#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/postgres-tools.sh
source "${script_dir}/lib/postgres-tools.sh"

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

if [[ "${RESTORE_TARGET_DISPOSABLE:-}" != "1" ]]; then
  echo "restore refused: set RESTORE_TARGET_DISPOSABLE=1 only for a disposable target" >&2
  exit 2
fi

if [[ -z "${RESTORE_EXPECTED_DATABASE:-}" ]]; then
  echo "restore refused: RESTORE_EXPECTED_DATABASE must name the disposable target exactly" >&2
  exit 2
fi

case "${RESTORE_EXPECTED_DATABASE}" in
  postgres | template0 | template1)
    echo "restore refused: protected PostgreSQL database name" >&2
    exit 2
    ;;
esac

if ! native_database_url="$(normalize_postgres_url_for_native_tools "${DATABASE_URL}")"; then
  echo "DATABASE_URL must use a PostgreSQL URL scheme" >&2
  exit 2
fi

checksum_file="${backup_file}.sha256"
if [[ ! -f "${checksum_file}" ]]; then
  echo "restore refused: backup checksum file not found: ${checksum_file}" >&2
  exit 2
fi
expected_checksum="$(tr -d '[:space:]' <"${checksum_file}")"
if [[ ! "${expected_checksum}" =~ ^[[:xdigit:]]{64}$ ]]; then
  echo "restore refused: backup checksum file is malformed" >&2
  exit 2
fi
expected_checksum="$(printf '%s' "${expected_checksum}" | tr '[:upper:]' '[:lower:]')"
actual_checksum="$(sha256_file "${backup_file}")"
if [[ "${actual_checksum}" != "${expected_checksum}" ]]; then
  echo "restore refused: backup checksum verification failed" >&2
  exit 1
fi

actual_database="$(
  postgres_tool psql \
    --no-psqlrc \
    --tuples-only \
    --no-align \
    --set=ON_ERROR_STOP=1 \
    --dbname "${native_database_url}" \
    --command 'SELECT current_database()'
)"
if [[ "${actual_database}" != "${RESTORE_EXPECTED_DATABASE}" ]]; then
  echo "restore refused: connected database does not match RESTORE_EXPECTED_DATABASE" >&2
  exit 2
fi

# Validate that the input is a readable archive before issuing any cleaning SQL.
postgres_tool pg_restore --list <"${backup_file}" >/dev/null

postgres_tool pg_restore \
  --clean \
  --if-exists \
  --exit-on-error \
  --single-transaction \
  --no-owner \
  --no-acl \
  --dbname "${native_database_url}" <"${backup_file}"

echo "restore completed after disposable-target and checksum verification"
