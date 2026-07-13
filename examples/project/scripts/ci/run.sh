#!/usr/bin/env bash
set -Eeuo pipefail

suite="${1:-}"
case "$suite" in
  fast|full) ;;
  *)
    echo "usage: $0 <fast|full>" >&2
    exit 64
    ;;
esac

repo_slug="${GITHUB_REPOSITORY#*/}"
raw_name="ci-${repo_slug}-${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-1}"
COMPOSE_PROJECT_NAME="$(printf '%s' "$raw_name" |
  tr '[:upper:]' '[:lower:]' |
  tr -cs 'a-z0-9_-' '-' |
  cut -c1-63)"
export COMPOSE_PROJECT_NAME

compose=(docker compose -f compose.ci.yaml)

cleanup() {
  local status=$?
  trap - EXIT INT TERM
  "${compose[@]}" down --remove-orphans --volumes || true
  exit "$status"
}
trap cleanup EXIT INT TERM

"${compose[@]}" build --pull test

case "$suite" in
  fast)
    "${compose[@]}" run --rm --no-deps test ./scripts/test-fast.sh
    ;;
  full)
    "${compose[@]}" up -d --wait
    "${compose[@]}" run --rm test ./scripts/test-full.sh
    ;;
esac
