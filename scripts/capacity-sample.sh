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
# Admission measurements also run while idle. Keep the existing capacity history
# independent: a failed/terminated disk probe must not suppress its collection.
disk_args=()
if [[ ${CI_FLEET_TESTING:-0} == 1 && -n ${CI_FLEET_ROOT_PREFIX:-} ]]; then
  disk_args+=(--output "$CI_FLEET_ROOT_PREFIX/var/lib/ci-fleet/disk-admission")
fi
disk_result=0
history_result=0
timeout --kill-after=1s 15s python3 "$script_dir/disk_admission_sample.py" "${disk_args[@]}" || disk_result=$?
python3 "$script_dir/health.py" "${args[@]}" || history_result=$?
((history_result == 0)) || exit "$history_result"
exit "$disk_result"
