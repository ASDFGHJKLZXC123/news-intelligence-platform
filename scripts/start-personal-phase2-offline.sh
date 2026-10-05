#!/usr/bin/env bash
set -euo pipefail

# Apply test mode and blank every supported provider credential before any repository Python
# process starts, including manifest/database helpers and migrations that may import Settings.
export APP_ENV=test
export ANTHROPIC_API_KEY=
export OPENAI_API_KEY=
export GEMINI_API_KEY=
export DEEPSEEK_API_KEY=

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUN_ID="${PERSONAL_HARNESS_ID:-$("${PROJECT_ROOT}/.venv/bin/python" -c 'import uuid; print(uuid.uuid4().hex[:12])')}"
if [[ ! "${RUN_ID}" =~ ^[A-Za-z0-9_]{1,24}$ ]]; then
  echo "PERSONAL_HARNESS_ID must be 1-24 letters, numbers, or underscores." >&2
  exit 1
fi
RUN_SUFFIX="$(printf '%s' "${RUN_ID}" | tr '[:upper:]' '[:lower:]')"
STATE_DIR="${PERSONAL_HARNESS_STATE_DIR:-/tmp/nip-personal-phase2-offline-${RUN_SUFFIX}}"
MANIFEST="${STATE_DIR}/manifest.json"
DATABASE_NAME="nip_personal_phase2_offline_${RUN_SUFFIX}"
POSTGRES_CONTAINER="nip-personal-phase2-offline-pg-${RUN_SUFFIX}"
REDIS_CONTAINER="nip-personal-phase2-offline-redis-${RUN_SUFFIX}"
OWNER_TOKEN="$("${PROJECT_ROOT}/.venv/bin/python" -c 'import uuid; print(uuid.uuid4())')"
API_KEY_VALUE="${PERSONAL_HARNESS_API_KEY:-$("${PROJECT_ROOT}/.venv/bin/python" -c 'import secrets; print(secrets.token_urlsafe(24))')}"
FIXTURE_PATH="${PROJECT_ROOT}/personal-project-conversion/fixtures/phase-2-offline-workflow.json"
FRONTEND_ORIGIN_INPUT="${PERSONAL_HARNESS_FRONTEND_ORIGIN:-http://127.0.0.1:3000}"
EXTERNAL_POSTGRES_URL="${PERSONAL_HARNESS_POSTGRES_ADMIN_URL:-}"
POSTGRES_IMAGE="${PERSONAL_HARNESS_POSTGRES_IMAGE:-news-intelligence-platform-postgres:latest}"
REDIS_IMAGE="${PERSONAL_HARNESS_REDIS_IMAGE:-redis:7-alpine}"

if [[ ! "${API_KEY_VALUE}" =~ ^[A-Za-z0-9_-]{16,128}$ ]]; then
  echo "PERSONAL_HARNESS_API_KEY must be 16-128 URL-safe letters, numbers, underscores, or hyphens." >&2
  exit 1
fi

cd "${PROJECT_ROOT}"
export PYTHONPATH="${PROJECT_ROOT}"
FRONTEND_ORIGIN="$(.venv/bin/python scripts/personal_phase2_offline_db.py validate-frontend-origin --origin "${FRONTEND_ORIGIN_INPUT}")"
if [[ -n "${EXTERNAL_POSTGRES_URL}" ]]; then
  POSTGRES_MODE="external"
  PREPARE_POSTGRES_ARGS=()
else
  POSTGRES_MODE="owned"
  PREPARE_POSTGRES_ARGS=(--postgres-container "${POSTGRES_CONTAINER}")
fi
.venv/bin/python scripts/personal_phase2_offline_db.py prepare \
  --manifest "${MANIFEST}" \
  --run-id "${RUN_ID}" \
  --database-name "${DATABASE_NAME}" \
  --owner-token "${OWNER_TOKEN}" \
  --postgres-mode "${POSTGRES_MODE}" \
  "${PREPARE_POSTGRES_ARGS[@]}" \
  --redis-container "${REDIS_CONTAINER}" >/dev/null

cleanup_on_exit() {
  status=$?
  trap - EXIT INT TERM
  if [[ "${status}" -ne 0 ]]; then
    echo "Harness startup failed; cleaning only resources recorded in ${MANIFEST}." >&2
    "${PROJECT_ROOT}/scripts/stop-personal-phase2-offline.sh" "${STATE_DIR}" || true
  fi
  exit "${status}"
}
trap 'exit 130' INT
trap 'exit 143' TERM
trap cleanup_on_exit EXIT

record_container_or_remove() {
  kind="$1"
  name="$2"
  container_id="$3"
  host_port="$4"
  if .venv/bin/python scripts/personal_phase2_offline_db.py record-container \
    --manifest "${MANIFEST}" --kind "${kind}" --container-id "${container_id}" \
    --host-port "${host_port}"; then
    return 0
  fi
  actual_id="$(docker inspect --format '{{.Id}}' "${name}" 2>/dev/null || true)"
  if [[ -n "${actual_id}" && "${actual_id}" == "${container_id}" ]]; then
    docker rm -f "${name}" >/dev/null || true
  fi
  return 1
}

record_process_or_stop() {
  kind="$1"
  pid="$2"
  marker="$3"
  sleep 0.2
  started="$(ps -o lstart= -p "${pid}" | xargs)"
  if .venv/bin/python scripts/personal_phase2_offline_db.py record-process \
    --manifest "${MANIFEST}" --kind "${kind}" --pid "${pid}" \
    --started "${started}" "--command-marker=${marker}"; then
    return 0
  fi
  actual_started="$(ps -o lstart= -p "${pid}" 2>/dev/null | xargs || true)"
  actual_command="$(ps -o command= -p "${pid}" 2>/dev/null || true)"
  if [[ "${actual_started}" == "${started}" && "${actual_command}" == *"${marker}"* ]]; then
    kill "${pid}" >/dev/null 2>&1 || true
  fi
  return 1
}

if [[ "${POSTGRES_MODE}" == "owned" ]]; then
  docker image inspect "${POSTGRES_IMAGE}" >/dev/null
  postgres_id="$(docker run --rm -d \
    --name "${POSTGRES_CONTAINER}" \
    -e POSTGRES_USER=news \
    -e POSTGRES_PASSWORD=news \
    -e POSTGRES_DB=postgres \
    -p 127.0.0.1::5432 \
    "${POSTGRES_IMAGE}")"
  postgres_port="$(docker port "${POSTGRES_CONTAINER}" 5432/tcp)"
  postgres_port="${postgres_port##*:}"
  record_container_or_remove postgres "${POSTGRES_CONTAINER}" "${postgres_id}" "${postgres_port}"
  for _ in $(seq 1 30); do
    if docker exec "${POSTGRES_CONTAINER}" pg_isready -U news -d postgres >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done
  docker exec "${POSTGRES_CONTAINER}" pg_isready -U news -d postgres >/dev/null
  MAINTENANCE_URL="postgresql+psycopg2://news:news@127.0.0.1:${postgres_port}/postgres"
else
  MAINTENANCE_URL="${EXTERNAL_POSTGRES_URL}"
fi

.venv/bin/python scripts/personal_phase2_offline_db.py init \
  --manifest "${MANIFEST}" --maintenance-url "${MAINTENANCE_URL}"
DATABASE_URL_VALUE="$(.venv/bin/python scripts/personal_phase2_offline_db.py value --manifest "${MANIFEST}" --field database.url)"

docker image inspect "${REDIS_IMAGE}" >/dev/null
redis_id="$(docker run --rm -d --name "${REDIS_CONTAINER}" -p 127.0.0.1::6379 "${REDIS_IMAGE}")"
redis_port="$(docker port "${REDIS_CONTAINER}" 6379/tcp)"
redis_port="${redis_port##*:}"
record_container_or_remove redis "${REDIS_CONTAINER}" "${redis_id}" "${redis_port}"

API_PORT="${PERSONAL_HARNESS_API_PORT:-$(.venv/bin/python -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')}"
COMMON_ENV=(
  APP_ENV=test
  DATABASE_URL="${DATABASE_URL_VALUE}"
  REDIS_URL="redis://127.0.0.1:${redis_port}/0"
  CELERY_BROKER_URL="redis://127.0.0.1:${redis_port}/1"
  CELERY_RESULT_BACKEND="redis://127.0.0.1:${redis_port}/2"
  API_KEY="${API_KEY_VALUE}"
  PERSONAL_OFFLINE_FIXTURE_PATH="${FIXTURE_PATH}"
  CORS_ALLOW_ORIGINS="${FRONTEND_ORIGIN}"
  ANTHROPIC_API_KEY=
  OPENAI_API_KEY=
  GEMINI_API_KEY=
  DEEPSEEK_API_KEY=
)

api_pid="$(env "${COMMON_ENV[@]}" .venv/bin/python scripts/personal_phase2_offline_db.py \
  spawn-process --manifest "${MANIFEST}" --kind api --log "${STATE_DIR}/api.log" -- \
  .venv/bin/python -m uvicorn apps.api.main:app --host 127.0.0.1 --port "${API_PORT}")"
record_process_or_stop api "${api_pid}" "--port ${API_PORT}"

WORKER_HOSTNAME="personal-offline-${RUN_SUFFIX}@localhost"
worker_pid="$(env "${COMMON_ENV[@]}" .venv/bin/python scripts/personal_phase2_offline_db.py \
  spawn-process --manifest "${MANIFEST}" --kind worker --log "${STATE_DIR}/worker.log" -- \
  .venv/bin/celery -A workers.celery_app.celery_app worker --loglevel=INFO --pool=solo \
  --queues=pipeline --hostname="${WORKER_HOSTNAME}")"
record_process_or_stop worker "${worker_pid}" "--hostname=${WORKER_HOSTNAME}"

for _ in $(seq 1 30); do
  if curl --max-time 2 -fsS "http://127.0.0.1:${API_PORT}/api/v1/personal/workspace" \
    >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
curl --max-time 2 -fsS "http://127.0.0.1:${API_PORT}/api/v1/personal/workspace" >/dev/null

worker_ready=false
for _ in $(seq 1 30); do
  if ! kill -0 "${worker_pid}" >/dev/null 2>&1; then
    break
  fi
  if grep -Fq "workers.personal_tasks.run_personal_daily" "${STATE_DIR}/worker.log" \
    && grep -Fq "${WORKER_HOSTNAME} ready." "${STATE_DIR}/worker.log"; then
    worker_ready=true
    break
  fi
  sleep 1
done
if [[ "${worker_ready}" != "true" ]]; then
  echo "The isolated personal worker did not become ready." >&2
  exit 1
fi

trap - EXIT INT TERM
printf 'Synthetic harness ready; no network or paid provider route is configured.\n'
printf 'State and ownership manifest: %s\n' "${STATE_DIR}"
printf 'API: http://127.0.0.1:%s\n' "${API_PORT}"
printf 'Allowed frontend origin: %s\n' "${FRONTEND_ORIGIN}"
printf 'Frontend: %s/SIGNAL%%20-%%20Intelligence%%20Platform.html?api=http://127.0.0.1:%s\n' "${FRONTEND_ORIGIN%/}" "${API_PORT}"
printf 'Throwaway API key: %s\n' "${API_KEY_VALUE}"
printf 'Stop: "%s/scripts/stop-personal-phase2-offline.sh" "%s"\n' "${PROJECT_ROOT}" "${STATE_DIR}"
