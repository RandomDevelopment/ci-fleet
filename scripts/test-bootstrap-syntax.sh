#!/usr/bin/env bash
# Regression gate for the GitHub App bootstrap registration deadline loop.
#
# The privacy rewrite scrubbed the org identity "RandomDevelopment" to the
# placeholder "private-repository" repo-wide. That global substitution also
# hit a shell numeric comparison operator inside scripts/bootstrap-github.sh,
# turning `$SECONDS -lt $deadline` into `$SECONDS -private-repository $deadline`.
# The result is not valid bash: `bash -n` fails and shellcheck reports
# SC1035/SC1072/SC1073, so the script can never be parsed or run.
#
# This test is intentionally static (no network) so it fails deterministically
# on the corrupted operator and passes once the valid comparison is restored.
set -Eeuo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
script=$script_dir/bootstrap-github.sh

# 1. The script must parse as valid bash at all.
bash -n "$script" || { echo "FAIL bootstrap-github.sh is not valid bash (syntax error)"; exit 1; }

# 2. The registration deadline loop must use a valid numeric comparison.
#    `-private-repository` is not a test operator and breaks `[[ ... ]]`.
# shellcheck disable=SC2016  # literal \$SECONDS pattern, not expansion
if grep -Eq '\$SECONDS -private-repository \$deadline' "$script"; then
  echo "FAIL corrupted comparison operator present in registration deadline loop"
  exit 1
fi

# 3. A real numeric comparison against the deadline must be present.
# shellcheck disable=SC2016  # literal \$SECONDS pattern, not expansion
grep -Eq '\$SECONDS[[:space:]]+-[a-z]{2}[[:space:]]+\$deadline' "$script" \
  || { echo "FAIL registration deadline loop missing a valid numeric comparison"; exit 1; }

echo "BOOTSTRAP_SYNTAX_OK"
