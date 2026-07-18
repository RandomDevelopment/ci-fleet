#!/usr/bin/env bash
set -Eeuo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo_root"

for script in scripts/*.sh examples/project/scripts/ci/*.sh; do bash -n "$script"; done
python3 -m py_compile \
  .github/actions/plan/plan.py \
  .github/actions/plan/test_plan.py \
  scripts/desired_state.py \
  scripts/test_desired_state.py
python3 .github/actions/plan/test_plan.py
python3 scripts/test_desired_state.py
python3 .github/actions/plan/plan.py --plan examples/project/scripts/ci/plan.json --group fast >/dev/null
python3 .github/actions/plan/plan.py --plan examples/project/scripts/ci/plan.json --group full >/dev/null
scripts/test-capacity-preflight.sh
scripts/test-install-worker-controller.sh

tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
printf '%s\n' 'validation-only-not-a-private-key' >"$tmp"
export CI_FLEET_GITHUB_URL=https://github.com/EXAMPLE-ORG
export CI_FLEET_INSTANCE=validation
export CI_FLEET_GITHUB_APP_CLIENT_ID=validation
export CI_FLEET_GITHUB_APP_INSTALLATION_ID=1
export CI_FLEET_GITHUB_APP_PRIVATE_KEY_FILE="$tmp"
export CI_FLEET_DOCKER_GID=999
export CI_FLEET_RUNNER_IMAGE=${CI_FLEET_RUNNER_IMAGE:-ci-fleet-runner:dev}
export CI_FLEET_CONTROLLER_IMAGE=${CI_FLEET_CONTROLLER_IMAGE:-ci-fleet-controller:dev}
docker compose -f deploy/compose.yaml config --quiet

docker compose -f deploy/compose.yaml build runner-image controller
docker run --rm --entrypoint /bin/bash "$CI_FLEET_RUNNER_IMAGE" -c './bin/Runner.Listener --version && docker --version && docker compose version && git --version'
CI_FLEET_INSTANCE=validation scripts/cleanup.sh

if rg -n --hidden --glob '!*.example' --glob '!scripts/validate.sh' --glob '!docs/SECRETS.md' --glob '!SECURITY.md' '(BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY|github_pat_|ghp_[A-Za-z0-9]{20,})' .; then
  echo "possible committed secret detected" >&2
  exit 1
fi
