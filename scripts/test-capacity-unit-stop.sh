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
