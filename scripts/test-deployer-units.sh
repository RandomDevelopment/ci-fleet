#!/usr/bin/env bash
set -Eeuo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
command -v systemd-analyze >/dev/null || { printf 'systemd-analyze is required\n' >&2; exit 1; }
[[ -d /usr/lib/systemd/system ]] || { printf 'systemd unit library is missing\n' >&2; exit 1; }
root=$(mktemp -d)
trap 'rm -rf "$root"' EXIT
install -d -m 0755 "$root/etc/systemd/system" \
  "$root/opt/ci-fleet-deployer/current/scripts" "$root/usr/lib/systemd"
cp -a /usr/lib/systemd/system "$root/usr/lib/systemd/"
install -m 0755 "$repo_root/scripts/deployer-runtime.sh" \
  "$root/opt/ci-fleet-deployer/current/scripts/deployer-runtime.sh"
install -m 0644 "$repo_root"/deploy/deployer/* "$root/etc/systemd/system/"
systemd-analyze verify --root="$root" \
  ci-fleet-deployer.service \
  ci-fleet-deployer-health.service ci-fleet-deployer-health.timer \
  ci-fleet-deployer-cleanup.service ci-fleet-deployer-cleanup.timer \
  ci-fleet-deployer-drain.service
printf 'DEPLOYER_UNIT_TESTS_OK\n'
