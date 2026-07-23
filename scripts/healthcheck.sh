#!/usr/bin/env bash
set -Eeuo pipefail
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
environment=/etc/ci-fleet/ci-fleet.env
args=(local)
if [[ ${CI_FLEET_TESTING:-0} == 1 && -n ${CI_FLEET_ROOT_PREFIX:-} ]]; then
  environment="$CI_FLEET_ROOT_PREFIX/etc/ci-fleet/ci-fleet.env"
  args+=(--monitoring-config "$CI_FLEET_ROOT_PREFIX/etc/ci-fleet/monitoring.env" --output "$CI_FLEET_ROOT_PREFIX/var/lib/ci-fleet/health/latest.json")
fi
if [[ -r $environment ]]; then
  set -a
  # shellcheck disable=SC1090
  . "$environment"
  set +a
fi
exec python3 "$repo_root/scripts/health.py" "${args[@]}" "$@"
