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
health_testing=${CI_FLEET_TESTING-}
health_root_prefix=${CI_FLEET_ROOT_PREFIX-}
health_docker_socket=${CI_FLEET_DOCKER_SOCKET-}
health_bootstrap=${CI_FLEET_HEALTH_BOOTSTRAP-}
health_suppress_delivery=${CI_FLEET_HEALTH_SUPPRESS_DELIVERY-}
while IFS= read -r variable; do unset "$variable"; done < <(compgen -A variable CI_FLEET_)
[[ -z $health_testing ]] || export CI_FLEET_TESTING=$health_testing
[[ -z $health_root_prefix ]] || export CI_FLEET_ROOT_PREFIX=$health_root_prefix
[[ -z $health_docker_socket ]] || export CI_FLEET_DOCKER_SOCKET=$health_docker_socket
[[ -z $health_bootstrap ]] || export CI_FLEET_HEALTH_BOOTSTRAP=$health_bootstrap
[[ -z $health_suppress_delivery ]] || export CI_FLEET_HEALTH_SUPPRESS_DELIVERY=$health_suppress_delivery
if [[ -r $selected_environment ]]; then
  set -a
  # shellcheck disable=SC1090
  . "$selected_environment"
  set +a
fi
use_local_docker
exec python3 "$repo_root/scripts/health.py" "${args[@]}" "$@"
