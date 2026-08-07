#!/usr/bin/env bash

# Shared helpers for PostgreSQL operations scripts. This file is sourced by the
# backup, restore, and recovery-drill entry points; it is not an entry point itself.

normalize_postgres_url_for_native_tools() {
  local configured_url="${1:-}"
  local scheme=""

  case "${configured_url}" in
    postgresql://* | postgres://*)
      printf '%s\n' "${configured_url}"
      ;;
    postgresql+*://*)
      scheme="${configured_url%%://*}"
      if [[ "${scheme}" == "postgresql+" ]]; then
        return 1
      fi
      printf 'postgresql://%s\n' "${configured_url#*://}"
      ;;
    *)
      return 1
      ;;
  esac
}

postgres_tool() {
  local tool="$1"
  shift

  if [[ -n "${POSTGRES_TOOL_CONTAINER:-}" ]]; then
    if [[ ! "${POSTGRES_TOOL_CONTAINER}" =~ ^[a-zA-Z0-9_.-]+$ ]]; then
      echo "POSTGRES_TOOL_CONTAINER contains unsupported characters" >&2
      return 2
    fi
    command docker exec --interactive "${POSTGRES_TOOL_CONTAINER}" "${tool}" "$@"
  else
    command "${tool}" "$@"
  fi
}

sha256_file() {
  local path="$1"

  if command -v sha256sum >/dev/null 2>&1; then
    command sha256sum -- "${path}" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    command shasum -a 256 -- "${path}" | awk '{print $1}'
  else
    echo "a SHA-256 utility (sha256sum or shasum) is required" >&2
    return 2
  fi
}
