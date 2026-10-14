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
runtime=$root/retained-runtime
timer_names=(ci-fleet-health.timer ci-fleet-cleanup.timer ci-fleet-drift.timer)
unit_names=(ci-fleet-health.service ci-fleet-cleanup.service ci-fleet-drift.service "${timer_names[@]}")
capacity_unit_names=(ci-fleet-capacity.service ci-fleet-capacity.timer)
mkdir -p "$systemd_dir"
mkdir -p "$runtime/host/systemd" "$runtime/scripts"
for unit in "${unit_names[@]}"; do printf 'trusted %s\n' "$unit" >"$runtime/host/systemd/$unit"; done
: >"$state_file"
printf 'CI_FLEET_ENGINE_REF=%040d\n' 1 >"$rendered_env"
die() { printf '%s\n' "$*" >&2; exit 1; }
install_release() { :; }
install_manager() { [[ $1 == "$engine_ref" && $2 == candidate-runtime ]]; printf '%s\n' "$engine_ref" >"$root/manager-ref"; }
current_runtime_release() { printf '%s\n' "$runtime"; }
install_systemd_units() { [[ $1 == "$runtime" ]]; cp "$runtime/host/systemd/"* "$systemd_dir/"; }
staged_runtime_matches() { :; }
systemctl() { printf '%s\n' "$*" >>"$root/systemctl.log"; [[ ${timer_failure:-false} != true && ${!#} != "${failed_timer:-}" ]]; }
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
printf 'changed\n' >>"$systemd_dir/ci-fleet-drift.service"
if mandatory_timers_active; then exit 1; fi
cp "$runtime/host/systemd/ci-fleet-drift.service" "$systemd_dir/ci-fleet-drift.service"
rm "$systemd_dir/ci-fleet-health.service"
if mandatory_timers_active; then exit 1; fi
cp "$runtime/host/systemd/ci-fleet-health.service" "$systemd_dir/ci-fleet-health.service"
touch "$runtime/scripts/capacity-sample.sh"
chmod +x "$runtime/scripts/capacity-sample.sh"
for unit in "${capacity_unit_names[@]}"; do printf 'trusted %s\n' "$unit" >"$runtime/host/systemd/$unit"; done
install_systemd_units "$runtime"
mandatory_timers_active
failed_timer=ci-fleet-capacity.timer
if mandatory_timers_active; then exit 1; fi
failed_timer=''
rm "$systemd_dir/ci-fleet-capacity.timer"
if mandatory_timers_active; then exit 1; fi
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
  sed -n '/^managed_image_objects_match() {/,/^}/p' "$repo_root/scripts/install-worker-controller.sh"
  sed -n '/^checkpoint_images_reproducible() {/,/^}/p' "$repo_root/scripts/install-worker-controller.sh"
  sed -n '/^restore_reviewed_image_tags() {/,/^}/p' "$repo_root/scripts/install-worker-controller.sh"
  sed -n '/^preflight_checkpoint_images() {/,/^}/p' "$repo_root/scripts/install-worker-controller.sh"
  sed -n '/^rebuild_checkpoint_images() {/,/^}/p' "$repo_root/scripts/install-worker-controller.sh"
  cat <<'TEST'
repo_root=$1
root=$2
mkdir -p "$root/legacy" "$root/reviewed"
printf '{"schema_version":1,"capabilities":{"reproducible_managed_images":false}}\n' >"$root/legacy/engine-capabilities.json"
cp "$repo_root/engine-capabilities.json" "$root/reviewed/engine-capabilities.json"
build_managed_images() { [[ ${build_available:-true} == true ]] || return 1; printf 'build\n' >>"$root/rebuild.log"; image_present=true; }
managed_images_match() { [[ ${match_images:-true} == true ]]; }
note() { printf '%s\n' "$*" >>"$root/rebuild-notes.log"; }
for legacy_manifest in missing disabled; do
  if [[ $legacy_manifest == missing ]]; then release=$root/absent; else release=$root/legacy; fi
  if rebuild_checkpoint_images "$release" fixture-env 2>/dev/null; then exit 1; fi
  [[ ! -e "$root/rebuild.log" ]]
  grep -Fq ROLLBACK_IMAGE_REINSTALL_REQUIRED "$root/rebuild-notes.log"
done
identifier=sha256:3333333333333333333333333333333333333333333333333333333333333333
printf 'CI_FLEET_ENGINE_REF=0000000000000000000000000000000000000001\nCI_FLEET_CONTROLLER_IMAGE=controller\nCI_FLEET_RUNNER_IMAGE=runner\nCI_FLEET_CONTROLLER_IMAGE_DIGEST=%s\nCI_FLEET_RUNNER_IMAGE_DIGEST=%s\n' "$identifier" "$identifier" >"$root/checkpoint.env"
checkpoint_images=(controller runner "$identifier" absent)
docker() {
  case "$1 ${2:-}" in
    'image inspect')
      if [[ ${image_present:-false} == true ]]; then
        if [[ "$*" == *'{{eq '* ]]; then printf '%s\n' "${match_images:-true}"; else printf '%s\n' "$identifier"; fi
      else printf 'Error: No such image: %s\n' "$identifier" >&2; return 1; fi ;;
    'image tag') printf 'tag\n' >>"$root/tag.log" ;;
    'info ') [[ ${daemon_available:-true} == true ]] ;;
    *) return 1 ;;
  esac
}
if preflight_checkpoint_images "$root/legacy" "$root/checkpoint.env"; then exit 1; fi
[[ ! -e "$root/rebuild.log" ]]
image_present=true
preflight_checkpoint_images "$root/legacy" "$root/checkpoint.env"
image_present=false
build_available=false
before_tags=$(cat "$root/tag.log" 2>/dev/null || true)
if preflight_checkpoint_images "$root/reviewed" "$root/checkpoint.env"; then exit 1; fi
[[ ! -e "$root/rebuild.log" && $(cat "$root/tag.log" 2>/dev/null || true) == "$before_tags" ]]
build_available=true
daemon_available=false
if preflight_checkpoint_images "$root/reviewed" "$root/checkpoint.env"; then exit 1; fi
[[ ! -e "$root/rebuild.log" ]]
daemon_available=true
preflight_checkpoint_images "$root/reviewed" "$root/checkpoint.env"
[[ $(<"$root/rebuild.log") == build && "$image_present" == true ]]
preflight_checkpoint_images "$root/reviewed" "$root/checkpoint.env"
[[ $(wc -l <"$root/rebuild.log") == 1 ]]
[[ ! -e "$root/tag.log" ]] || { printf 'preflight changed canonical tags\n' >&2; exit 1; }
rebuild_checkpoint_images "$root/reviewed" "$root/checkpoint.env"
[[ $(wc -l <"$root/rebuild.log") == 2 ]]
match_images=false
if rebuild_checkpoint_images "$root/reviewed" "$root/checkpoint.env"; then exit 1; fi
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
python3 - "$repo_root" "$temporary" <<'PYTHON'
import copy
import json
from pathlib import Path
import re
import subprocess
import sys

repo, root = map(Path, sys.argv[1:])
configuration = root / "staging-config"
configuration.mkdir()
value = json.loads((repo / "templates/config-repository/fleet.json").read_text())
value.pop("managed_images")
value["organization"].update(slug="sample-org", registry="ghcr.io/sample-org")
project = next(iter(value["projects"].values()))
value["runner_pools"][project["ci_pool"]]["allowed_repositories"] = ["sample-org/sample-app"]
project.update(repository="sample-org/sample-app", image="ghcr.io/sample-org/sample-app")
for controller in value["controllers"].values():
    controller["docker_network_policy"]["default_address_pools"][0]["base"] = "10.64.0.0/24"
evidence = {"schema_version": 1, "status_reporting_engine_capabilities": {
    name: dict(engine_ref="1" * 40, docker_network_policy_config=True, docker_default_bridge_cidr_config=True, status_reporting_config=False, required_status_reporting=False)
    for name in value["controllers"]}}
(configuration / "engine-rollout-evidence.json").write_text(json.dumps(evidence))
subprocess.run(["git", "init", "-q", str(configuration)], check=True)
for index in range(2):
    candidate = copy.deepcopy(value)
    for controller in candidate["controllers"].values():
        controller["engine_ref"] = str(index + 1) * 40
    (configuration / "fleet.json").write_text(json.dumps(candidate))
    subprocess.run(["git", "-C", str(configuration), "add", "."], check=True)
    subprocess.run(["git", "-C", str(configuration), "-c", "user.name=fixture", "-c", "user.email=fixture@invalid.example", "commit", "-qm", "fixture"], check=True)
function = re.search(r"^validate_candidate_config_commit\(\) \{.*?^\}", (repo / "scripts/install-worker-controller.sh").read_text(), re.M | re.S).group()
wrapper = root / "validate-staging.sh"
wrapper.write_text('''set -Eeuo pipefail
mode=$1
restore_lkg=false
repo_root=$2
temporary=$3
config_source_checkout=$4
config_ref=$(git -C "$config_source_checkout" rev-parse HEAD)
candidate_config=$config_source_checkout/fleet.json
config_identity=fixture-org/fleet-config
controller_id=example-ci-01
testing=1
state_file=$temporary/state
rendered_env=$temporary/env
mkdir -p "$temporary"
if [[ "$mode" != install ]]; then touch "$state_file" "$rendered_env"; fi
die() { printf '%s\\n' "$*" >&2; exit 1; }
''' + function + "\nvalidate_candidate_config_commit\n")
for mode in ("upgrade", "check", "install"):
    result = subprocess.run(["bash", str(wrapper), mode, str(repo), str(root / ("context-" + mode)), str(configuration)], capture_output=True, text=True)
    assert (result.returncode == 0) == (mode != "install"), (mode, result.stdout, result.stderr)
    if mode == "install":
        assert "initial strict configuration requires reviewed image IDs" in result.stderr
staged_ref = subprocess.check_output(["git", "-C", str(configuration), "rev-parse", "HEAD"], text=True).strip()
candidate["managed_images"] = {"2" * 40: {"amd64": dict(controller="sha256:" + "3" * 64, runner="sha256:" + "4" * 64)}}
(configuration / "fleet.json").write_text(json.dumps(candidate))
subprocess.run(["git", "-C", str(configuration), "add", "."], check=True)
subprocess.run(["git", "-C", str(configuration), "-c", "user.name=fixture", "-c", "user.email=fixture@invalid.example", "commit", "-qm", "activation"], check=True)
result = subprocess.run(["bash", str(wrapper), "upgrade", str(repo), str(root / "context-activation"), str(configuration)], capture_output=True, text=True)
assert result.returncode == 0, (result.stdout, result.stderr)
remote = re.search(r"^validate_config\(\) \{.*?^\}", (repo / "scripts/remote-reconcile.sh").read_text(), re.M | re.S).group()
remote_wrapper = root / "validate-reconciliation.sh"
remote_wrapper.write_text('''set -Eeuo pipefail
repo_root=$1
temp_dir=$2
mkdir -p "$temp_dir"
log_json() { printf '%s\\n' "$*" >&2; }
''' + remote + '\nvalidate_config "$3" "$4"\n')
activation_ref = subprocess.check_output(["git", "-C", str(configuration), "rev-parse", "HEAD"], text=True).strip()
for commit in (staged_ref, activation_ref):
    result = subprocess.run(["bash", str(remote_wrapper), str(repo), str(root / ("remote-" + commit)), str(configuration), commit], capture_output=True, text=True)
    assert result.returncode == 0, (commit, result.stdout, result.stderr)
legacy = root / "retained-config"
subprocess.run(["git", "clone", "-q", str(configuration), str(legacy)], check=True)
subprocess.run(["git", "-C", str(legacy), "checkout", "-q", staged_ref + "^"], check=True)
legacy_ref = subprocess.check_output(["git", "-C", str(legacy), "rev-parse", "HEAD"], text=True).strip()
context = root / "context-retained"
context.mkdir()
(context / "state").write_text(json.dumps(dict(schema_version=1, config_repository="fixture-org/fleet-config", config_ref=legacy_ref, controller="example-ci-01", engine_ref="1" * 40)))
(context / "env").write_text(f"CI_FLEET_CONFIG_REPOSITORY=fixture-org/fleet-config\nCI_FLEET_CONFIG_REF={legacy_ref}\nCI_FLEET_INSTANCE=example-ci-01\nCI_FLEET_ENGINE_REF={'1' * 40}\n")
for name in ("state", "env"):
    (context / name).chmod(0o600)
result = subprocess.run(["bash", str(wrapper), "check", str(repo), str(context), str(legacy)], capture_output=True, text=True)
assert result.returncode == 0, (result.stdout, result.stderr)
check = re.search(r"^perform_check\(\) \{.*?^\}", (repo / "scripts/install-worker-controller.sh").read_text(), re.M | re.S).group()
admission = re.search(r"^reject_isolated_host\(\) \{.*?^\}", (repo / "scripts/install-worker-controller.sh").read_text(), re.M | re.S).group()
probe = root / "retained-check.sh"
probe.write_text('''set -Eeuo pipefail
root=$1
root_path() { printf '%s%s' "$root" "$1"; }
systemctl() { [[ $1 == list-unit-files || $1 == list-units ]]; }
retained_installed_ref=true
config_ref=retained
engine_ref=old-runtime
manager_releases=/trusted-managers
display_last_health() { :; }
managed_image_ids_configured() { return 1; }
manager_release_from_raw_pointer() { printf '/trusted-managers/new-manager\\n'; }
staged_runtime_matches() { return "${runtime_failure:-0}"; }
mandatory_timers_active() { return "${timer_failure:-0}"; }
note() { printf '%s\\n' "$*"; }
die() { printf '%s\\n' "$*" >&2; exit 1; }
''' + admission + '\n' + check + "\nperform_check\n")
result = subprocess.run(["bash", str(probe), str(root)], capture_output=True, text=True)
assert result.returncode == 0 and "CHECK_RETAINED" in result.stdout, (result.stdout, result.stderr)
for variable in ("runtime_failure", "timer_failure"):
    result = subprocess.run(["env", f"{variable}=1", "bash", str(probe), str(root)], capture_output=True, text=True)
    assert result.returncode != 0, variable
for role in ("deployer", "tester"):
    isolated = root / f"etc/ci-fleet-{role}"
    isolated.mkdir(parents=True)
    result = subprocess.run(["bash", str(probe), str(root)], capture_output=True, text=True)
    assert result.returncode != 0 and f"{role} host state is present" in result.stderr, (result.stdout, result.stderr)
    isolated.rmdir()
# A restore context must match protected LKG metadata and a complete checkpoint.
selector = re.search(r"^find_bootstrap_lkg_checkpoint\(\) \{.*?^\}", (repo / "scripts/install-worker-controller.sh").read_text(), re.M | re.S).group()
checkpoints = root / "lkg-checkpoints"
checkpoint = checkpoints / "checkpoint"
checkpoint.mkdir(parents=True)
checkpoints.chmod(0o700)
checkpoint.chmod(0o700)
metadata = root / "lkg" / "metadata.json"
metadata.parent.mkdir()
identity = dict(config_repository="fixture-org/fleet-config", config_ref=legacy_ref, controller="example-ci-01")
metadata.write_text(json.dumps(identity))
metadata.chmod(0o600)
(checkpoint / "install-state.json").write_text(json.dumps(identity))
(checkpoint / "ci-fleet.env").write_text((context / "env").read_text())
(checkpoint / ".complete").touch()
for path in checkpoint.iterdir():
    path.chmod(0o600)
selector_script = root / "select-lkg.sh"
selector_script.write_text(f"set -Eeuo pipefail\nrepo_root={repo}\ncheckpoints_dir={checkpoints}\nCI_FLEET_LKG_DIR={metadata.parent}\nconfig_identity=fixture-org/fleet-config\nconfig_ref={legacy_ref}\ncontroller_id=example-ci-01\ntesting=1\n" + selector + "\nfind_bootstrap_lkg_checkpoint\n")
def select():
    return subprocess.run(["bash", str(selector_script)], capture_output=True, text=True)
result = select()
assert result.returncode == 0 and result.stdout.strip() == str(checkpoint), (result.stdout, result.stderr)
metadata.write_text(json.dumps({**identity, "controller": "another-controller"}))
assert select().returncode != 0
metadata.write_text(json.dumps(identity))
(checkpoint / ".complete").unlink()
assert select().returncode != 0
(checkpoint / ".complete").touch(mode=0o600)
(checkpoint / "ci-fleet.env").chmod(0o644)
assert select().returncode != 0
(checkpoint / "ci-fleet.env").chmod(0o600)
with (checkpoint / "ci-fleet.env").open("a") as stream:
    stream.write("CI_FLEET_RUNNER_IMAGE_DIGEST=sha256:" + "4" * 64 + "\n")
assert select().returncode != 0
PYTHON
printf 'MANAGER_STAGING_AND_IMAGE_RECOVERY_TESTS_OK\n'
