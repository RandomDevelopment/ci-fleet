#!/usr/bin/env bash
set -Eeuo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
installer="$repo_root/scripts/install-status-receiver.sh"
unit="$repo_root/deploy/status-receiver/ci-fleet-status-receiver.service"
test -x "$installer"
test -f "$unit"

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
source_tree="$tmp/source"
root="$tmp/root"
mkdir -p "$source_tree/scripts" "$source_tree/deploy/status-receiver"
cp "$installer" "$source_tree/scripts/"
cp "$repo_root/scripts/status_receiver.py" "$repo_root/scripts/status_auth.py" "$source_tree/scripts/"
cp "$unit" "$source_tree/deploy/status-receiver/"
git -C "$source_tree" init -q
git -C "$source_tree" config user.name test
git -C "$source_tree" config user.email test@example.invalid
git -C "$source_tree" add .
git -C "$source_tree" commit -qm initial
first=$(git -C "$source_tree" rev-parse HEAD)

run() {
  CI_FLEET_STATUS_ROOT="$root" "$source_tree/scripts/install-status-receiver.sh" "$@"
}

test "$(run --install --ref "$first")" = INSTALLED
test "$(readlink "$root/opt/ci-fleet-status/current")" = "releases/$first"
test -f "$root/opt/ci-fleet-status/current/status_receiver.py"
test "$(stat -c %a "$root/var/lib/ci-fleet-status")" = 700
test "$(run --install --ref "$first")" = NO_CHANGE

git -C "$source_tree" commit --allow-empty -qm upgrade
second=$(git -C "$source_tree" rev-parse HEAD)
test "$(run --upgrade --ref "$second")" = UPGRADED
test "$(readlink "$root/opt/ci-fleet-status/current")" = "releases/$second"
test "$(cat "$root/var/lib/ci-fleet-status/previous-ref")" = "$first"
test "$(run --check)" = "CHECK_OK $second"
test "$(run --rollback)" = "ROLLED_BACK $first"
test "$(readlink "$root/opt/ci-fleet-status/current")" = "releases/$first"

grep -F -- '--bind 127.0.0.1' "$unit" >/dev/null
grep -F 'User=ci-fleet-status' "$unit" >/dev/null
grep -F 'NoNewPrivileges=yes' "$unit" >/dev/null
grep -F 'ProtectSystem=strict' "$unit" >/dev/null
grep -F 'ReadWritePaths=/var/lib/ci-fleet-status' "$unit" >/dev/null

echo STATUS_RECEIVER_INSTALL_TESTS_OK
