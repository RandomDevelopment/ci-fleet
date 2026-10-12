#!/usr/bin/env bash
set -Eeuo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

sed -n '/^restore_consumed_requests() {$/,/^}$/p' "$repo_root/scripts/deployer-runtime.sh" >"$tmp/functions.sh"
# shellcheck source=/dev/null
source "$tmp/functions.sh"
expected_uid=$(id -u)
log_root=$tmp/log
consumed_root=$log_root/consumed-requests
prior_id=$(printf prior | sha256sum | cut -d' ' -f1)
missing_id=$(printf missing | sha256sum | cut -d' ' -f1)
extra_id=$(printf injected | sha256sum | cut -d' ' -f1)
# The sourced runtime function reads this array.
# shellcheck disable=SC2034
consumed_requests=("$prior_id" "$missing_id")

install() {
  if [[ ${!#} == "$consumed_root/$missing_id" ]]; then
    case $fault in
      write) return 1 ;;
      kill) kill -KILL "$BASHPID" ;;
    esac
  fi
  command install "$@"
}
sync() {
  [[ $fault != sync || "$*" != "-f $consumed_root $log_root" ]] || return 1
  command sync "$@"
}

for fault in write kill sync success; do
  rm -rf "$log_root"
  command install -d -m 0700 "$log_root" "$consumed_root"
  command install -m 0600 /dev/null "$consumed_root/$prior_id"
  command install -m 0600 /dev/null "$consumed_root/$extra_id"
  prior_inode=$(stat -c '%d:%i' "$consumed_root/$prior_id")
  status=0
  (restore_consumed_requests) >"$tmp/$fault.log" 2>&1 || status=$?
  if [[ $fault == success ]]; then
    ((status == 0)) || fail 'ledger reconstruction failed'
    [[ ! -e $consumed_root/$extra_id && -f $consumed_root/$missing_id ]] || fail 'ledger reconstruction retained injected state or lost a missing marker'
  else
    ((status != 0)) || fail "$fault did not interrupt reconstruction"
    [[ $fault != kill || $status == 137 ]] || fail 'reconstruction was not killed'
    [[ -f $consumed_root/$extra_id ]] || fail 'ledger pruning preceded durable restoration'
  fi
  [[ -f $consumed_root/$prior_id && $(stat -c '%d:%i:%u:%a:%s' "$consumed_root/$prior_id") == "$prior_inode:$expected_uid:600:0" ]] || fail "$fault replaced or lost a consumed approval"
done

printf 'DEPLOYER_REPLAY_LEDGER_TESTS_OK\n'
