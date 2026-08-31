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
health_testing_present=${CI_FLEET_TESTING+x}
health_testing=${CI_FLEET_TESTING-}
health_root_prefix_present=${CI_FLEET_ROOT_PREFIX+x}
health_root_prefix=${CI_FLEET_ROOT_PREFIX-}
health_docker_socket_present=${CI_FLEET_DOCKER_SOCKET+x}
health_docker_socket=${CI_FLEET_DOCKER_SOCKET-}
health_bootstrap_present=${CI_FLEET_HEALTH_BOOTSTRAP+x}
health_bootstrap=${CI_FLEET_HEALTH_BOOTSTRAP-}
health_suppress_delivery_present=${CI_FLEET_HEALTH_SUPPRESS_DELIVERY+x}
health_suppress_delivery=${CI_FLEET_HEALTH_SUPPRESS_DELIVERY-}
while IFS= read -r variable; do unset "$variable"; done < <(compgen -A variable CI_FLEET_)
if [[ -r $selected_environment ]]; then
  parsed_environment=$(mktemp)
  trap 'rm -f -- "$parsed_environment"' EXIT
  if ! /usr/bin/python3 - "$selected_environment" "$repo_root/scripts" >"$parsed_environment" <<'PY'
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[2])
from desired_state import parse_env

values = parse_env(Path(sys.argv[1]), allow_unknown=True)
invalid = next((name for name in values if not name.startswith("CI_FLEET_")), None)
if invalid is not None:
    raise SystemExit(f"ERROR: rendered environment variable must start with CI_FLEET_: {invalid}")
for name, value in values.items():
    if name.startswith("CI_FLEET_HEALTH_"):
        continue
    sys.stdout.buffer.write(name.encode() + b"\0" + value.encode() + b"\0")
PY
  then
    exit 2
  fi
  while IFS= read -r -d '' variable && IFS= read -r -d '' value; do
    export "$variable=$value"
  done <"$parsed_environment"
  rm -f -- "$parsed_environment"
  trap - EXIT
fi
if [[ $health_testing_present == x ]]; then export CI_FLEET_TESTING=$health_testing; else unset CI_FLEET_TESTING; fi
if [[ $health_root_prefix_present == x ]]; then export CI_FLEET_ROOT_PREFIX=$health_root_prefix; else unset CI_FLEET_ROOT_PREFIX; fi
if [[ $health_docker_socket_present == x ]]; then export CI_FLEET_DOCKER_SOCKET=$health_docker_socket; else unset CI_FLEET_DOCKER_SOCKET; fi
if [[ $health_bootstrap_present == x ]]; then export CI_FLEET_HEALTH_BOOTSTRAP=$health_bootstrap; else unset CI_FLEET_HEALTH_BOOTSTRAP; fi
if [[ $health_suppress_delivery_present == x ]]; then export CI_FLEET_HEALTH_SUPPRESS_DELIVERY=$health_suppress_delivery; else unset CI_FLEET_HEALTH_SUPPRESS_DELIVERY; fi
use_local_docker || exit 2
exec python3 "$repo_root/scripts/health.py" "${args[@]}" "$@"
