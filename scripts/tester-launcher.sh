#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

root_prefix=${CI_FLEET_ROOT_PREFIX:-}
[[ -z $root_prefix || ${CI_FLEET_TESTING:-0} == 1 ]] || { printf 'ERROR: CI_FLEET_ROOT_PREFIX is test-only\n' >&2; exit 1; }
root_path() { printf '%s%s' "$root_prefix" "$1"; }
lock_dir=$(root_path /run/lock/ci-fleet-tester)
lock_file=$lock_dir/runtime.lock
release_dir=$(root_path /opt/ci-fleet-tester/releases)
current_link=$(root_path /opt/ci-fleet-tester/current)
expected_uid=0
[[ ${CI_FLEET_TESTING:-0} != 1 ]] || expected_uid=$(id -u)
mkdir -m 0755 "$lock_dir" 2>/dev/null || true
[[ -d $lock_dir && ! -L $lock_dir && $(stat -c %u "$lock_dir") == "$expected_uid" && $(stat -c %a "$lock_dir") == 755 ]] || { printf 'ERROR: tester lock directory is unsafe\n' >&2; exit 1; }
if [[ ${CI_FLEET_TESTER_LOCK_FD:-} != 8 || ! -e /proc/$$/fd/8 || $(readlink -f /proc/$$/fd/8) != "$lock_file" ]]; then
  exec 8>"$lock_file"
  flock -x 8
fi
activation_pending=$(root_path /var/lib/ci-fleet-tester/activation-pending)
if [[ -e $activation_pending || -L $activation_pending ]]; then
  [[ -f $activation_pending && ! -L $activation_pending && $(stat -c '%u:%a' "$activation_pending") == "$expected_uid:600" ]] || { printf 'ERROR: tester activation journal is unsafe\n' >&2; exit 1; }
  activation_allowed=false
  if [[ $# == 1 && ( $1 == --check || $1 == --health ) && ${CI_FLEET_TESTER_ACTIVATION_FD:-} == 9 && -e /proc/$$/fd/9 && $(readlink -f /proc/$$/fd/9) == "$lock_dir/lifecycle.lock" ]] && flock -n 9; then activation_allowed=true; fi
  read -r _ _ _ phase installer_pid installer_start installer_boot extra <"$activation_pending"
  if [[ $activation_allowed == false && $phase == probes && $installer_pid =~ ^[1-9][0-9]*$ && -z $extra && ${INVOCATION_ID:-} =~ ^[a-f0-9]{32}$ ]]; then
    unit=
    case $1 in --health) unit=ci-fleet-tester-health.service ;; --cleanup) unit=ci-fleet-tester-cleanup.service ;; esac
    if [[ -n $unit && -e /proc/$installer_pid/fd/9 && $(readlink -f /proc/"$installer_pid"/fd/9) == "$lock_dir/lifecycle.lock" && $(awk '{print $22}' /proc/"$installer_pid"/stat 2>/dev/null) == "$installer_start" && $(</proc/sys/kernel/random/boot_id) == "$installer_boot" ]]; then
      properties=$(systemctl show "$unit" --property=MainPID --property=InvocationID --property=FragmentPath) || properties=
      if grep -Fxq "MainPID=$$" <<<"$properties" && grep -Fxq "InvocationID=$INVOCATION_ID" <<<"$properties" && grep -Fxq "FragmentPath=$(root_path /etc/systemd/system)/$unit" <<<"$properties"; then activation_allowed=true; fi
    fi
  fi
  [[ $activation_allowed == true ]] || { printf 'ERROR: interrupted tester activation requires installer recovery\n' >&2; exit 1; }
fi
target=$(readlink -f "$current_link")
revision=$(basename "$target")
[[ $target == "$release_dir/$revision" && $revision =~ ^[0-9a-f]{40}$ && -x $target/scripts/tester-runtime.sh ]] || { printf 'ERROR: active tester release is invalid\n' >&2; exit 1; }
export CI_FLEET_TESTER_LOCK_FD=8
exec "$target/scripts/tester-runtime.sh" "$@"
