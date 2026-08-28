#!/usr/bin/env bash
# shellcheck disable=SC2016  # backslash-$ in grep -E patterns is intentional here
# Regression for PR #76 rewrite corruption: four numeric comparisons in the
# tester installer/runtime were rewritten to the invalid token `-private-repository`,
# which is a bash syntax error (SC1073/SC1035/SC1072) and broke exact-head CI.
# Each site must use a real numeric comparison (`-lt`). This test reproduces all
# four (fails on corruption) and passes once `-lt` is restored.
set -Eeuo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
installer=$repo_root/scripts/install-tester.sh
runtime=$repo_root/scripts/tester-runtime.sh
test_install=$repo_root/scripts/test-install-tester.sh

fail() { printf 'FAIL %s\n' "$*" >&2; exit 1; }

# Execution-level reproduction: a corrupted comparison makes the whole script
# unparseable, so `bash -n` must succeed on every affected file.
for f in "$installer" "$runtime" "$test_install"; do
  bash -n "$f" || fail "syntax error in $f (corrupted numeric comparison?)"
done

# Per-site assertions: the corrupted token must be gone, and the correct `-lt`
# numeric comparison must be present.
check_site() {
  local file=$1 label=$2 pattern=$3
  if grep -Eq -- '-private-repository' "$file"; then
    fail "$label: corrupted '-private-repository' token still present in $file"
  fi
  if ! grep -Eq "$pattern" "$file"; then
    fail "$label: expected numeric '-lt' comparison not found in $file"
  fi
}

# 1. install-tester.sh host_preflight: disk usage below 80% warn gate.
check_site "$installer" "install-tester.sh:90 disk<80" 'used -lt 80'
# 2. tester-runtime.sh prepare_converge: tracked count below max_environments.
check_site "$runtime" "tester-runtime.sh:192 count<max" 'count -lt '
# 3. tester-runtime.sh --check: disk usage below the configured warn threshold.
check_site "$runtime" "tester-runtime.sh:281 disk<warn" 'used -lt \$disk_warn'
# 4. test-install-tester.sh timer-order: disable < check < enable line numbers.
check_site "$test_install" "test-install-tester.sh:201 timer-order" '\$disable_line -lt \$check_line && \$check_line -lt \$enable_line'

# Behavioral lock of the `<` direction using the exact expression shape.
bash -c 'used=20; disk_warn=80; [[ $used =~ ^[0-9]+$ && $used -lt $disk_warn ]]' || fail 'low disk usage should pass the warn gate'
if bash -c 'used=90; disk_warn=80; [[ $used =~ ^[0-9]+$ && $used -lt $disk_warn ]]'; then
  fail 'high disk usage should trip the warn gate'
fi
bash -c 'count=2; max=3; [[ -f /nonexistent || $count -lt $max ]]' || fail 'new environment below max should be allowed'
bash -c 'count=3; max=3; [[ -f /nonexistent || $count -lt $max ]]' && fail 'new environment at max should be rejected'
tmp_existing=$(mktemp); bash -c 'count=3; max=3; [[ -f '"$tmp_existing"' || $count -lt $max ]]' || fail 'existing environment at max should still be allowed via existing-state path'; rm -f "$tmp_existing"

printf 'NUMERIC_REGRESSIONS_OK\n'
