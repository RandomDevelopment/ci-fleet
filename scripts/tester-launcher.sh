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
[[ -d $lock_dir && ! -L $lock_dir && $(stat -c %u "$lock_dir") == "$expected_uid" && $(stat -c %a "$lock_dir") == 755 ]] || { printf 'ERROR: tester lock directory is unsafe\n' >&2; exit 1; }
exec 8>"$lock_file"
flock -x 8
target=$(readlink -f "$current_link")
revision=$(basename "$target")
[[ $target == "$release_dir/$revision" && $revision =~ ^[0-9a-f]{40}$ && -x $target/scripts/tester-runtime.sh ]] || { printf 'ERROR: active tester release is invalid\n' >&2; exit 1; }
export CI_FLEET_TESTER_LOCK_FD=8
exec "$target/scripts/tester-runtime.sh" "$@"
