#!/usr/bin/env bash
set -Eeuo pipefail
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
temporary=$(mktemp -d)
trap 'rm -rf "$temporary"' EXIT
{
  printf 'set -Eeuo pipefail\n'
  sed -n '/^mandatory_timers_active() {/,/^}/p' "$repo_root/scripts/install-worker-controller.sh"
  sed -n '/^stage_manager_only() {/,/^}/p' "$repo_root/scripts/install-worker-controller.sh"
  cat <<'TEST'
root=$1
mode=upgrade
state_file=$root/state
rendered_env=$root/env
systemd_dir=$root/units
release_dir=candidate-runtime
runtime=incumbent-runtime
timer_names=(ci-fleet-health.timer ci-fleet-cleanup.timer ci-fleet-drift.timer)
mkdir -p "$systemd_dir"
: >"$state_file"
printf 'CI_FLEET_ENGINE_REF=%040d\n' 1 >"$rendered_env"
die() { printf '%s\n' "$*" >&2; exit 1; }
install_release() { :; }
install_manager() { [[ $1 == "$engine_ref" && $2 == candidate-runtime ]]; printf '%s\n' "$engine_ref" >"$root/manager-ref"; }
current_runtime_release() { printf '%s\n' "$runtime"; }
install_systemd_units() { [[ $1 == incumbent-runtime ]]; }
staged_runtime_matches() { :; }
systemctl() { printf '%s\n' "$*" >>"$root/systemctl.log"; [[ ${timer_failure:-false} != true ]]; }
note() { printf '%s\n' "$*"; }
printf -v engine_ref '%040d' 2
stage_manager_only
grep -Fqx 'enable --now ci-fleet-health.timer ci-fleet-cleanup.timer ci-fleet-drift.timer' "$root/systemctl.log"
printf -v engine_ref '%040d' 1
stage_manager_only
timer_failure=true
if mandatory_timers_active; then exit 1; fi
# Fail after pointer publication, then retry that same staging revision.
set +e
( set -e; stage_manager_only )
status=$?
set -e
[[ $status != 0 && $(<"$root/manager-ref") == "$engine_ref" ]]
timer_failure=false
stage_manager_only
TEST
} >"$temporary/staging.sh"
bash "$temporary/staging.sh" "$temporary"
{
  printf 'set -Eeuo pipefail\n'
  sed -n '/^retag_checkpoint_image() {/,/^}/p' "$repo_root/scripts/install-worker-controller.sh"
  cat <<'TEST'
identifier=sha256:3333333333333333333333333333333333333333333333333333333333333333
docker() {
  case "$1 ${2:-}" in
    'image tag') [[ $test_case == retained ]] ;;
    'image inspect')
      if [[ $test_case == absent || $test_case == daemon_failed ]]; then
        printf 'Error: No such image: %s\n' "$identifier" >&2; return 1
      fi
      printf '%s\n' "$identifier" ;;
    'info ') [[ $test_case != daemon_failed ]] ;;
    *) return 1 ;;
  esac
}
for test_case in retained absent daemon_failed tag_failed; do
  result=0
  retag_checkpoint_image "$identifier" fixture-tag || result=$?
  case $test_case in retained) expected=0 ;; absent) expected=2 ;; *) expected=1 ;; esac
  [[ $result == "$expected" ]] || { printf 'FAIL: %s returned %s\n' "$test_case" "$result" >&2; exit 1; }
done
test_case=retained
trap 'status=0; retag_checkpoint_image "$identifier" fixture-tag || status=$?; [[ $status == 0 ]] || exit 99; exit 0' ERR
(exit 42)
TEST
} >"$temporary/recovery.sh"
bash "$temporary/recovery.sh"
{
  printf 'set -Eeuo pipefail\n'
  sed -n '/^checkpoint_images_reproducible() {/,/^}/p' "$repo_root/scripts/install-worker-controller.sh"
  sed -n '/^preflight_checkpoint_images() {/,/^}/p' "$repo_root/scripts/install-worker-controller.sh"
  sed -n '/^rebuild_checkpoint_images() {/,/^}/p' "$repo_root/scripts/install-worker-controller.sh"
  cat <<'TEST'
repo_root=$1
root=$2
mkdir -p "$root/legacy" "$root/reviewed"
printf '{"schema_version":1,"capabilities":{"reproducible_managed_images":false}}\n' >"$root/legacy/engine-capabilities.json"
cp "$repo_root/engine-capabilities.json" "$root/reviewed/engine-capabilities.json"
build_managed_images() { printf 'build\n' >>"$root/rebuild.log"; }
managed_images_match() { [[ ${match_images:-true} == true ]]; }
note() { printf '%s\n' "$*" >>"$root/rebuild-notes.log"; }
for legacy_manifest in missing disabled; do
  if [[ $legacy_manifest == missing ]]; then release=$root/absent; else release=$root/legacy; fi
  if rebuild_checkpoint_images "$release" fixture-env 2>/dev/null; then exit 1; fi
  [[ ! -e "$root/rebuild.log" ]]
  grep -Fq ROLLBACK_IMAGE_REINSTALL_REQUIRED "$root/rebuild-notes.log"
done
identifier=sha256:3333333333333333333333333333333333333333333333333333333333333333
printf 'CI_FLEET_CONTROLLER_IMAGE_DIGEST=%s\nCI_FLEET_RUNNER_IMAGE_DIGEST=%s\n' "$identifier" "$identifier" >"$root/checkpoint.env"
checkpoint_images=(controller runner "$identifier" absent)
docker() { [[ ${image_present:-false} == true ]] || return 1; printf '%s\n' "$identifier"; }
if preflight_checkpoint_images "$root/legacy" "$root/checkpoint.env"; then exit 1; fi
[[ ! -e "$root/rebuild.log" ]]
image_present=true
preflight_checkpoint_images "$root/legacy" "$root/checkpoint.env"
image_present=false
preflight_checkpoint_images "$root/reviewed" "$root/checkpoint.env"
rebuild_checkpoint_images "$root/reviewed" fixture-env
[[ $(<"$root/rebuild.log") == build ]]
match_images=false
if rebuild_checkpoint_images "$root/reviewed" fixture-env; then exit 1; fi
TEST
} >"$temporary/rebuild.sh"
bash "$temporary/rebuild.sh" "$repo_root" "$temporary"
{
  printf 'set -Eeuo pipefail\n'
  sed -n '/^build_managed_images() {/,/^}/p' "$repo_root/scripts/install-worker-controller.sh"
  cat <<'TEST'
root=$1
mkdir -p "$root/older-release/scripts"
printf 'printf "older-release-contract %%s %%s\\n" "$1" "$2"\n' >"$root/older-release/scripts/build-managed-images.sh"
[[ $(build_managed_images "$root/older-release" old-env) == "older-release-contract $root/older-release old-env" ]]
TEST
} >"$temporary/release-build.sh"
bash "$temporary/release-build.sh" "$temporary"
printf 'MANAGER_STAGING_AND_IMAGE_RECOVERY_TESTS_OK\n'
