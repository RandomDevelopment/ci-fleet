#!/usr/bin/env bash
set -Eeuo pipefail
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck disable=SC1091
source "$repo_root/scripts/docker-local-env.sh"
environment=/etc/ci-fleet/ci-fleet.env
args=(local)
if [[ ${CI_FLEET_TESTING:-0} == 1 && -n ${CI_FLEET_ROOT_PREFIX:-} ]]; then
  environment="$CI_FLEET_ROOT_PREFIX/etc/ci-fleet/ci-fleet.env"
  args+=(--monitoring-config "$CI_FLEET_ROOT_PREFIX/etc/ci-fleet/monitoring.env" --output "$CI_FLEET_ROOT_PREFIX/var/lib/ci-fleet/health/latest.json")
fi
if [[ ${1:-} == --env ]]; then
  [[ $# -ge 2 && -r $2 ]] || { printf 'ERROR: --env requires a readable file\n' >&2; exit 2; }
  environment=$2
  shift 2
fi
selected_environment=$environment
health_suppress_delivery=${CI_FLEET_HEALTH_SUPPRESS_DELIVERY-}
while IFS= read -r variable; do unset "$variable"; done < <(compgen -A variable CI_FLEET_)
if [[ -r $selected_environment ]]; then
  set -a
  # shellcheck disable=SC1090
  . "$selected_environment"
  set +a
fi
[[ -z $health_suppress_delivery ]] || export CI_FLEET_HEALTH_SUPPRESS_DELIVERY=$health_suppress_delivery
use_local_docker
exec python3 "$repo_root/scripts/health.py" "${args[@]}" "$@"
