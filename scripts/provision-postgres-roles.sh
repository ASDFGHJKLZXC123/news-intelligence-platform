#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/postgres-tools.sh
source "${script_dir}/lib/postgres-tools.sh"

required_variables=(
  DATABASE_ADMIN_URL
  APP_DATABASE_NAME
  MIGRATION_DATABASE_ROLE
  MIGRATION_DATABASE_PASSWORD
  RUNTIME_DATABASE_ROLE
  RUNTIME_DATABASE_PASSWORD
)
for variable_name in "${required_variables[@]}"; do
  if [[ -z "${!variable_name:-}" ]]; then
    echo "${variable_name} is required" >&2
    exit 2
  fi
done

app_schema="${APP_DATABASE_SCHEMA:-public}"
template="${script_dir}/../infra/sql/provision-app-roles.psql"

validate_identifier() {
  local variable_name="$1"
  local value="$2"

  if [[ ! "${value}" =~ ^[a-zA-Z_][a-zA-Z0-9_]*$ ]] || (( ${#value} > 63 )); then
    echo "${variable_name} must be a simple PostgreSQL identifier of at most 63 characters" >&2
    exit 2
  fi
}

validate_password() {
  local variable_name="$1"
  local value="$2"

  if (( ${#value} < 16 )); then
    echo "${variable_name} must contain at least 16 characters" >&2
    exit 2
  fi
}

validate_identifier APP_DATABASE_NAME "${APP_DATABASE_NAME}"
validate_identifier APP_DATABASE_SCHEMA "${app_schema}"
validate_identifier MIGRATION_DATABASE_ROLE "${MIGRATION_DATABASE_ROLE}"
validate_identifier RUNTIME_DATABASE_ROLE "${RUNTIME_DATABASE_ROLE}"
validate_password MIGRATION_DATABASE_PASSWORD "${MIGRATION_DATABASE_PASSWORD}"
validate_password RUNTIME_DATABASE_PASSWORD "${RUNTIME_DATABASE_PASSWORD}"

if [[ "${MIGRATION_DATABASE_ROLE}" == "${RUNTIME_DATABASE_ROLE}" ]]; then
  echo "migration and runtime database roles must be distinct" >&2
  exit 2
fi
if [[ "${MIGRATION_DATABASE_PASSWORD}" == "${RUNTIME_DATABASE_PASSWORD}" ]]; then
  echo "migration and runtime database passwords must be distinct" >&2
  exit 2
fi
for role_name in "${MIGRATION_DATABASE_ROLE}" "${RUNTIME_DATABASE_ROLE}"; do
  normalized_role_name="$(printf '%s' "${role_name}" | tr '[:upper:]' '[:lower:]')"
  if [[ "${normalized_role_name}" == pg_* ]]; then
    echo "managed role names beginning with pg_ are reserved" >&2
    exit 2
  fi
  case "${normalized_role_name}" in
    public | current_role | current_user | session_user | postgres | root | admin | administrator | rdsadmin | rds_admin | azure_pg_admin | cloudsqladmin | cloudsqlsuperuser | alloydbadmin | supabase_admin | neon_superuser)
      echo "managed role uses a reserved or administrative role name" >&2
      exit 2
      ;;
  esac
done

if ! native_admin_url="$(normalize_postgres_url_for_native_tools "${DATABASE_ADMIN_URL}")"; then
  echo "DATABASE_ADMIN_URL must use a PostgreSQL URL scheme" >&2
  exit 2
fi
if [[ ! -r "${template}" ]]; then
  echo "role provisioning template is not readable" >&2
  exit 2
fi

export NIP_APP_DATABASE="${APP_DATABASE_NAME}"
export NIP_APP_SCHEMA="${app_schema}"
export NIP_MIGRATION_ROLE="${MIGRATION_DATABASE_ROLE}"
export NIP_MIGRATION_PASSWORD="${MIGRATION_DATABASE_PASSWORD}"
export NIP_RUNTIME_ROLE="${RUNTIME_DATABASE_ROLE}"
export NIP_RUNTIME_PASSWORD="${RUNTIME_DATABASE_PASSWORD}"

psql_arguments=(
  --no-psqlrc
  --quiet
  --set=ON_ERROR_STOP=1
)

if [[ -n "${POSTGRES_TOOL_CONTAINER:-}" ]]; then
  if [[ ! "${POSTGRES_TOOL_CONTAINER}" =~ ^[a-zA-Z0-9_.-]+$ ]]; then
    echo "POSTGRES_TOOL_CONTAINER contains unsupported characters" >&2
    exit 2
  fi
  if [[ -z "${POSTGRES_TOOL_CONTAINER_ADMIN_ROLE:-}" ]]; then
    echo "POSTGRES_TOOL_CONTAINER_ADMIN_ROLE is required with POSTGRES_TOOL_CONTAINER" >&2
    exit 2
  fi
  validate_identifier POSTGRES_TOOL_CONTAINER_ADMIN_ROLE "${POSTGRES_TOOL_CONTAINER_ADMIN_ROLE}"
  command docker exec --interactive \
    --env NIP_APP_DATABASE \
    --env NIP_APP_SCHEMA \
    --env NIP_MIGRATION_ROLE \
    --env NIP_MIGRATION_PASSWORD \
    --env NIP_RUNTIME_ROLE \
    --env NIP_RUNTIME_PASSWORD \
    "${POSTGRES_TOOL_CONTAINER}" \
    psql \
    --username "${POSTGRES_TOOL_CONTAINER_ADMIN_ROLE}" \
    --dbname "${APP_DATABASE_NAME}" \
    "${psql_arguments[@]}" <"${template}"
else
  # libpq accepts a full connection string through PGDATABASE. Keeping it in
  # the environment avoids exposing an admin password in process arguments.
  PGDATABASE="${native_admin_url}" command psql "${psql_arguments[@]}" <"${template}"
fi

echo "database migration/runtime roles provisioned"
