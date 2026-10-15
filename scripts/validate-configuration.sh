#!/usr/bin/env bash
set -Eeuo pipefail
cd "${CI_FLEET_VALIDATION_ROOT:?source checkout is required}"
BASE_SHA=${CI_FLEET_VALIDATION_BASE:?base commit is required}
temporary_directory=$(mktemp -d)
trap 'rm -rf "$temporary_directory"' EXIT
bash -n scripts/init.sh scripts/validate.sh
python3 -m py_compile scripts/init.py scripts/validate.py scripts/test_policy.py scripts/scan_committed_secrets.py

python3 scripts/test_policy.py

if [[ -n "$BASE_SHA" && ! "$BASE_SHA" =~ ^0+$ ]] && git cat-file -e "$BASE_SHA:fleet.json" 2>/dev/null; then
  git show "$BASE_SHA:fleet.json" >"$temporary_directory/previous-fleet.json"
  args=(--previous-config "$temporary_directory/previous-fleet.json")
  if git cat-file -e "$BASE_SHA:engine-rollout-evidence.json" 2>/dev/null; then
    git show "$BASE_SHA:engine-rollout-evidence.json" >"$temporary_directory/previous-rollout-evidence.json"
    args+=(--previous-rollout-evidence "$temporary_directory/previous-rollout-evidence.json")
  fi
  ./scripts/validate.sh --strict "${args[@]}"
else
  ./scripts/validate.sh --strict
fi
./scripts/validate.sh --config examples/multi-host/fleet.json


engine_ref=$(python3 -c 'import json; value = json.load(open("fleet.json", encoding="utf-8")); print(next(iter(value["controllers"].values()))["engine_ref"])')
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
