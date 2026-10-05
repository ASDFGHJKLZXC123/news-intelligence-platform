#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"
cd "${repo_root}"

# shellcheck source=lib/postgres-tools.sh
source "${script_dir}/lib/postgres-tools.sh"

project_base="${RESTORE_DRILL_PROJECT:-nip-restore-drill-$(date -u +%Y%m%d%H%M%S)-$$-${RANDOM}}"
requested_port="${RESTORE_DRILL_PORT:-0}"
ready_attempts="${RESTORE_DRILL_READY_ATTEMPTS:-60}"
postgres_image="${RESTORE_DRILL_POSTGRES_IMAGE:-}"
python_bin="${PYTHON:-}"
expected_alembic_head="0023_personal_processing_control"

readonly db_user="news"
readonly db_password="news"
readonly source_db="news"
readonly target_db="news_restore_drill"
readonly source_project="${project_base}-source"
readonly target_project="${project_base}-target"

if [[ ! "${project_base}" =~ ^[a-z0-9][a-z0-9_-]*$ ]]; then
  echo "RESTORE_DRILL_PROJECT must contain only lowercase letters, digits, dashes, or underscores" >&2
  exit 2
fi
if [[ ! "${requested_port}" =~ ^[0-9]+$ ]]; then
  echo "RESTORE_DRILL_PORT must be zero (automatic) or a positive integer" >&2
  exit 2
fi
if [[ ! "${ready_attempts}" =~ ^[0-9]+$ ]] || (( ready_attempts < 1 )); then
  echo "RESTORE_DRILL_READY_ATTEMPTS must be a positive integer" >&2
  exit 2
fi
if [[ -z "${python_bin}" ]]; then
  if [[ -x ".venv/bin/python" ]]; then
    python_bin=".venv/bin/python"
  else
    python_bin="python3"
  fi
fi

temporary_dir="$(mktemp -d "${TMPDIR:-/tmp}/nip-restore-drill.XXXXXX")"
backup_dir="${temporary_dir}/backups"
source_sentinel="${temporary_dir}/source-sentinel.txt"
target_sentinel="${temporary_dir}/target-sentinel.txt"
source_started=0
target_started=0

compose_for() {
  local project="$1"
  local database="$2"
  shift 2
  POSTGRES_HOST_PORT="${requested_port}" \
    POSTGRES_USER="${db_user}" \
    POSTGRES_PASSWORD="${db_password}" \
    POSTGRES_DB="${database}" \
    docker compose --project-name "${project}" "$@"
}

project_resources() {
  local project="$1"
  {
    docker ps --all --quiet --filter "label=com.docker.compose.project=${project}"
    docker volume ls --quiet --filter "label=com.docker.compose.project=${project}"
    docker network ls --quiet --filter "label=com.docker.compose.project=${project}"
    docker image ls --quiet --filter "label=com.docker.compose.project=${project}"
    if docker image inspect "${project}-postgres" >/dev/null 2>&1; then
      printf 'image-tag:%s-postgres\n' "${project}"
    fi
  } | sed '/^$/d'
}

destroy_project() {
  local project="$1"
  local database="$2"

  compose_for "${project}" "${database}" down --volumes --remove-orphans --rmi local
  if [[ -n "${postgres_image}" ]] && docker image inspect "${project}-postgres" >/dev/null 2>&1; then
    docker image rm "${project}-postgres" >/dev/null
  fi
  if [[ -n "$(project_resources "${project}")" ]]; then
    echo "disposable Compose resources remain for project ${project}" >&2
    return 1
  fi
  echo ">> cleanup verified for ${project}"
}

cleanup() {
  local status=$?
  trap - EXIT HUP INT TERM

  if (( target_started )); then
    if (( status != 0 )); then
      compose_for "${target_project}" "${target_db}" logs postgres || true
    fi
    destroy_project "${target_project}" "${target_db}" || status=1
  fi
  if (( source_started )); then
    if (( status != 0 )); then
      compose_for "${source_project}" "${source_db}" logs postgres || true
    fi
    destroy_project "${source_project}" "${source_db}" || status=1
  fi

  if [[ -d "${temporary_dir}" && "$(basename "${temporary_dir}")" == nip-restore-drill.* ]]; then
    rm -rf -- "${temporary_dir}"
  else
    echo "temporary restore-drill directory did not match the safe cleanup pattern" >&2
    status=1
  fi
  exit "${status}"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

assert_project_absent() {
  local project="$1"
  if [[ -n "$(project_resources "${project}")" ]]; then
    echo "Compose project ${project} already has resources; choose another RESTORE_DRILL_PROJECT" >&2
    exit 2
  fi
}

start_postgres() {
  local project="$1"
  local database="$2"
  local attempt=1

  if [[ -n "${postgres_image}" ]]; then
    if ! docker image inspect "${postgres_image}" >/dev/null 2>&1; then
      echo "RESTORE_DRILL_POSTGRES_IMAGE does not exist locally: ${postgres_image}" >&2
      return 2
    fi
    docker tag "${postgres_image}" "${project}-postgres"
    compose_for "${project}" "${database}" up --detach --no-build postgres
  else
    compose_for "${project}" "${database}" up --detach --build postgres
  fi
  until compose_for "${project}" "${database}" exec --no-TTY postgres \
    pg_isready --username "${db_user}" --dbname "${database}" >/dev/null 2>&1; do
    if (( attempt >= ready_attempts )); then
      echo "PostgreSQL did not become ready after ${ready_attempts} attempts" >&2
      return 1
    fi
    attempt=$((attempt + 1))
    sleep 2
  done
}

container_id_for() {
  local project="$1"
  local database="$2"
  compose_for "${project}" "${database}" ps --quiet postgres
}

host_port_for() {
  local project="$1"
  local database="$2"
  local endpoint=""
  local port=""

  endpoint="$(compose_for "${project}" "${database}" port postgres 5432)"
  port="${endpoint##*:}"
  if [[ ! "${port}" =~ ^[0-9]+$ ]] || (( port < 1 )); then
    echo "could not determine the disposable PostgreSQL host port" >&2
    return 1
  fi
  printf '%s\n' "${port}"
}

container_database_url() {
  local database="$1"
  printf 'postgresql+psycopg2://%s:%s@127.0.0.1:5432/%s\n' \
    "${db_user}" "${db_password}" "${database}"
}

host_database_url() {
  local database="$1"
  local port="$2"
  printf 'postgresql+psycopg2://%s:%s@127.0.0.1:%s/%s\n' \
    "${db_user}" "${db_password}" "${port}" "${database}"
}

query_container() {
  local container_id="$1"
  local database_url="$2"
  local sql="$3"
  POSTGRES_TOOL_CONTAINER="${container_id}" postgres_tool psql \
    --no-psqlrc \
    --tuples-only \
    --no-align \
    --set=ON_ERROR_STOP=1 \
    --dbname "$(normalize_postgres_url_for_native_tools "${database_url}")" \
    --command "${sql}"
}

tool_major() {
  local version_output="$1"
  if [[ "${version_output}" =~ ([0-9]+)(\.[0-9]+)? ]]; then
    printf '%s\n' "${BASH_REMATCH[1]}"
  else
    echo "could not parse PostgreSQL tool version" >&2
    return 1
  fi
}

assert_project_absent "${source_project}"
assert_project_absent "${target_project}"

echo ">> starting isolated source PostgreSQL"
source_started=1
start_postgres "${source_project}" "${source_db}"
source_container="$(container_id_for "${source_project}" "${source_db}")"
source_port="$(host_port_for "${source_project}" "${source_db}")"
source_container_url="$(container_database_url "${source_db}")"
source_host_url="$(host_database_url "${source_db}" "${source_port}")"

source_server_version="$(query_container "${source_container}" "${source_container_url}" 'SHOW server_version_num')"
source_server_major="$((source_server_version / 10000))"
source_dump_major="$(tool_major "$(docker exec "${source_container}" pg_dump --version)")"
if [[ "${source_dump_major}" != "${source_server_major}" ]]; then
  echo "source pg_dump major version does not match the source server" >&2
  exit 1
fi

echo ">> migrating source database to Alembic head ${expected_alembic_head}"
DATABASE_URL="${source_host_url}" "${python_bin}" -m alembic upgrade head
source_head="$(query_container "${source_container}" "${source_container_url}" 'SELECT version_num FROM alembic_version')"
if [[ "${source_head}" != "${expected_alembic_head}" ]]; then
  echo "source database is at Alembic ${source_head}, expected ${expected_alembic_head}" >&2
  exit 1
fi

POSTGRES_TOOL_CONTAINER="${source_container}" postgres_tool psql \
  --no-psqlrc \
  --set=ON_ERROR_STOP=1 \
  --dbname "$(normalize_postgres_url_for_native_tools "${source_container_url}")" <<'SQL'
CREATE TABLE public.recovery_drill_sentinel (
    id integer PRIMARY KEY,
    payload text NOT NULL,
    created_at timestamp with time zone NOT NULL
);
INSERT INTO public.recovery_drill_sentinel (id, payload, created_at)
VALUES (1, 'news-intelligence-restore-drill-v1', '2026-01-02T03:04:05.678901Z');
SQL

sentinel_query="SELECT id::text || '|' || payload || '|' || to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US') FROM public.recovery_drill_sentinel ORDER BY id"
query_container "${source_container}" "${source_container_url}" "${sentinel_query}" >"${source_sentinel}"
source_sentinel_checksum="$(sha256_file "${source_sentinel}")"

echo ">> creating checksummed backup with the server-matched pg_dump"
backup_file="$({
  DATABASE_URL="${source_container_url}" \
    BACKUP_DIR="${backup_dir}" \
    POSTGRES_TOOL_CONTAINER="${source_container}" \
    "${script_dir}/backup-postgres.sh"
})"
archive_checksum="$(sha256_file "${backup_file}")"
recorded_archive_checksum="$(tr -d '[:space:]' <"${backup_file}.sha256")"
if [[ "${archive_checksum}" != "${recorded_archive_checksum}" ]]; then
  echo "backup checksum sidecar does not match the archive" >&2
  exit 1
fi

echo ">> destroying the source server and its volume before restore"
destroy_project "${source_project}" "${source_db}"
source_started=0

echo ">> starting isolated restore target PostgreSQL"
target_started=1
start_postgres "${target_project}" "${target_db}"
target_container="$(container_id_for "${target_project}" "${target_db}")"
target_port="$(host_port_for "${target_project}" "${target_db}")"
target_container_url="$(container_database_url "${target_db}")"
target_host_url="$(host_database_url "${target_db}" "${target_port}")"

target_server_version="$(query_container "${target_container}" "${target_container_url}" 'SHOW server_version_num')"
target_server_major="$((target_server_version / 10000))"
target_restore_major="$(tool_major "$(docker exec "${target_container}" pg_restore --version)")"
if [[ "${target_server_major}" != "${source_server_major}" || "${target_restore_major}" != "${target_server_major}" ]]; then
  echo "restore server and pg_restore must match the source PostgreSQL major version" >&2
  exit 1
fi

if [[ "$(sha256_file "${backup_file}")" != "${archive_checksum}" ]]; then
  echo "backup archive changed between backup and restore" >&2
  exit 1
fi

echo ">> restoring only after explicit disposable-target verification"
DATABASE_URL="${target_container_url}" \
  POSTGRES_TOOL_CONTAINER="${target_container}" \
  RESTORE_TARGET_DISPOSABLE=1 \
  RESTORE_EXPECTED_DATABASE="${target_db}" \
  "${script_dir}/restore-postgres.sh" "${backup_file}"

repo_heads="$({ DATABASE_URL="${target_host_url}" "${python_bin}" -m alembic heads; } | sed -n 's/ .*//p')"
if [[ "${repo_heads}" != "${expected_alembic_head}" ]]; then
  echo "repository Alembic head is '${repo_heads}', expected exactly ${expected_alembic_head}" >&2
  exit 1
fi
restored_head="$(query_container "${target_container}" "${target_container_url}" 'SELECT version_num FROM alembic_version')"
if [[ "${restored_head}" != "${repo_heads}" ]]; then
  echo "restored Alembic revision does not match the repository head" >&2
  exit 1
fi

restored_extensions="$(query_container "${target_container}" "${target_container_url}" "SELECT string_agg(extname, ',' ORDER BY extname) FROM pg_extension WHERE extname IN ('postgis', 'vector')")"
if [[ "${restored_extensions}" != "postgis,vector" ]]; then
  echo "restored database is missing pgvector or PostGIS" >&2
  exit 1
fi

query_container "${target_container}" "${target_container_url}" "${sentinel_query}" >"${target_sentinel}"
target_sentinel_checksum="$(sha256_file "${target_sentinel}")"
if [[ "${target_sentinel_checksum}" != "${source_sentinel_checksum}" ]]; then
  echo "restored sentinel checksum does not match the source" >&2
  exit 1
fi

echo ">> destroying the restore target and its volume"
destroy_project "${target_project}" "${target_db}"
target_started=0

echo ">> restore drill passed: PostgreSQL ${source_server_major}, Alembic ${restored_head}, pgvector/PostGIS, archive and sentinel checksums, and cleanup verified"
