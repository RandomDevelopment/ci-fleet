#!/usr/bin/env bash
set -Eeuo pipefail

# Load diagnostics from the same pinned policy as this command plan.
# shellcheck source=scripts/validation_diagnostics.sh
source "$(dirname "${BASH_SOURCE[0]}")/validation_diagnostics.sh"

repo_root=${CI_FLEET_VALIDATION_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
cd "$repo_root"
case ${1:-} in
  ''|--quick|--containers) ;;
  *) printf 'unknown validation profile\n' >&2; exit 2 ;;
esac

if [[ ${1:-} != --containers ]]; then
for script in scripts/*.sh examples/project/scripts/ci/*.sh; do validation_command "syntax:$script" bash -n "$script"; done
validation_command python-compile python3 -m py_compile \
  scripts/validation_diagnostics.py \
  scripts/test_validation_diagnostics.py \
  scripts/validation_resources.py \
  scripts/test_validation_resources.py \
  scripts/validation_tasks.py \
  scripts/test_validation_tasks.py \
  scripts/fixture_manifest.py \
  scripts/test_fixture_manifest.py \
  scripts/test_validation_images.py \
  .github/actions/plan/plan.py \
  .github/actions/plan/test_plan.py \
  scripts/desired_state.py \
  scripts/managed_image_identity.py \
  tests/test_managed_image_identity.py \
  scripts/apt_cache.py \
  scripts/test_apt_cache.py \
  scripts/test_apt_cache_e2e.py \
  scripts/bootstrap-github-callback.py \
  scripts/fixtures/fake-bootstrap-curl.py \
  scripts/verify_config_approval.py \
  scripts/test_config_approval.py \
  scripts/local_validation_executor.py \
  scripts/test_local_validation_executor.py \
  scripts/cleanup_networks.py \
  scripts/cleanup_storage.py \
  scripts/test_cleanup_storage.py \
  scripts/test_proxy_memory.py \
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
  scripts/explicit_recovery.py \
  scripts/test_explicit_recovery.py \
  scripts/test_health.py \
  scripts/test_status_receiver.py \
  scripts/test_quickstart.py \
  scripts/test_update_actions_runner.py
task_args=(--root "$repo_root")
if [[ -n ${CI_FLEET_TASK_OUTPUT:-} ]]; then task_args+=(--output "$CI_FLEET_TASK_OUTPUT"); fi
if [[ -n ${CI_FLEET_TASK_RESUME:-} ]]; then task_args+=(--resume-from "$CI_FLEET_TASK_RESUME"); fi
validation_command regressions python3 "$(dirname "${BASH_SOURCE[0]}")/validation_tasks.py" "${task_args[@]}"

fi
if [[ ${1:-} == --quick ]]; then exit 0; fi

tmp=$(mktemp)
cleanup_validation() {
  result=$?
  rm -f "$tmp"
  if [[ -n ${CI_FLEET_VALIDATION_ID:-} ]]; then
    if ! docker info >/dev/null 2>&1; then
      result=1
    else
      if remaining=$(docker container ls --all --quiet --filter "name=^/${CI_FLEET_VALIDATION_ID}-runner-inspect$"); then
        if [[ -n $remaining ]]; then
          docker rm -f "${CI_FLEET_VALIDATION_ID}-runner-inspect" >/dev/null || result=1
        fi
      else
        result=1
      fi
      for image in "$CI_FLEET_RUNNER_IMAGE" "$CI_FLEET_CONTROLLER_IMAGE"; do
        if remaining=$(docker image ls --quiet "$image"); then
          if [[ -n $remaining ]]; then
            docker image rm "$image" >/dev/null || result=1
          fi
        else
          result=1
        fi
      done
    fi
  fi
  return "$result"
}
trap cleanup_validation EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
printf '%s\n' 'validation-only-not-a-private-key' >"$tmp"
export CI_FLEET_GITHUB_URL=https://github.com/EXAMPLE-ORG
export CI_FLEET_INSTANCE=validation
export CI_FLEET_GITHUB_APP_CLIENT_ID=validation
export CI_FLEET_GITHUB_APP_INSTALLATION_ID=1
export CI_FLEET_GITHUB_APP_PRIVATE_KEY_FILE="$tmp"
export CI_FLEET_DOCKER_GID=999
export CI_FLEET_RUNNER_IMAGE=${CI_FLEET_RUNNER_IMAGE:-ci-fleet-runner:dev}
export CI_FLEET_CONTROLLER_IMAGE=${CI_FLEET_CONTROLLER_IMAGE:-ci-fleet-controller:dev}
if validation_command reject-unpinned-compose env -u CI_FLEET_RUNNER_IMAGE_DIGEST -u CI_FLEET_CONTROLLER_IMAGE_DIGEST docker compose -f deploy/compose.yaml config --quiet; then
  printf 'FAIL: an incumbent installer can load runtime Compose without reviewed image IDs\n' >&2
  exit 1
fi
export CI_FLEET_RUNNER_IMAGE_DIGEST=sha256:4444444444444444444444444444444444444444444444444444444444444444
export CI_FLEET_CONTROLLER_IMAGE_DIGEST=sha256:3333333333333333333333333333333333333333333333333333333333333333
export CI_FLEET_RUNNER_RUNTIME_IMAGE_ID=$CI_FLEET_RUNNER_IMAGE_DIGEST
export CI_FLEET_CONTROLLER_RUNTIME_IMAGE_ID=$CI_FLEET_CONTROLLER_IMAGE_DIGEST
validation_command compose-config docker compose -f deploy/compose.yaml config --quiet
validation_command compose-json docker compose -f deploy/compose.yaml config --format json | validation_command compose-image-check python3 -I -c 'import json, os, sys; config = json.load(sys.stdin); assert config["services"]["controller"]["environment"]["CI_FLEET_RUNNER_IMAGE"] == os.environ["CI_FLEET_RUNNER_IMAGE_DIGEST"]'

export CI_FLEET_COMMIT
CI_FLEET_COMMIT=$(git rev-parse HEAD)
export CI_FLEET_VERSION=${CI_FLEET_COMMIT:0:12}
validation_command reference-images bash "$(dirname "${BASH_SOURCE[0]}")/build-validation-images.sh"
validation_command runner-inspect docker run --name "${CI_FLEET_VALIDATION_ID:-ci-fleet-validation}-runner-inspect" --rm --entrypoint /bin/bash "$CI_FLEET_RUNNER_IMAGE" -c './bin/Runner.Listener --version && docker --version && docker compose version && git --version'
validation_command proxy-memory python3 scripts/test_proxy_memory.py --image "$CI_FLEET_CONTROLLER_IMAGE"
cache_args=(--reference-runner "$CI_FLEET_RUNNER_IMAGE" --reference-controller "$CI_FLEET_CONTROLLER_IMAGE")
case ${CI_FLEET_IMAGE_COMPARISON:-required} in
  required) ;;
  unaffected) cache_args+=(--skip-managed-comparison) ;;
  *) printf 'invalid image comparison selection\n' >&2; exit 2 ;;
esac
validation_command apt-cache-e2e python3 scripts/test_apt_cache_e2e.py "${cache_args[@]}"
validation_command cleanup-e2e bash scripts/test-cleanup-e2e.sh
CI_FLEET_INSTANCE=${CI_FLEET_VALIDATION_ID:-validation} validation_command runner-cleanup scripts/cleanup.sh

validation_command secrets python3 scripts/scan_committed_secrets.py
