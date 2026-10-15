#!/usr/bin/env bash
set -Eeuo pipefail
cd "${CI_FLEET_VALIDATION_ROOT:-$(dirname "${BASH_SOURCE[0]}")/..}"
cmp -s scripts/scan_committed_secrets.py templates/config-repository/scripts/scan_committed_secrets.py
python3 templates/config-repository/scripts/test_policy.py
python3 templates/config-repository/scripts/scan_committed_secrets.py
templates/config-repository/scripts/validate.sh
templates/config-repository/scripts/validate.sh --config templates/config-repository/examples/multi-host/fleet.json
temporary_directory="$(mktemp -d)"
trap 'rm -rf "$temporary_directory"' EXIT
if templates/config-repository/scripts/init.sh \
  --organization test-company \
  --project test-app \
  --output "${temporary_directory}/missing-engine.json"; then
  echo 'initializer accepted a missing engine revision' >&2
  exit 1
fi
test ! -e "${temporary_directory}/missing-engine.json"
if templates/config-repository/scripts/init.sh \
  --organization test-company \
  --project test-app \
  --registry BAD \
  --engine-ref "$(git rev-parse HEAD)" \
  --architecture amd64 \
  --controller-image-digest "sha256:$(printf '3%.0s' {1..64})" \
  --runner-image-digest "sha256:$(printf '4%.0s' {1..64})" \
  --output "${temporary_directory}/invalid.json"; then
  echo 'initializer accepted an invalid registry' >&2
  exit 1
fi
test ! -e "${temporary_directory}/invalid.json"
templates/config-repository/scripts/init.sh \
  --organization test-company \
  --project test-app \
  --engine-ref "$(git rev-parse HEAD)" \
  --architecture amd64 \
  --controller-image-digest "sha256:$(printf '3%.0s' {1..64})" \
  --runner-image-digest "sha256:$(printf '4%.0s' {1..64})" \
  --output "${temporary_directory}/fleet.json"
test "$(stat -c '%a' "${temporary_directory}/fleet.json")" = 644
