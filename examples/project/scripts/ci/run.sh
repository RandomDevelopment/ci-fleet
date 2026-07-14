#!/usr/bin/env bash
set -Eeuo pipefail

task="${1:-}"
shift || true

run_aggregate() {
  local spec aggregate_task shard
  for spec in "$@"; do
    aggregate_task=${spec%%:*}
    shard=${spec#*:}
    "$0" "$aggregate_task" --shard "$shard"
  done
}

case "$task" in
  fast)
    run_aggregate lint:1/1 syntax:1/1 unit:1/4 unit:2/4 unit:3/4 unit:4/4
    exit 0
    ;;
  full)
    run_aggregate \
      lint:1/1 syntax:1/1 \
      unit:1/4 unit:2/4 unit:3/4 unit:4/4 \
      integration:1/3 integration:2/3 integration:3/3 \
      database:1/2 database:2/2 browser:1/1 security:1/1
    exit 0
    ;;
  lint|syntax|unit|integration|database|browser|security) ;;
  *)
    echo "usage: $0 <fast|full|task> [--shard INDEX/TOTAL]" >&2
    exit 64
    ;;
esac

if [[ "${1:-}" != "--shard" || ! "${2:-}" =~ ^([1-9][0-9]*)/([1-9][0-9]*)$ || -n "${3:-}" ]]; then
  echo "usage: $0 ${task} --shard INDEX/TOTAL" >&2
  exit 64
fi
shard_index=${BASH_REMATCH[1]}
shard_total=${BASH_REMATCH[2]}
if (( shard_index > shard_total )); then
  echo "shard index cannot exceed shard total" >&2
  exit 64
fi

repository="${GITHUB_REPOSITORY:-local/project}"
repo_component="$(printf '%s' "${repository#*/}" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9_-' '-' | cut -c1-12)"
task_component="$(printf '%s' "$task" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9_-' '-' | cut -c1-12)"
raw_name="ci-${repo_component}-${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-1}-${task_component}-${shard_index}of${shard_total}"
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

case "$task" in
  integration|database|browser)
    "${compose[@]}" up -d --wait database
    ;;
esac

# Replace these sample scripts with project-owned task implementations. A
# sharding-aware test framework should receive both values deterministically.
"${compose[@]}" run --rm \
  -e "CI_FLEET_TASK=${task}" \
  -e "CI_FLEET_SHARD_INDEX=${shard_index}" \
  -e "CI_FLEET_SHARD_TOTAL=${shard_total}" \
  test "./scripts/test-${task}.sh" "$shard_index" "$shard_total"
