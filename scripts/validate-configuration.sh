#!/usr/bin/env bash
set -Eeuo pipefail
cd "${CI_FLEET_VALIDATION_ROOT:?source checkout is required}"
BASE_SHA=${CI_FLEET_VALIDATION_BASE:?base commit is required}
trusted_template=$(cd "$(dirname "${BASH_SOURCE[0]}")/../templates/config-repository" && pwd)
validator=$trusted_template/scripts/validate.py
temporary_directory=$(mktemp -d)
trap 'rm -rf "$temporary_directory"' EXIT
bash -n scripts/init.sh scripts/validate.sh
python3 -m py_compile scripts/init.py scripts/validate.py scripts/test_policy.py scripts/scan_committed_secrets.py

# Policy tests use the pinned public fixtures, not an initialized adopter's fleet.
python3 "$trusted_template/scripts/test_policy.py"
git ls-tree -rz --name-only HEAD >"$temporary_directory/tree-paths"
current=(--config "$PWD/fleet.json" --tree-paths "$temporary_directory/tree-paths")
if [[ -f engine-rollout-evidence.json ]]; then current+=(--rollout-evidence "$PWD/engine-rollout-evidence.json"); fi

if [[ -n "$BASE_SHA" && ! "$BASE_SHA" =~ ^0+$ ]] && git cat-file -e "$BASE_SHA:fleet.json" 2>/dev/null; then
  git show "$BASE_SHA:fleet.json" >"$temporary_directory/previous-fleet.json"
  args=(--previous-config "$temporary_directory/previous-fleet.json")
  if git cat-file -e "$BASE_SHA:engine-rollout-evidence.json" 2>/dev/null; then
    git show "$BASE_SHA:engine-rollout-evidence.json" >"$temporary_directory/previous-rollout-evidence.json"
    args+=(--previous-rollout-evidence "$temporary_directory/previous-rollout-evidence.json")
  fi
  python3 "$validator" --strict "${current[@]}" "${args[@]}"
elif [[ "$BASE_SHA" == 0000000000000000000000000000000000000000 ]]; then
  # No integrated controller can supply capability or rollout evidence yet.
  printf '{"managed_images":{},"controllers":{}}\n' >"$temporary_directory/previous-fleet.json"
  python3 "$validator" --strict "${current[@]}" --previous-config "$temporary_directory/previous-fleet.json"
else
  python3 "$validator" --strict "${current[@]}"
fi
python3 "$validator" --config "$PWD/examples/multi-host/fleet.json" --tree-paths "$temporary_directory/tree-paths"


engine_ref=$(python3 -I -c 'import json; value = json.load(open("fleet.json", encoding="utf-8")); print(next(iter(value["controllers"].values()))["engine_ref"])')
if ./scripts/init.sh \
  --organization test-company \
  --project test-app \
  --output "${temporary_directory}/missing-engine.json"; then
  echo 'initializer accepted a missing engine revision' >&2
  exit 1
fi
test ! -e "${temporary_directory}/missing-engine.json"
if ./scripts/init.sh \
  --organization test-company \
  --project test-app \
  --registry BAD \
  --engine-ref "${engine_ref}" \
  --architecture amd64 \
  --controller-image-digest "sha256:$(printf '3%.0s' {1..64})" \
  --runner-image-digest "sha256:$(printf '4%.0s' {1..64})" \
  --output "${temporary_directory}/invalid.json"; then
  echo 'initializer accepted an invalid registry' >&2
  exit 1
fi
test ! -e "${temporary_directory}/invalid.json"
./scripts/init.sh \
  --organization test-company \
  --project test-app \
  --engine-ref "${engine_ref}" \
  --architecture amd64 \
  --controller-image-digest "sha256:$(printf '3%.0s' {1..64})" \
  --runner-image-digest "sha256:$(printf '4%.0s' {1..64})" \
  --output "${temporary_directory}/fleet.json"
test "$(stat -c '%a' "${temporary_directory}/fleet.json")" = 644
python3 "$validator" --config "${temporary_directory}/fleet.json" --skip-path-scan
