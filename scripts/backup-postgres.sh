#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/postgres-tools.sh
source "${script_dir}/lib/postgres-tools.sh"

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "DATABASE_URL is required" >&2
  exit 2
fi

if ! native_database_url="$(normalize_postgres_url_for_native_tools "${DATABASE_URL}")"; then
  echo "DATABASE_URL must use a PostgreSQL URL scheme" >&2
  exit 2
fi

backup_dir="${BACKUP_DIR:-backups/postgres}"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "${backup_dir}"

output="${backup_dir}/news-intelligence-${timestamp}.dump"
checksum_output="${output}.sha256"
if [[ -e "${output}" || -e "${checksum_output}" ]]; then
  echo "backup output already exists; wait one second or choose another BACKUP_DIR" >&2
  exit 1
fi

temporary_output="$(mktemp "${output}.tmp.XXXXXX")"
temporary_checksum="$(mktemp "${checksum_output}.tmp.XXXXXX")"
cleanup_temporary_files() {
  rm -f -- "${temporary_output}" "${temporary_checksum}"
}
trap cleanup_temporary_files EXIT HUP INT TERM

postgres_tool pg_dump \
  --format=custom \
  --no-owner \
  --no-acl \
  "${native_database_url}" >"${temporary_output}"

if [[ ! -s "${temporary_output}" ]]; then
  echo "pg_dump produced an empty backup" >&2
  exit 1
fi

checksum="$(sha256_file "${temporary_output}")"
printf '%s\n' "${checksum}" >"${temporary_checksum}"
chmod 600 "${temporary_output}" "${temporary_checksum}"
mv "${temporary_output}" "${output}"
mv "${temporary_checksum}" "${checksum_output}"

echo "${output}"
