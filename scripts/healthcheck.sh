#!/usr/bin/env bash
set -Eeuo pipefail
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
args=(local)
if [[ ${CI_FLEET_TESTING:-0} == 1 && -n ${CI_FLEET_ROOT_PREFIX:-} ]]; then
  args+=(--monitoring-config "$CI_FLEET_ROOT_PREFIX/etc/ci-fleet/monitoring.env" --output "$CI_FLEET_ROOT_PREFIX/var/lib/ci-fleet/health/latest.json")
fi
exec python3 "$repo_root/scripts/health.py" "${args[@]}" "$@"
