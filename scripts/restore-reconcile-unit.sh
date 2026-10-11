#!/usr/bin/env bash
set -Eeuo pipefail

die() { printf 'ERROR: deferred reconcile unit restoration failed: %s\n' "$*" >&2; exit 1; }
[[ $# == 3 ]] || die 'expected staged units, systemd directory, and installer lock'
staged_units=$1
systemd_dir=$2
lock_file=$3
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
rm -rf -- "$staged_units"
printf 'RECONCILE_UNIT_RESTORED\n'
