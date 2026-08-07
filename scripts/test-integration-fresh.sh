#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"
cd "${repo_root}"

project="${EPHEMERAL_PROJECT:-nip-ephemeral-$(date -u +%Y%m%d%H%M%S)-$$-${RANDOM}}"
requested_port="${EPHEMERAL_PORT:-0}"
ready_attempts="${EPHEMERAL_READY_ATTEMPTS:-60}"
postgres_image="${EPHEMERAL_POSTGRES_IMAGE:-}"
python_bin="${PYTHON:-}"

# The Stage 6/7 integration helpers intentionally use these fixed local-only
# credentials. Keeping the disposable service fixed prevents a caller's `.env`
# credentials from making only part of the integration suite fail.
readonly db_user="news"
readonly db_password="news"
readonly db_name="news"

if [[ -z "${python_bin}" ]]; then
  if [[ -x ".venv/bin/python" ]]; then
    python_bin=".venv/bin/python"
  else
    python_bin="python3"
  fi
fi

if [[ ! "${requested_port}" =~ ^[0-9]+$ ]]; then
  echo "EPHEMERAL_PORT must be zero (automatic) or a positive integer" >&2
  exit 2
fi
if [[ ! "${ready_attempts}" =~ ^[0-9]+$ ]] || (( ready_attempts < 1 )); then
  echo "EPHEMERAL_READY_ATTEMPTS must be a positive integer" >&2
  exit 2
fi

compose() {
  POSTGRES_HOST_PORT="${requested_port}" \
    POSTGRES_USER="${db_user}" \
    POSTGRES_PASSWORD="${db_password}" \
    POSTGRES_DB="${db_name}" \
    docker compose --project-name "${project}" "$@"
}

started=0
cleanup() {
  status=$?
  trap - EXIT HUP INT TERM
  if (( started )); then
    if (( status != 0 )); then
      echo ">> disposable Postgres logs after failure"
      compose logs postgres || true
    fi
    echo ">> tearing down disposable Postgres (including its volume)"
    if ! compose down --volumes --remove-orphans --rmi local; then
      echo "!! disposable Postgres cleanup failed" >&2
      if (( status == 0 )); then
        status=1
      fi
    fi
    if [[ -n "${postgres_image}" ]] && docker image inspect "${project}-postgres" >/dev/null 2>&1; then
      if ! docker image rm "${project}-postgres" >/dev/null; then
        echo "!! disposable Postgres image alias cleanup failed" >&2
        status=1
      fi
    fi

    remaining_containers="$(
      docker ps --all --quiet --filter "label=com.docker.compose.project=${project}" || true
    )"
    remaining_volumes="$(
      docker volume ls --quiet --filter "label=com.docker.compose.project=${project}" || true
    )"
    remaining_networks="$(
      docker network ls --quiet --filter "label=com.docker.compose.project=${project}" || true
    )"
    remaining_images="$(
      docker image ls --quiet --filter "label=com.docker.compose.project=${project}" || true
    )"
    remaining_image_alias=""
    if docker image inspect "${project}-postgres" >/dev/null 2>&1; then
      remaining_image_alias="${project}-postgres"
    fi
    if [[ -n "${remaining_containers}${remaining_volumes}${remaining_networks}${remaining_images}${remaining_image_alias}" ]]; then
      echo "!! disposable Compose resources remain after cleanup" >&2
      if (( status == 0 )); then
        status=1
      fi
    else
      echo ">> cleanup verified: no project containers, volumes, networks, or images remain"
    fi
  fi
  exit "${status}"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

existing_containers="$(
  docker ps --all --quiet --filter "label=com.docker.compose.project=${project}"
)"
existing_volumes="$(
  docker volume ls --quiet --filter "label=com.docker.compose.project=${project}"
)"
existing_networks="$(
  docker network ls --quiet --filter "label=com.docker.compose.project=${project}"
)"
if [[ -n "${existing_containers}${existing_volumes}${existing_networks}" ]]; then
  echo "Compose project '${project}' already has resources; choose another EPHEMERAL_PROJECT or remove the stale project." >&2
  exit 2
fi

if [[ "${requested_port}" == "0" ]]; then
  port_description="automatic host port"
else
  port_description="host port ${requested_port}"
fi

echo ">> starting disposable Postgres (project=${project}, ${port_description})"
started=1
if [[ -n "${postgres_image}" ]]; then
  if ! docker image inspect "${postgres_image}" >/dev/null 2>&1; then
    echo "EPHEMERAL_POSTGRES_IMAGE does not exist locally: ${postgres_image}" >&2
    exit 2
  fi
  docker tag "${postgres_image}" "${project}-postgres"
  compose up --detach --no-build postgres
else
  compose up --detach --build postgres
fi

published_endpoint="$(compose port postgres 5432)"
port="${published_endpoint##*:}"
if [[ ! "${port}" =~ ^[0-9]+$ ]] || (( port < 1 )); then
  echo "!! could not determine the disposable Postgres host port: ${published_endpoint}" >&2
  exit 1
fi
# Compose publishes this service on IPv4 loopback. Use the matching literal so libpq never
# resolves localhost to ::1 and reaches an unrelated IPv6 listener during a long test run.
database_url="postgresql+psycopg2://${db_user}:${db_password}@127.0.0.1:${port}/${db_name}"

echo ">> waiting for readiness"
attempt=1
until compose exec --no-TTY postgres pg_isready --username "${db_user}" --dbname "${db_name}" >/dev/null 2>&1; do
  if (( attempt >= ready_attempts )); then
    echo "!! Postgres did not become ready after ${ready_attempts} attempts" >&2
    compose logs postgres
    exit 1
  fi
  attempt=$((attempt + 1))
  sleep 2
done

echo ">> disposable Postgres is ready on host port ${port}"
postgres_container="$(compose ps --quiet postgres)"
if [[ -z "${postgres_container}" ]]; then
  echo "!! could not determine the disposable Postgres container" >&2
  exit 1
fi
echo ">> migrating blank database to head"
DATABASE_URL="${database_url}" "${python_bin}" -m alembic upgrade head

echo ">> running integration suite"
DATABASE_URL="${database_url}" \
  POSTGRES_HOST_PORT="${port}" \
  POSTGRES_USER="${db_user}" \
  POSTGRES_PASSWORD="${db_password}" \
  POSTGRES_DB="${db_name}" \
  REQUIRE_POSTGRES=1 \
  REQUIRE_DATABASE_ROLE_HARDENING=1 \
  POSTGRES_TOOL_CONTAINER="${postgres_container}" \
  POSTGRES_TOOL_CONTAINER_ADMIN_ROLE="${db_user}" \
  "${python_bin}" -m pytest -m integration
