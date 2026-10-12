#!/usr/bin/env bash
set -Eeuo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
temporary=$(mktemp -d)
trap 'rm -rf "$temporary"' EXIT

{
  printf 'set -Eeuo pipefail\n'
  sed -n '/^stop_capacity_units() {/,/^}/p' "$repo_root/scripts/install-worker-controller.sh"
  cat <<'EOF'
systemd_dir=$1
systemctl() {
  case ${2:-} in
    --property=LoadState) printf 'not-found\n' ;;
    --property=ActiveState)
      [[ $test_unit_state != unavailable ]] || return 1
      printf '%s\n' "$test_unit_state" ;;
    *) return 1 ;;
  esac
}
for test_unit_state in inactive active activating deactivating reloading failed unknown unavailable; do
  result=blocked
  if stop_capacity_units; then result=allowed; fi
  expected=blocked
  [[ $test_unit_state != inactive ]] || expected=allowed
  [[ $result == "$expected" ]] || { printf 'FAIL: missing unit configuration with state %s was %s\n' "$test_unit_state" "$result" >&2; exit 1; }
done
EOF
} >"$temporary/check.sh"
bash "$temporary/check.sh" "$temporary"
printf 'CAPACITY_UNIT_STOP_TESTS_OK\n'

{
  printf 'set -Eeuo pipefail\n'
  sed -n '/^perform_uninstall() {/,/^}/p' "$repo_root/scripts/install-worker-controller.sh"
  cat <<'TEST'
state_root=$1/state
current_link=$1/current
manager_current=$1/manager-current
rendered_env=$1/env
state_file=$1/install-state
releases_dir=$1/releases
host_config=fixture
etc_dir=$1/etc
mkdir -p "$state_root/capacity" "$state_root/health"
printf 'retained observations\n' >"$state_root/capacity/samples.jsonl"
load_installed_controller_identity() { :; }
controller_status() { :; }
manager_release_from_raw_pointer() { :; }
current_runtime_release() { :; }
canonical_release_target() { :; }
make_checkpoint() { :; }
drain_current() { :; }
remove_inactive_managed_runners() { :; }
remove_systemd_units() { :; }
note() { :; }
rm() {
  if [[ ${fail_manager_removal:-false} == true && "$*" == "-f $manager_current" ]]; then return 1; fi
  command rm "$@"
}
transaction_active=false
fail_manager_removal=true
trap '[[ $transaction_active == true && -f $state_root/capacity/samples.jsonl ]] || exit 99; exit 7' ERR
perform_uninstall
TEST
} >"$temporary/uninstall.sh"
status=0
bash "$temporary/uninstall.sh" "$temporary" || status=$?
[[ $status == 7 ]] || { printf 'FAIL: uninstall removed capacity history before commit, status=%s\n' "$status" >&2; exit 1; }
sed -i 's/fail_manager_removal=true/fail_manager_removal=false/' "$temporary/uninstall.sh"
bash "$temporary/uninstall.sh" "$temporary"
[[ ! -e $temporary/state/capacity ]] || { printf 'FAIL: committed uninstall retained capacity history\n' >&2; exit 1; }
printf 'CAPACITY_UNINSTALL_TRANSACTION_TESTS_OK\n'
