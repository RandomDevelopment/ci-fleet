#!/usr/bin/env bash
set -Eeuo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo_root"

for script in scripts/*.sh examples/project/scripts/ci/*.sh; do bash -n "$script"; done

tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
printf '%s\n' 'validation-only-not-a-private-key' >"$tmp"
CI_FLEET_GITHUB_URL=https://github.com/EXAMPLE-ORG \
CI_FLEET_INSTANCE=validation \
CI_FLEET_GITHUB_APP_CLIENT_ID=validation \
CI_FLEET_GITHUB_APP_INSTALLATION_ID=1 \
CI_FLEET_GITHUB_APP_PRIVATE_KEY_FILE="$tmp" \
CI_FLEET_DOCKER_GID=999 \
docker compose -f deploy/compose.yaml config --quiet

docker compose -f deploy/compose.yaml build runner-image controller
docker run --rm --entrypoint /bin/bash ci-fleet-runner:dev -c './bin/Runner.Listener --version && docker --version && docker compose version && git --version'
CI_FLEET_INSTANCE=validation scripts/cleanup.sh

if rg -n --hidden --glob '!*.example' --glob '!scripts/validate.sh' --glob '!docs/SECRETS.md' --glob '!SECURITY.md' '(BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY|github_pat_|ghp_[A-Za-z0-9]{20,})' .; then
  echo "possible committed secret detected" >&2
  exit 1
fi
