#!/usr/bin/env bash
set -Eeuo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo_root"

for script in scripts/*.sh examples/project/scripts/ci/*.sh; do bash -n "$script"; done
python3 -m py_compile \
  .github/actions/plan/plan.py \
  .github/actions/plan/test_plan.py \
  scripts/desired_state.py \
  scripts/apt_cache.py \
  scripts/test_apt_cache.py \
  scripts/test_apt_cache_e2e.py \
  scripts/bootstrap-github-callback.py \
  scripts/fixtures/fake-bootstrap-curl.py \
  scripts/verify_config_approval.py \
  scripts/test_config_approval.py \
  scripts/cleanup_networks.py \
  scripts/cleanup_storage.py \
  scripts/test_cleanup_storage.py \
  scripts/health.py \
  scripts/endpoint_snapshot.py \
  scripts/endpoint_snapshot_sources.py \
  scripts/endpoint_snapshot_store.py \
  scripts/test_endpoint_snapshot.py \
  scripts/test_endpoint_snapshot_sources.py \
  scripts/test_endpoint_snapshot_store.py \
  scripts/status_auth.py \
  scripts/status_receiver.py \
  scripts/scan_committed_secrets.py \
  scripts/update_actions_runner.py \
  scripts/test_desired_state.py \
  scripts/test_cleanup_networks.py \
  scripts/test_apply_docker_network_policy.py \
  scripts/test_remote_reconcile.py \
  scripts/test_health.py \
  scripts/test_status_receiver.py \
  scripts/test_quickstart.py \
  scripts/test_update_actions_runner.py
python3 .github/actions/plan/test_plan.py
python3 scripts/test_update_actions_runner.py
python3 scripts/test_desired_state.py
python3 scripts/test_apt_cache.py
python3 scripts/test_config_approval.py
python3 scripts/test_cleanup_networks.py
python3 scripts/test_cleanup_storage.py
python3 scripts/test_apply_docker_network_policy.py
python3 scripts/test_remote_reconcile.py
python3 scripts/test_health.py
python3 scripts/test_endpoint_snapshot.py
python3 scripts/test_endpoint_snapshot_sources.py
python3 scripts/test_endpoint_snapshot_store.py
python3 scripts/test_status_receiver.py
python3 scripts/test_quickstart.py
scripts/test-bootstrap-github.sh
python3 scripts/test_validate_commits.py
python3 scripts/test_workflow_tag_validation.py
python3 -m json.tool schemas/status-report-v1.json >/dev/null
python3 scripts/desired_state.py validate-engine-capabilities --manifest engine-capabilities.json --require-docker-network-policy-config --require-status-reporting-config --require-status-reporting >/dev/null
python3 .github/actions/plan/plan.py --plan examples/project/scripts/ci/plan.json --group fast >/dev/null
python3 .github/actions/plan/plan.py --plan examples/project/scripts/ci/plan.json --group full >/dev/null
scripts/test-capacity-preflight.sh
scripts/test-numeric-regressions.sh
scripts/test-install-tester.sh
bash scripts/test-capacity-unit-stop.sh
bash scripts/test-manager-staging.sh
bash scripts/test-managed-image-build.sh
scripts/test-healthcheck.sh
bash scripts/test-systemd-rollback.sh
scripts/test-install-worker-controller.sh
scripts/test-install-deployer.sh
python3 scripts/test_deployer_adapter.py
scripts/test-deployer-units.sh
scripts/test-install-status-receiver.sh

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
if env -u CI_FLEET_RUNNER_IMAGE_DIGEST -u CI_FLEET_CONTROLLER_IMAGE_DIGEST docker compose -f deploy/compose.yaml config --quiet; then
  printf 'FAIL: an incumbent installer can load runtime Compose without reviewed image IDs\n' >&2
  exit 1
fi
export CI_FLEET_RUNNER_IMAGE_DIGEST=sha256:4444444444444444444444444444444444444444444444444444444444444444
export CI_FLEET_CONTROLLER_IMAGE_DIGEST=sha256:3333333333333333333333333333333333333333333333333333333333333333
docker compose -f deploy/compose.yaml config --quiet
docker compose -f deploy/compose.yaml config --format json | python3 -c 'import json, os, sys; config = json.load(sys.stdin); assert config["services"]["controller"]["environment"]["CI_FLEET_RUNNER_IMAGE"] == os.environ["CI_FLEET_RUNNER_IMAGE_DIGEST"]'

docker compose -f deploy/compose.yaml build runner-image controller
docker run --rm --entrypoint /bin/bash "$CI_FLEET_RUNNER_IMAGE" -c './bin/Runner.Listener --version && docker --version && docker compose version && git --version'
python3 scripts/test_apt_cache_e2e.py
bash scripts/test-cleanup-e2e.sh
CI_FLEET_INSTANCE=validation scripts/cleanup.sh

python3 scripts/scan_committed_secrets.py
