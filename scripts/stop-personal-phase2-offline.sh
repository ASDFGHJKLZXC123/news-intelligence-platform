#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STATE_DIR="${1:-}"
if [[ -z "${STATE_DIR}" ]]; then
  echo "usage: $0 /tmp/nip-personal-phase2-offline-<id>" >&2
  exit 2
fi
STATE_DIR="$(cd "${STATE_DIR}" && pwd -P)"
MANIFEST="${STATE_DIR}/manifest.json"
if [[ ! -f "${MANIFEST}" ]]; then
  echo "Refusing cleanup: no harness ownership manifest at ${MANIFEST}." >&2
  exit 1
fi
cd "${PROJECT_ROOT}"
export PYTHONPATH="${PROJECT_ROOT}"

value() {
  .venv/bin/python scripts/personal_phase2_offline_db.py value \
    --manifest "${MANIFEST}" --field "$1"
}

failure=0
stop_process() {
  kind="$1"
  pid="$(value "processes.${kind}.pid")"
  [[ -z "${pid}" ]] && return 0
  expected_started="$(value "processes.${kind}.started")"
  marker="$(value "processes.${kind}.command_marker")"
  if ! kill -0 "${pid}" >/dev/null 2>&1; then
    return 0
  fi
  actual_started="$(ps -o lstart= -p "${pid}" | xargs)"
  actual_command="$(ps -o command= -p "${pid}")"
  if [[ "${actual_started}" != "${expected_started}" || "${actual_command}" != *"${marker}"* ]]; then
    echo "Refusing to stop PID ${pid}: ${kind} process identity no longer matches manifest." >&2
    failure=1
    return 0
  fi
  kill "${pid}"
  for _ in $(seq 1 20); do
    kill -0 "${pid}" >/dev/null 2>&1 || return 0
    sleep 0.25
  done
  actual_started="$(ps -o lstart= -p "${pid}" | xargs)"
  actual_command="$(ps -o command= -p "${pid}")"
  if [[ "${actual_started}" == "${expected_started}" && "${actual_command}" == *"${marker}"* ]]; then
    kill -KILL "${pid}"
  else
    echo "Refusing force-stop: ${kind} process identity changed after TERM." >&2
    failure=1
  fi
}

remove_container() {
  kind="$1"
  owned="$(value "${kind}.owned")"
  [[ "${owned}" != "true" ]] && return 0
  name="$(value "${kind}.container_name")"
  expected_id="$(value "${kind}.container_id")"
  actual_id="$(docker inspect --format '{{.Id}}' "${name}" 2>/dev/null || true)"
  [[ -z "${actual_id}" ]] && return 0
  if [[ "${actual_id}" != "${expected_id}" ]]; then
    echo "Refusing to remove ${kind}: container identity no longer matches manifest." >&2
    failure=1
    return 0
  fi
  docker rm -f "${name}" >/dev/null
}

stop_process worker
stop_process api
if [[ "${failure}" -eq 0 ]]; then
  if ! .venv/bin/python scripts/personal_phase2_offline_db.py cleanup-db --manifest "${MANIFEST}"; then
    echo "Database cleanup failed; preserving manifest and containers for safe recovery." >&2
    failure=1
  fi
fi
if [[ "${failure}" -eq 0 ]]; then
  remove_container redis
  remove_container postgres
fi
if [[ "${failure}" -ne 0 ]]; then
  exit 1
fi
printf 'Synthetic Phase 2 harness resources removed. Logs and manifest remain at %s.\n' "${STATE_DIR}"
