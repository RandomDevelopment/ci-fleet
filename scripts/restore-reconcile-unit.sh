#!/usr/bin/env bash
set -Eeuo pipefail

die() { printf 'ERROR: deferred reconcile unit restoration failed: %s\n' "$*" >&2; exit 1; }
[[ $# == 5 ]] || die 'expected staged units, systemd directory, installed state, manager pointer, and installer lock'
staged_units=$1
systemd_dir=$2
state_file=$3
manager_current=$4
lock_file=$5
unit=ci-fleet-reconcile.service
[[ "$staged_units" == "$systemd_dir"/.ci-fleet-restore.* && -d "$staged_units" && ! -L "$staged_units" \
  && $(stat -c %u "$staged_units") == "$(id -u)" && $(stat -c %a "$staged_units") == 700 ]] || die 'untrusted staged directory'
[[ -f "$staged_units/current.service" && ! -L "$staged_units/current.service" ]] || die 'missing preserved unit'
exec 9>"$lock_file"
while true; do
  state=$(systemctl show --property=ActiveState --value "$unit") || die 'cannot determine service state'
  case "$state" in
    inactive|failed) break ;;
    active|activating|reloading|deactivating) sleep 1 ;;
    *) die 'unknown service state' ;;
  esac
done
flock -w 300 9 || die 'installer lock is busy'
pending=$systemd_dir/.ci-fleet-reconcile-unit-restore
if [[ $(readlink "$pending") != "$staged_units" ]]; then
  rm -rf -- "$staged_units"
  printf 'RECONCILE_UNIT_RESTORE_SUPERSEDED\n'
  exit 0
fi
state_matches=false manager_matches=false
if [[ -f "$staged_units/install-state.json" ]]; then
  if cmp -s "$staged_units/install-state.json" "$state_file"; then state_matches=true; fi
elif [[ ! -e "$state_file" && ! -L "$state_file" ]]; then state_matches=true; fi
if [[ -f "$staged_units/manager-current" ]]; then
  if [[ -L "$manager_current" && $(readlink "$manager_current") == $(<"$staged_units/manager-current") ]]; then manager_matches=true; fi
elif [[ ! -e "$manager_current" && ! -L "$manager_current" ]]; then manager_matches=true; fi
if [[ "$state_matches" != true || "$manager_matches" != true ]]; then
  rm -f -- "$pending"
  rm -rf -- "$staged_units"
  printf 'RECONCILE_UNIT_RESTORE_SUPERSEDED\n'
  exit 0
fi
state=$(systemctl show --property=ActiveState --value "$unit") || die 'cannot determine service state'
[[ "$state" == inactive || "$state" == failed ]] || die 'reconciliation is still executing'
cmp -s "$staged_units/current.service" "$systemd_dir/$unit" || die 'a later installer changed the unit'
if [[ -f "$staged_units/$unit" && ! -L "$staged_units/$unit" ]]; then
  install -m 0644 "$staged_units/$unit" "$staged_units/replacement.service"
  mv -Tf "$staged_units/replacement.service" "$systemd_dir/$unit"
else
  [[ ! -e "$staged_units/$unit" && ! -L "$staged_units/$unit" ]] || die 'invalid checkpointed unit'
  rm -f "$systemd_dir/$unit"
fi
systemctl daemon-reload
rm -f -- "$pending"
rm -rf -- "$staged_units"
printf 'RECONCILE_UNIT_RESTORED\n'
