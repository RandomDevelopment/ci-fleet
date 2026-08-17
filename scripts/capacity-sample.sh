#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
environment=/etc/ci-fleet/ci-fleet.env
args=(capacity-sample)
if [[ ${CI_FLEET_TESTING:-0} == 1 && -n ${CI_FLEET_ROOT_PREFIX:-} ]]; then
  environment="$CI_FLEET_ROOT_PREFIX/etc/ci-fleet/ci-fleet.env"
  args+=(--root "$CI_FLEET_ROOT_PREFIX" --history "$CI_FLEET_ROOT_PREFIX/var/lib/ci-fleet/capacity/samples.jsonl")
fi
[[ -r $environment ]] || { printf 'CRITICAL capacity_configuration_missing\n' >&2; exit 2; }
set -a
# shellcheck disable=SC1090
. "$environment"
set +a
exec python3 "$script_dir/health.py" "${args[@]}"
