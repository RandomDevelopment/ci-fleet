#!/usr/bin/env bash
set -Eeuo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
command -v systemd-analyze >/dev/null || { printf 'systemd-analyze is required\n' >&2; exit 1; }
[[ -d /usr/lib/systemd/system ]] || { printf 'systemd unit library is missing\n' >&2; exit 1; }
root=$(mktemp -d)
trap 'rm -rf "$root"' EXIT
install -d -m 0755 "$root/etc/systemd/system" \
  "$root/opt/ci-fleet-deployer/current/scripts" "$root/usr/lib/systemd" "$root/usr/bin"
install -m 0755 /usr/bin/mkdir "$root/usr/bin/mkdir"
cp -a /usr/lib/systemd/system "$root/usr/lib/systemd/"
install -m 0755 "$repo_root/scripts/deployer-runtime.sh" \
  "$root/opt/ci-fleet-deployer/current/scripts/deployer-runtime.sh"
install -m 0644 "$repo_root"/deploy/deployer/* "$root/etc/systemd/system/"
systemd-analyze verify --root="$root" \
  ci-fleet-deployer.service \
  ci-fleet-deployer-health.service ci-fleet-deployer-health.timer \
  ci-fleet-deployer-cleanup.service ci-fleet-deployer-cleanup.timer \
  ci-fleet-deployer-drain.service
for unit in ci-fleet-deployer.service ci-fleet-deployer-health.service ci-fleet-deployer-cleanup.service; do
  grep -Fxq 'CapabilityBoundingSet=~CAP_SYS_ADMIN CAP_SYS_PTRACE' "$root/etc/systemd/system/$unit" || { printf 'unsafe adapter capabilities in %s\n' "$unit" >&2; exit 1; }
done

# Model /var/lock -> /run/lock after the volatile lock directory disappears.
install -d -m 0755 "$root/var" "$root/run/lock"
ln -s ../run/lock "$root/var/lock"
for unit in "$root/etc/systemd/system/"*.service; do
  prestart=$(sed -n 's/^ExecStartPre=//p' "$unit")
  [[ $prestart == '+/usr/bin/mkdir -p -m 0700 /var/lock/ci-fleet-deployer' ]] || { printf 'missing lock preparation in %s\n' "$unit" >&2; exit 1; }
  read -r -a command_line <<<"${prestart#+}"
  command_line[-1]=$root${command_line[-1]}
  "${command_line[@]}"
  [[ $(stat -c '%u:%a' "$root/run/lock/ci-fleet-deployer") == "$(id -u):700" ]] || exit 1
  exec 9<"$root/run/lock/ci-fleet-deployer"
  flock -n 9
  "${command_line[@]}"
  if (exec 8<"$root/var/lock/ci-fleet-deployer"; flock -n 8); then
    printf 'lock preparation replaced a held operation lock\n' >&2; exit 1
  fi
  exec 9<&-
  chmod 0755 "$root/run/lock/ci-fleet-deployer"
  "${command_line[@]}"
  [[ $(stat -c %a "$root/run/lock/ci-fleet-deployer") == 755 ]] || { printf 'lock preparation changed an existing unsafe directory\n' >&2; exit 1; }
  rm -rf "$root/run/lock/ci-fleet-deployer"
done
printf 'DEPLOYER_UNIT_TESTS_OK\n'
