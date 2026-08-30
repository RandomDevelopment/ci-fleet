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
mkdir -p "$root/run/lock"
chmod 1777 "$root/run/lock"
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
  CI_FLEET_STATUS_TEST_MODE=1 CI_FLEET_STATUS_TEST_EXPECTED_OWNER="$(id -u)" CI_FLEET_STATUS_ROOT="$root" \
    "$source_tree/scripts/install-status-receiver.sh" "$@"
}

assert_systemd_mode() { test "$(stat -c %a "$root/etc/systemd/system")" = 750; }

if CI_FLEET_STATUS_ROOT="$root" "$source_tree/scripts/install-status-receiver.sh" --check >/dev/null 2>&1; then
  echo "alternate root accepted without explicit test mode" >&2
  exit 1
fi
mkdir "$tmp/attacker-lock-target"
chmod 0777 "$tmp/attacker-lock-target"
ln -s "$tmp/attacker-lock-target" "$root/run/lock/ci-fleet-status"
if run --install --ref "$first" >/dev/null 2>&1; then
  echo "symlinked installer lock directory was accepted" >&2
  exit 1
fi
test "$(stat -c %a "$tmp/attacker-lock-target")" = 777
rm "$root/run/lock/ci-fleet-status"
mkdir -p "$root/opt/ci-fleet-status" "$tmp/untrusted-releases"
chmod 0755 "$root/opt/ci-fleet-status" "$tmp/untrusted-releases"
ln -s "$tmp/untrusted-releases" "$root/opt/ci-fleet-status/releases"
if run --install --ref "$first" >/dev/null 2>&1; then
  echo "symlinked release root was accepted" >&2
  exit 1
fi
rm -rf "$root/opt/ci-fleet-status"
mkdir -p "$root/etc/systemd/system"
chmod 0750 "$root/etc/systemd/system"
printf 'deployer\n' >"$root/etc/systemd/system/ci-fleet-deployer.service"
if run --install --ref "$first" >/dev/null 2>&1; then
  echo "deployer role was accepted by the status receiver installer" >&2
  exit 1
fi
rm "$root/etc/systemd/system/ci-fleet-deployer.service"
role_lock="$root/run/ci-fleet-role-admission.lock"
exec 8>"$role_lock"
flock -n 8
if run --install --ref "$first" >/dev/null 2>&1; then
  echo "status receiver installation ignored the shared role lock" >&2
  exit 1
fi
flock -u 8; exec 8>&-
export CI_FLEET_STATUS_TEST_FAIL_RELOAD_ONCE=$tmp/fail-reload-once
: >"$CI_FLEET_STATUS_TEST_FAIL_RELOAD_ONCE"
if run --install --ref "$first" >/dev/null 2>&1; then
  echo "simulated daemon reload failure was accepted" >&2
  exit 1
fi
test "$(readlink "$root/opt/ci-fleet-status/current")" = "releases/$first"
test "$(run --install --ref "$first")" = NO_CHANGE
assert_systemd_mode
test "$(readlink "$root/opt/ci-fleet-status/current")" = "releases/$first"
test -f "$root/opt/ci-fleet-status/current/status_receiver.py"
external="$tmp/external/$first"
mkdir -p "$external"
cp "$source_tree/scripts/status_receiver.py" "$source_tree/scripts/status_auth.py" "$external/"
ln -sfn "$external" "$root/opt/ci-fleet-status/current"
if run --check >/dev/null 2>&1; then
  echo "external same-basename release was accepted" >&2
  exit 1
fi
test "$(run --install --ref "$first")" = INSTALLED
assert_systemd_mode
test "$(readlink "$root/opt/ci-fleet-status/current")" = "releases/$first"
test "$(stat -c %a "$root/var/lib/ci-fleet-status")" = 700
test "$(stat -c %a "$root/run/lock")" = 1777
test "$(stat -c %a "$root/opt/ci-fleet-status/releases/$first")" = 755
cp "$source_tree/deploy/status-receiver/ci-fleet-status-receiver.service" "$tmp/first-unit"
rm "$root/etc/systemd/system/ci-fleet-status-receiver.service"
printf '%s\n' 'ExecStart=python3 --bind 127.0.0.1' >"$root/etc/systemd/system/ci-fleet-status-receiver.service"
test "$(run --install --ref "$first")" = NO_CHANGE
assert_systemd_mode
test -L "$root/etc/systemd/system/ci-fleet-status-receiver.service"
cmp "$source_tree/deploy/status-receiver/ci-fleet-status-receiver.service" \
  "$root/etc/systemd/system/ci-fleet-status-receiver.service"
printf '%s\n' 1 >"$root/var/lib/ci-fleet-status-installer/restart-required"
printf '%s\n' 1 >"$root/var/lib/ci-fleet-status-installer/previous-was-active"
test "$(run --upgrade --ref "$first")" = UPGRADED
test ! -e "$root/var/lib/ci-fleet-status-installer/restart-required"
test -e "$root/var/lib/ci-fleet-status-installer/previous-was-active"

printf '\n# dirty\n' >>"$source_tree/scripts/status_auth.py"
if run --upgrade --ref "$first" >/dev/null 2>&1; then
  echo "dirty reviewed input was installed" >&2
  exit 1
fi
git -C "$source_tree" checkout -q -- scripts/status_auth.py

for version in 3.24.9 3.25.0 3.99.0; do
  if output=$(CI_FLEET_STATUS_TEST_SQLITE_VERSION="$version" run --install --ref "$first" 2>&1); then
    [[ "$version" != 3.24.9 ]] || { echo "old SQLite was accepted" >&2; exit 1; }
  else
    [[ "$version" == 3.24.9 && "$output" == *"SQLite 3.25.0 or newer is required"* ]] || {
      echo "supported SQLite $version was rejected" >&2
      exit 1
    }
  fi
done

lock="$root/run/lock/ci-fleet-status"
ready="$tmp/lock-ready"
(flock 9; : >"$ready"; sleep 1) 9<"$lock" &
lock_pid=$!
for _ in {1..20}; do [[ -e "$ready" ]] && break; sleep 0.05; done
if CI_FLEET_STATUS_TEST_MODE=1 CI_FLEET_STATUS_ROOT="$root" \
  timeout 0.1 "$source_tree/scripts/install-status-receiver.sh" --check >/dev/null 2>&1; then
  echo "overlapping installer did not wait for lock" >&2
  exit 1
else
  test "$?" = 124
fi
wait "$lock_pid"

printf '\n# upgraded unit\n' >>"$source_tree/deploy/status-receiver/ci-fleet-status-receiver.service"
git -C "$source_tree" add .
git -C "$source_tree" commit -qm upgrade
second=$(git -C "$source_tree" rev-parse HEAD)
if run --install --ref "$second" >/dev/null 2>&1; then
  echo "install mode changed an active release" >&2
  exit 1
fi
printf '%s\n' 1 >"$root/var/lib/ci-fleet-status-installer/restart-required"
test "$(run --upgrade --ref "$second")" = UPGRADED
test ! -e "$root/var/lib/ci-fleet-status-installer/restart-required"
test -e "$root/var/lib/ci-fleet-status-installer/previous-was-active"
test "$(cat "$root/run/ci-fleet-status-last-restart-force")" = 1
assert_systemd_mode
test "$(readlink "$root/opt/ci-fleet-status/current")" = "releases/$second"
test "$(cat "$root/var/lib/ci-fleet-status-installer/previous-ref")" = "$first"
test "$(stat -c %a "$root/var/lib/ci-fleet-status-installer")" = 700
cmp "$source_tree/deploy/status-receiver/ci-fleet-status-receiver.service" \
  "$root/etc/systemd/system/ci-fleet-status-receiver.service"
printf '%s\n' 1 >"$root/var/lib/ci-fleet-status-installer/restart-required"
if run --check >/dev/null 2>&1; then
  echo "pending receiver restart was accepted" >&2
  exit 1
fi
rm "$root/var/lib/ci-fleet-status-installer/restart-required"
test "$(run --check)" = "CHECK_OK $second"
assert_systemd_mode
rm "$root/etc/systemd/system/ci-fleet-status-receiver.service"
printf '%s\n' drift >"$root/etc/systemd/system/ci-fleet-status-receiver.service"
test "$(run --rollback)" = "ROLLED_BACK $first"
test ! -e "$root/var/lib/ci-fleet-status-installer/previous-was-active"
assert_systemd_mode
test -L "$root/etc/systemd/system/ci-fleet-status-receiver.service"
test "$(readlink "$root/opt/ci-fleet-status/current")" = "releases/$first"
test "$(cat "$root/var/lib/ci-fleet-status-installer/previous-ref")" = "$first"
cmp "$tmp/first-unit" "$root/etc/systemd/system/ci-fleet-status-receiver.service"
test "$(run --upgrade --ref "$second")" = UPGRADED
test "$(run --rollback)" = "ROLLED_BACK $first"
assert_systemd_mode

active="$root/opt/ci-fleet-status/releases/$first"
printf '\n# modified\n' >>"$active/status_receiver.py"
if run --check >/dev/null 2>&1; then echo "modified artifact was accepted" >&2; exit 1; fi
git -C "$source_tree" show "$first:scripts/status_receiver.py" >"$active/status_receiver.py"
chmod 0755 "$active/status_receiver.py"
chmod 0664 "$active/status_auth.py"
if run --check >/dev/null 2>&1; then echo "writable artifact was accepted" >&2; exit 1; fi
chmod 0644 "$active/status_auth.py"
mv "$active/status_auth.py" "$tmp/status_auth.py.real"
ln -s "$tmp/status_auth.py.real" "$active/status_auth.py"
if run --rollback >/dev/null 2>&1; then echo "symlinked artifact was accepted" >&2; exit 1; fi
rm "$active/status_auth.py"
mv "$tmp/status_auth.py.real" "$active/status_auth.py"
if CI_FLEET_STATUS_TEST_MODE=1 CI_FLEET_STATUS_TEST_EXPECTED_OWNER=99999 CI_FLEET_STATUS_ROOT="$root" \
  "$source_tree/scripts/install-status-receiver.sh" --check >/dev/null 2>&1; then
  echo "service-owned release directory was accepted" >&2
  exit 1
fi
test "$(run --check)" = "CHECK_OK $first"
test "$(CI_FLEET_STATUS_TEST_MODE=1 CI_FLEET_STATUS_TEST_EXPECTED_OWNER="$(id -u)" \
  CI_FLEET_STATUS_TEST_ACCOUNT_GROUPS=12345 CI_FLEET_STATUS_ROOT="$root" \
  "$source_tree/scripts/install-status-receiver.sh" --check)" = "CHECK_OK $first"
if CI_FLEET_STATUS_TEST_MODE=1 CI_FLEET_STATUS_TEST_EXPECTED_OWNER="$(id -u)" \
  CI_FLEET_STATUS_TEST_ACCOUNT_GROUPS='12345 99999' CI_FLEET_STATUS_ROOT="$root" \
  "$source_tree/scripts/install-status-receiver.sh" --check >/dev/null 2>&1; then
  echo "service account supplementary groups were accepted" >&2
  exit 1
fi
for directory in "$root/etc/ci-fleet-status" "$root/var/lib/ci-fleet-status"; do
  mv "$directory" "$directory.real"
  ln -s "${directory##*/}.real" "$directory"
  if run --check >/dev/null 2>&1; then
    echo "symlinked receiver directory was accepted: $directory" >&2
    exit 1
  fi
  rm "$directory"
  mv "$directory.real" "$directory"
done
if grep -F '%F' "$source_tree/scripts/install-status-receiver.sh" >/dev/null; then
  echo "locale-dependent stat file types remain" >&2
  exit 1
fi

grep -F -- '--bind 127.0.0.1' "$unit" >/dev/null
grep -F 'User=ci-fleet-status' "$unit" >/dev/null
grep -F 'NoNewPrivileges=yes' "$unit" >/dev/null
grep -F 'ProtectSystem=strict' "$unit" >/dev/null
grep -F 'ReadWritePaths=/var/lib/ci-fleet-status' "$unit" >/dev/null
grep -F 'useradd --system --user-group' "$installer" >/dev/null
grep -F 'restart-required' "$installer" >/dev/null
grep -F 'getent passwd ci-fleet-status' "$installer" >/dev/null
grep -F "\$gid\" != 0" "$installer" >/dev/null
grep -F '/usr/bin/python3' "$installer" >/dev/null
grep -F 'SQLite 3.25.0 or newer is required' "$installer" >/dev/null

echo STATUS_RECEIVER_INSTALL_TESTS_OK
