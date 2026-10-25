#!/usr/bin/env python3
"""Offline caller regressions. No Docker, services, locks or installer entrypoint."""

import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import cleanup_storage as cleanup
from desired_state import RENDERED_ENV_NAMES, DesiredStateError

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "scripts/install-worker-controller.sh").read_text()
ENGINE = "a" * 40
CONTROLLER_CONFIG, RUNNER_CONFIG, CONTROLLER_ROOT, RUNNER_ROOT = (
    "sha256:" + character * 64 for character in "3456"
)


def functions(*names):
    return "\n".join(re.search(r"^" + name + r"\(\) \{.*?^\}", SOURCE, re.M | re.S).group() for name in names)


class LifecycleTests(unittest.TestCase):
    def shell(self, names, body, *, portable=True):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = root / "releases" / ENGINE
            release.mkdir(parents=True)
            (release / ".ci-fleet-engine-ref").write_text(ENGINE + "\n")
            (release / "engine-capabilities.json").write_text(json.dumps({"schema_version": 1, "capabilities": {"portable_managed_image_ids": portable}}))
            values = dict(CI_FLEET_ENGINE_REF=ENGINE, CI_FLEET_CONTROLLER_IMAGE="controller:reviewed",
                          CI_FLEET_RUNNER_IMAGE="runner:reviewed", CI_FLEET_CONTROLLER_IMAGE_DIGEST=CONTROLLER_CONFIG,
                          CI_FLEET_RUNNER_IMAGE_DIGEST=RUNNER_CONFIG)
            if portable:
                values.update(CI_FLEET_CONTROLLER_RUNTIME_IMAGE_ID=CONTROLLER_ROOT, CI_FLEET_RUNNER_RUNTIME_IMAGE_ID=RUNNER_ROOT)
            environment = root / "env"
            environment.write_text("".join(f"{key}={value}\n" for key, value in sorted(values.items())))
            script = "set -Eeuo pipefail\n" + functions(*names) + "\n" + f"""
repo_root={shlex.quote(str(ROOT))}
temporary={shlex.quote(str(root))}
install_root=$temporary/install
releases_dir=$temporary/releases
candidate_env=$temporary/env
candidate_metadata=$temporary/metadata
release_dir=$releases_dir/{ENGINE}
engine_ref={ENGINE}
rendered_env=$temporary/installed-env
state_file=$temporary/installed-state
checkpoint_dir=$temporary/checkpoint
controller_container=controller
note() {{ :; }}
""" + body
            result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=os.environ | {"PYTHONDONTWRITEBYTECODE": "1"})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_runtime_source_selects_semantics_not_scalar_syntax(self):
        for portable in (False, True):
            self.shell(("portable_managed_images",),
                       "portable_managed_images \"$candidate_env\"" if portable else "if portable_managed_images \"$candidate_env\"; then exit 1; fi", portable=portable)

    def test_publication_checks_pair_then_tags_runtime_roots(self):
        self.shell(("portable_managed_images", "managed_image_objects_match", "restore_reviewed_image_tags"), f"""
portable_image_objects_match() {{ printf 'pair\\n' >>"$temporary/order"; [[ $pair_ok == true ]]; }}
docker() {{ [[ "$1 $2" == 'image tag' ]]; printf '%s %s\\n' "$3" "$4" >>"$temporary/order"; }}
pair_ok=false
if restore_reviewed_image_tags "$candidate_env"; then exit 1; fi
[[ $(cat "$temporary/order") == pair ]]
: >"$temporary/order"
pair_ok=true
restore_reviewed_image_tags "$candidate_env"
[[ $(cat "$temporary/order") == $'pair\\n{CONTROLLER_ROOT} controller:reviewed\\n{RUNNER_ROOT} runner:reviewed' ]]
""")

    def test_distinct_legacy_primary_and_old_enforced_ids_stay_opaque(self):
        for first, second in (("7", "8"), ("9", "b")):
            controller, runner = ("sha256:" + value * 64 for value in (first, second))
            self.shell(("portable_managed_images", "managed_image_objects_match", "restore_reviewed_image_tags"), f"""
sed -i 's/{CONTROLLER_CONFIG}/{controller}/;s/{RUNNER_CONFIG}/{runner}/' "$candidate_env"
docker() {{
  if [[ "$1 $2" == 'image inspect' ]]; then
    if [[ "$4" == *'{{{{eq '* ]]; then printf 'true\\n'; else printf '%s\\n' "${{!#}}"; fi
  else printf '%s %s\\n' "$3" "$4" >>"$temporary/tags"; fi
}}
restore_reviewed_image_tags "$candidate_env"
[[ $(cat "$temporary/tags") == $'{controller} controller:reviewed\\n{runner} runner:reviewed' ]]
""", portable=False)

    def test_checkpoint_proof_and_raw_availability_precede_any_mutation(self):
        self.shell(("portable_managed_images", "preflight_checkpoint_images"), f"""
checkpoint_images=(runner:old controller:old absent sha256:{'d' * 64})
portable_image_objects_match() {{ [[ $proof == true ]]; }}
managed_image_objects_match() {{ :; }}
checkpoint_images_reproducible() {{ printf forbidden >"$temporary/mutation"; }}
rebuild_checkpoint_images() {{ printf forbidden >"$temporary/mutation"; }}
docker() {{
  [[ "$1 ${{2:-}}" == 'image inspect' || "$1" == info ]]
  if [[ "$1" == info ]]; then return 0; fi
  if [[ "${{!#}}" == sha256:{'d' * 64} && $raw_available == false ]]; then printf 'No such image: %s\\n' "${{!#}}" >&2; return 1; fi
  printf '%s\\n' "${{!#}}"
}}
proof=false
raw_available=true
if preflight_checkpoint_images "$release_dir" "$candidate_env"; then exit 1; fi
proof=true
raw_available=false
if preflight_checkpoint_images "$release_dir" "$candidate_env"; then exit 1; fi
raw_available=true
preflight_checkpoint_images "$release_dir" "$candidate_env"
[[ ! -e "$temporary/mutation" ]]
""")

    def test_opaque_manual_capture_preflight_drain_and_restoration_use_incumbent_source(self):
        self.shell(("release_tree_digest", "runtime_release_complete", "release_tree_permissions_trusted",
                    "current_runtime_release", "runtime_release_for_environment", "capture_current_pointer", "canonical_release_target",
                    "portable_managed_images", "capture_legacy_image_ids", "load_checkpoint_images", "make_checkpoint",
                    "checkpoint_images_reproducible", "preflight_checkpoint_images", "managed_image_objects_match",
                    "try_drain_current", "retag_checkpoint_image", "restore_reviewed_image_tags",
                    "managed_images_match", "restore_checkpoint"), r"""
install_root=$temporary/opt/ci-fleet
releases_dir=$install_root/releases
mkdir -p "$releases_dir"
mv "$release_dir" "$releases_dir/$engine_ref"
release_dir=$releases_dir/$engine_ref
manager_releases=$install_root/manager/releases
manager_current=$install_root/manager/current
current_link=$install_root/current
state_root=$temporary/state
checkpoints_dir=$state_root/checkpoints
systemd_dir=$temporary/units
testing=1
root_prefix=$temporary
mode=adopt
captured_current_state=''
staging_paths=()
unit_names=()
capacity_unit_names=()
optional_unit_names=()
timer_names=()
legacy_ref=7777777777777777777777777777777777777777
legacy_source=$manager_releases/$legacy_ref
legacy_runner=sha256:1111111111111111111111111111111111111111111111111111111111111111
legacy_controller=sha256:2222222222222222222222222222222222222222222222222222222222222222
mkdir -p "$legacy_source/deploy" "$legacy_source/scripts" "$legacy_source/controller" "$legacy_source/runner" "$systemd_dir" "$release_dir/deploy"
cp "$repo_root/deploy/compose.yaml" "$release_dir/deploy/compose.yaml"
python3 - "$repo_root" "$legacy_source" "$legacy_ref" "$rendered_env" "$release_dir" <<'PREPARE_LEGACY'
import json, sys
from pathlib import Path
source, target = map(Path, sys.argv[1:3])
revision, environment = sys.argv[3], Path(sys.argv[4])
compose = (source / "deploy/compose.yaml").read_text()
compose = compose.replace("${CI_FLEET_CONTROLLER_RUNTIME_IMAGE_ID:?verified controller runtime image ID is required}", "${CI_FLEET_CONTROLLER_IMAGE:-ci-fleet-controller:dev}")
compose = compose.replace("${CI_FLEET_RUNNER_RUNTIME_IMAGE_ID:?verified runner runtime image ID is required}", "${CI_FLEET_RUNNER_IMAGE_DIGEST:?reviewed runner image ID is required}")
(target / "deploy/compose.yaml").write_text(compose)
(target / ".ci-fleet-engine-ref").write_text(revision + "\n")
(target / "engine-capabilities.json").write_text(json.dumps({"schema_version": 1, "capabilities": {"portable_managed_image_ids": False, "reproducible_managed_images": False}}))
for name in ("preflight.sh", "healthcheck.sh", "cleanup.sh"):
    path = target / "scripts" / name
    path.write_text("#!/bin/bash\nexit 0\n")
    path.chmod(0o755)
for name in ("controller/Dockerfile", "controller/go.mod", "controller/main.go", "controller/config.go", "controller/scaler.go", "controller/state.go", "runner/Dockerfile"):
    (target / name).write_text("fixture source\n")
values = dict(line.split("=", 1) for line in (source / "deploy/ci-fleet.env.example").read_text().splitlines() if line and not line.startswith("#"))
values.update(CI_FLEET_ENGINE_REF=revision, CI_FLEET_CONTROLLER_STATE="active", CI_FLEET_INSTANCE="legacy-controller",
              CI_FLEET_RUNNER_IMAGE="runner:legacy", CI_FLEET_CONTROLLER_IMAGE="controller:legacy",
              CI_FLEET_RUNNER_IMAGE_DIGEST="sha256:" + "1" * 64, CI_FLEET_CONTROLLER_IMAGE_DIGEST="sha256:" + "2" * 64)
environment.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
environment.chmod(0o600)
candidate = Path(sys.argv[5])
for relative in ("scripts/preflight.sh", "scripts/healthcheck.sh", "scripts/cleanup.sh", "scripts/managed_image_identity.py",
                 "controller/Dockerfile", "controller/go.mod", "controller/main.go", "controller/config.go",
                 "controller/scaler.go", "controller/state.go", "runner/Dockerfile"):
    path = candidate / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("fixture candidate source\n")
    if path.suffix == ".sh":
        path.chmod(0o755)
PREPARE_LEGACY
release_tree_digest "$release_dir" >"$release_dir/.ci-fleet-tree-sha256"
release_tree_digest "$legacy_source" >"$legacy_source/.ci-fleet-tree-sha256"
controller_status() { printf running; }
die() { printf '%s\n' "$*" >&2; exit 7; }
systemctl() { return 1; }
managed_runner_count() { printf 0; }
configuration_approval_required() { return 1; }
load_installed_controller_identity() { :; }
remove_inactive_managed_runners() { :; }
restore_systemd_snapshot() { :; }
run_health_check() { [[ $1 == "$releases_dir/$legacy_ref" ]]; }
on_error() { exit 99; }
runner_tag=$legacy_runner
controller_tag=$legacy_controller
missing=false
docker() {
  case "$1 ${2:-}" in
    'image inspect')
      [[ $missing != true ]] || { printf 'No such image: %s\n' "${!#}" >&2; return 1; }
      if [[ "$*" == *'{{eq '* ]]; then printf true
      elif [[ "$*" == *'org.opencontainers.image.revision'* ]]; then printf '%s' "$legacy_ref"
      elif [[ "${!#}" == runner:legacy ]]; then printf '%s' "$runner_tag"
      elif [[ "${!#}" == controller:legacy ]]; then printf '%s' "$controller_tag"
      else printf '%s' "${!#}"; fi ;;
    'image tag')
      [[ $4 != runner:legacy ]] || runner_tag=$3
      [[ $4 != controller:legacy ]] || controller_tag=$3 ;;
    'inspect --format')
      if [[ $3 == '{{.Image}}' ]]; then printf '%s' "$legacy_controller"
      elif [[ $3 == *'org.opencontainers.image.revision'* ]]; then printf '%s' "$legacy_ref"
      else printf false; fi ;;
    'info ') : ;;
    *) return 98 ;;
  esac
}
compose() {
  python3 - "$1/deploy/compose.yaml" "$2" <<'CHECK_REQUIRED' || return
from pathlib import Path
import re, sys
compose, environment = map(Path, sys.argv[1:])
values = dict(line.split("=", 1) for line in environment.read_text().splitlines())
missing = {key for key in re.findall(r"\$\{([A-Z_]+):\?", compose.read_text()) if not values.get(key)}
if missing:
    print("missing required fields: " + ", ".join(sorted(missing)), file=sys.stderr)
    raise SystemExit(47)
CHECK_REQUIRED
  printf '%s|%s\n' "$1" "${*:3}" >>"$temporary/compose-calls"
}
# Candidate Compose demonstrably cannot interpolate this complete opaque environment.
if (set -e; compose "$release_dir" "$rendered_env" config --quiet); then exit 1; fi
rm -f "$temporary/compose-calls"
make_checkpoint "$release_dir"
[[ $(cat "$checkpoint_dir/release-target") == "$legacy_source" ]] || { printf 'checkpoint selected incompatible source: %s\n' "$(cat "$checkpoint_dir/release-target")" >&2; exit 1; }
[[ ! -e "$checkpoint_dir/install-state.json" ]]
[[ ! -e "$releases_dir/$legacy_ref" ]]
load_checkpoint_images "$checkpoint_dir/ci-fleet.env" "$checkpoint_dir/image-ids.env"
preflight_checkpoint_images "$legacy_source" "$checkpoint_dir/image-env"
if preflight_checkpoint_images "$release_dir" "$checkpoint_dir/image-env"; then exit 1; fi
missing=true
if preflight_checkpoint_images "$legacy_source" "$checkpoint_dir/image-env"; then exit 1; fi
missing=false
try_drain_current false "$rendered_env" "$release_dir"
grep -Fxq "$legacy_source|pause controller" "$temporary/compose-calls"
grep -Fxq "$legacy_source|stop --timeout 300 controller" "$temporary/compose-calls"
install_release() {
  [[ $1 == "$legacy_ref" && $2 == "$releases_dir/$legacy_ref" ]]
  mkdir -p "$2"
  cp -a "$legacy_source/deploy" "$legacy_source/scripts" "$legacy_source/controller" "$legacy_source/runner" "$legacy_source/engine-capabilities.json" "$legacy_source/.ci-fleet-engine-ref" "$legacy_source/.ci-fleet-tree-sha256" "$2/"
}
restore_checkpoint
grep -Fxq "$releases_dir/$legacy_ref|up -d --remove-orphans controller" "$temporary/compose-calls"
[[ $(awk -F= '$1 == "CI_FLEET_CONTROLLER_IMAGE_DIGEST" {print $2}' "$rendered_env") == "$legacy_controller" ]]
# A candidate revision, missing source, unsafe tree, or mismatched marker grants no fallback.
rm -rf "$releases_dir/$legacy_ref"
rm "$legacy_source/.ci-fleet-engine-ref"
: >"$temporary/compose-calls"
if (make_checkpoint "$release_dir"); then exit 1; fi
if try_drain_current false "$rendered_env" "$release_dir"; then exit 1; fi
[[ ! -s "$temporary/compose-calls" ]]
printf '%s\n' "$legacy_ref" >"$legacy_source/.ci-fleet-engine-ref"
chmod 777 "$legacy_source/scripts"
if try_drain_current false "$rendered_env" "$release_dir"; then exit 1; fi
chmod 755 "$legacy_source/scripts"
printf '%s\n' "$engine_ref" >"$legacy_source/.ci-fleet-engine-ref"
if try_drain_current false "$rendered_env" "$release_dir"; then exit 1; fi
[[ ! -s "$temporary/compose-calls" ]]
""")

    def test_in_place_incumbent_source_is_preserved_before_candidate_installation(self):
        converge = functions("perform_converge")
        preservation = converge[converge.index("  capture_current_pointer\n"):converge.index("  install_release\n", converge.index("  capture_current_pointer\n"))]
        self.shell((), """
mkdir -p "$install_root"
printf '%s\n' 7777777777777777777777777777777777777777 >"$install_root/.ci-fleet-engine-ref"
current_runtime_release() { printf '%s' "$install_root"; }
capture_current_pointer() { printf 'capture\n' >>"$temporary/order"; }
install_release() {
  [[ $# == 4 && $1 == 7777777777777777777777777777777777777777 && $2 == "$releases_dir/$1" && $3 == 0 && $4 == 0 ]]
  printf 'preserve-incumbent-source\n' >>"$temporary/order"
}
""" + preservation + """
[[ $(cat "$temporary/order") == $'capture\npreserve-incumbent-source' ]]
""")

    def test_checkpoint_capture_authenticates_origin_and_refreshes_only_original_roots(self):
        self.shell(("prepare_checkpoint_image_binding",), f"""
mkdir "$checkpoint_dir"
cp "$candidate_env" "$checkpoint_dir/ci-fleet.env"
cp "$candidate_env" "$checkpoint_dir/image-env"
die() {{ exit 7; }}
portable_image_objects_match() {{ [[ $warm == true ]]; }}
python3() {{
  if [[ "$1" == - && $# == 6 ]]; then
    cat >/dev/null
    printf 'authenticate\\n' >>"$temporary/order"
    [[ $trusted == true ]] || return 1
    printf '{{}}' >"$checkpoint_dir/install-state.json"
  elif [[ "$1" == - ]]; then
    command python3 "$@"
  else
    [[ "$*" == *'--runner-reference {RUNNER_ROOT}'* && "$*" == *'--controller-reference {CONTROLLER_ROOT}'* ]]
    printf 'fresh-original-pair\\n' >>"$temporary/order"
    if [[ $change_identity == true ]]; then printf 'CHANGED=true\\n' >>"$checkpoint_dir/image-env"; fi
  fi
}}
trusted=false
warm=false
change_identity=false
if (prepare_checkpoint_image_binding "$candidate_env" "$checkpoint_dir"); then exit 1; fi
[[ $(cat "$temporary/order") == authenticate ]]
: >"$temporary/order"
trusted=true
warm=true
prepare_checkpoint_image_binding "$candidate_env" "$checkpoint_dir"
[[ $(cat "$temporary/order") == authenticate ]]
: >"$temporary/order"
warm=false
prepare_checkpoint_image_binding "$candidate_env" "$checkpoint_dir"
[[ $(cat "$temporary/order") == $'authenticate\\nfresh-original-pair' ]]
change_identity=true
if (prepare_checkpoint_image_binding "$candidate_env" "$checkpoint_dir"); then exit 1; fi
[[ ! -e "$state_file" ]]
""")

    def test_live_runtime_check_uses_root_and_preserves_exact_container_id(self):
        self.shell(("portable_managed_images", "runtime_matches"), f"""
controller_status() {{ printf running; }}
controller_environment_matches() {{ :; }}
docker() {{
  if [[ "$1 $2" == 'image inspect' ]]; then
    [[ "${{!#}}" == {CONTROLLER_ROOT} ]] || return 1
    printf '%s\\n' {CONTROLLER_ROOT}
  elif [[ "$3" == '{{{{.Image}}}}' ]]; then printf '%s\\n' "$live";
  else printf '%s\\n' {ENGINE}; fi
}}
live={CONTROLLER_CONFIG}
if runtime_matches active; then exit 1; fi
live={CONTROLLER_ROOT}
runtime_matches active
""")

    def test_preparation_finishes_before_compose_and_never_publishes_tags(self):
        self.shell(("portable_managed_images", "build_candidate"), """
prepare_portable_images() { printf 'prepare\n' >>"$temporary/order"; return 1; }
build_managed_images() { [[ $3 == true ]]; printf 'build-pair\n' >>"$temporary/order"; }
portable_image_objects_match() { printf 'warm-pair\n' >>"$temporary/order"; }
compose() { printf 'compose\n' >>"$temporary/order"; }
docker() { exit 1; }
build_candidate
[[ $(cat "$temporary/order") == $'prepare\nbuild-pair\nwarm-pair\ncompose' ]]
""")

    def test_compose_failure_respects_source_specific_preparation_order(self):
        fixture = (ROOT / "scripts/test-install-worker-controller.sh").read_text()
        fake_docker = re.search(r'^cat >"\$fake_bin/docker" <<\'EOF\'\n(.*?)^EOF$', fixture, re.M | re.S).group(1)
        converge = functions("perform_converge")
        preparation = converge[converge.index("  capture_current_pointer\n"):converge.index("  build_before_drain=true\n")]
        for portable in (False, True):
            with self.subTest(portable=portable):
                self.shell(("compose", "portable_managed_images", "build_candidate"), """
mkdir "$temporary/bin"
cat >"$temporary/bin/docker" <<'FIXTURE_DOCKER'
""" + fake_docker + """FIXTURE_DOCKER
chmod 700 "$temporary/bin/docker"
PATH=$temporary/bin:$PATH
[[ $(command -v docker) == "$temporary/bin/docker" ]]
testing=1
root_prefix=$temporary
export FAKE_DOCKER_STATE=$temporary/incumbent
export FAKE_COMPOSE_LOG=$temporary/compose.log
export FAKE_FAIL_CONFIG=1
: >"$FAKE_DOCKER_STATE"
capture_current_pointer() { :; }
current_runtime_release() { :; }
install_release() { :; }
pending_policy_checkpoint() { return 1; }
make_checkpoint() { printf 'checkpoint\n' >>"$temporary/order"; printf 'CHECKPOINT_CREATED\n'; }
repair_pending_checkpoint_authority() { exit 98; }
managed_images_match() { return 0; }
managed_image_objects_match() { return 0; }
build_managed_images() { exit 98; }
prepare_portable_images() { printf 'prove-pair\n' >>"$temporary/order"; }
portable_image_objects_match() { printf 'check-pair\n' >>"$temporary/order"; }
die() { printf '%s\n' "$*" >&2; exit 97; }
set +e
(
  set -Eeuo pipefail
""" + preparation + """
) >"$temporary/output" 2>&1
status=$?
set -e
[[ $status == 47 ]]
[[ -f "$FAKE_DOCKER_STATE" ]]
[[ $(wc -l <"$FAKE_COMPOSE_LOG") == 1 ]]
grep -q '^config|' "$FAKE_COMPOSE_LOG"
if grep -Eq 'DRAIN_READY|ROLLBACK_' "$temporary/output"; then exit 1; fi
""" + ("""
[[ $(cat "$temporary/order") == $'checkpoint\nprove-pair\ncheck-pair' ]]
""" if portable else """
[[ ! -e "$temporary/order" ]]
if grep -q CHECKPOINT_CREATED "$temporary/output"; then exit 1; fi
"""), portable=portable)

    def test_hydration_precedes_exact_drift_comparisons(self):
        self.shell(("drift_count",), """
testing=0
target_state=active
hydrate_candidate_images() { printf 'hydrated\n' >"$candidate_env"; cp "$candidate_env" "$rendered_env"; chmod 600 "$rendered_env"; }
release_matches() { :; }
state_matches() { [[ $(cat "$candidate_env") == hydrated ]]; }
runtime_matches() { :; }
managed_image_ids_configured() { return 1; }
systemd_matches() { :; }
docker_network_policy_matches() { :; }
drift_count
[[ $DRIFT_COUNT == 0 ]]
""")

    def test_runtime_fields_are_rendered_only(self):
        for role in ("CONTROLLER", "RUNNER"):
            name = f"CI_FLEET_{role}_RUNTIME_IMAGE_ID"
            self.assertIn(name, RENDERED_ENV_NAMES)
            # Host validation rejects this field through parse_env's allowlist.
            from desired_state import parse_env
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "host"
                path.write_text(f"{name}={CONTROLLER_ROOT}\n")
                with self.assertRaises(DesiredStateError):
                    parse_env(path, allow_unknown=False)

    def test_pilot_commands_use_managed_identity_and_protected_environment(self):
        pilot = (ROOT / "docs/LIVE-PILOT.md").read_text()
        instance = re.search(r"^CI_FLEET_INSTANCE=(.+)$", pilot, re.M).group(1)
        commands = [shlex.split(line) for block in re.findall(r"```bash\n(.*?)```", pilot.split("## 6.", 1)[1], re.S)
                    for line in block.replace("\\\n", " ").splitlines() if line.strip()]
        manager = "/opt/ci-fleet/manager/current/scripts/install-worker-controller.sh"
        self.assertIn(["sudo", manager, "--uninstall"], commands)
        install = next(command for command in commands if "--install" in command)
        self.assertIn(["sudo", manager, "--check", *install[3:]], commands)
        for mode in ("--install", "--upgrade"):
            command = next(command for command in commands if mode in command)
            self.assertEqual(command[0], "sudo")
            self.assertEqual(command[command.index("--controller") + 1], instance)
        self.assertEqual(next(command for command in commands if "--upgrade" in command)[1], manager)
        cleanups = [command for command in commands if any(token.endswith("/scripts/cleanup.sh") for token in command)]
        self.assertEqual(len(cleanups), 1)
        cleanup = cleanups[0]
        self.assertEqual(cleanup[:2], ["sudo", "systemd-run"])
        self.assertIn("--property=User=root", cleanup)
        self.assertIn("--property=EnvironmentFile=/etc/ci-fleet/ci-fleet.env", cleanup)
        self.assertIn("--property=WorkingDirectory=/opt/ci-fleet/current", cleanup)
        self.assertEqual(cleanup[-3:], ["/opt/ci-fleet/current/scripts/cleanup.sh", "--instance", instance])
        self.assertNotIn("--apply", cleanup)
        health = [command for command in commands if any(token.endswith("/scripts/healthcheck.sh") for token in command)]
        self.assertTrue(health)
        self.assertTrue(all(command == ["sudo", "/opt/ci-fleet/manager/current/scripts/healthcheck.sh", "--json"] for command in health))
        for command in commands:
            self.assertEqual(command[0], "sudo")
            self.assertNotIn("compose", command)
            for token in command:
                self.assertNotIn("$", token)
                if token.startswith("/opt/ci-fleet/") and "/scripts/" in token:
                    self.assertTrue((ROOT / "scripts" / token.rsplit("/scripts/", 1)[1]).is_file(), token)

    def test_portable_retained_objects_compare_without_cmp(self):
        self.shell(("image_metadata_path", "portable_image_objects_match"), """
printf '{}' >"$candidate_metadata"
prepare_portable_images() {
  [[ $proof == true ]] || return 1
  if [[ $change == true ]]; then printf 'changed\n' >>"$1"; fi
}
mkdir "$temporary/no-cmp"
for tool in awk mktemp install dirname python3; do ln -s "$(command -v "$tool")" "$temporary/no-cmp/$tool"; done
PATH=$temporary/no-cmp
if command -v cmp >/dev/null; then exit 1; fi
proof=true
change=false
portable_image_objects_match "$candidate_env"
change=true
if portable_image_objects_match "$candidate_env"; then exit 1; fi
proof=false
if portable_image_objects_match "$candidate_env"; then exit 1; fi
""")

    def test_lkg_fetch_retains_parent_for_portable_and_opaque_image_policy(self):
        remote = (ROOT / "scripts/remote-reconcile.sh").read_text()
        apply = re.search(r"^apply_lkg\(\) \{.*?^\}", remote, re.M | re.S).group()
        for portable in (False, True):
            with self.subTest(portable=portable), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                repository = root / "configuration"
                repository.mkdir()
                def git(*arguments):
                    return subprocess.check_output(["git", "-C", str(repository), *arguments], text=True).strip()
                value = json.loads((ROOT / "templates/config-repository/fleet.json").read_text())
                value["organization"].update(slug="sample-org", registry="ghcr.io/sample-org")
                value["runner_pools"]["trusted-ci"]["allowed_repositories"] = ["sample-org/sample-app"]
                value["projects"]["example-app"].update(repository="sample-org/sample-app", image="ghcr.io/sample-org/sample-app")
                controller = value["controllers"]["example-ci-01"]
                controller.pop("docker_network_policy", None)
                controller.pop("status_reporting", None)
                engine = controller["engine_ref"]
                value["managed_images"] = {engine: {"amd64": {"controller": CONTROLLER_CONFIG, "runner": RUNNER_CONFIG}}}
                evidence = {"schema_version": 1, "status_reporting_engine_capabilities": {"example-ci-01": {
                    "engine_ref": engine, "status_reporting_config": False, "required_status_reporting": False,
                    "portable_managed_image_ids": portable}}}
                git("init", "-q")
                for index in range(2):
                    (repository / "fleet.json").write_text(json.dumps(value))
                    (repository / "engine-rollout-evidence.json").write_text(json.dumps(evidence))
                    (repository / "revision").write_text(str(index))
                    git("add", ".")
                    git("-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")
                ref = git("rev-parse", "HEAD")
                installer = root / "validate-retained.sh"
                installer.write_text("#!/bin/bash\nset -Eeuo pipefail\n" + functions("validate_candidate_config_commit") + f"""
repo_root={shlex.quote(str(ROOT))}
temporary={shlex.quote(str(root))}
state_file=$temporary/absent-state
rendered_env=$temporary/absent-env
mode=upgrade
restore_lkg=true
die() {{ printf '%s\\n' "$*" >&2; exit 7; }}
while (($#)); do
  case "$1" in
    --config-repo) config_source_checkout=$2; shift 2 ;;
    --ref) config_ref=$2; shift 2 ;;
    --config-identity|--controller) shift 2 ;;
    *) shift ;;
  esac
done
candidate_config=$config_source_checkout/fleet.json
validate_candidate_config_commit
printf '{{"schema_version":1,"outcome":"applied"}}\\n' >&7
""")
                installer.chmod(0o700)
                for token in (False, True):
                    for depth in (1, 2):
                        temporary = root / f"attempt-{token}-{depth}"
                        temporary.mkdir()
                        (temporary / "metadata.json").write_text(json.dumps({"config_ref": ref,
                            "config_repository": "sample-org/fleet-config", "controller": "example-ci-01"}))
                        if token:
                            (temporary / "reconcile-token").write_text("fixture-only")
                        function = apply.replace("--depth=2", f"--depth={depth}")
                        script = "set -Eeuo pipefail\n" + function + f"""
no_op=false
explicit_ref=''
lkg_dir={shlex.quote(str(temporary))}
temp_dir=$lkg_dir
transaction_result=$temp_dir/transaction-result
installer={shlex.quote(str(installer))}
note() {{ :; }}
log_json() {{ printf '%s\\n' "$*" >&2; }}
transaction_outcome() {{ printf applied; }}
git() {{
  if [[ "${{1:-}}" == -C && "${{3:-}}" == fetch ]]; then
    local target=$2 option depth
    for option in "$@"; do [[ "$option" != --depth=* ]] || depth=$option; done
    command git -C "$target" fetch -q "$depth" {shlex.quote(repository.as_uri())} {ref}
  else command git "$@"; fi
}}
apply_lkg
"""
                        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                                                env=os.environ | {"PYTHONDONTWRITEBYTECODE": "1"})
                        self.assertEqual(result.returncode == 0, depth == 2, result.stderr)
                        if depth == 1:
                            self.assertIn("previous integrated configuration history is unavailable", result.stderr)

    def test_callers_pass_both_pending_records_and_refuse_missing_parent_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "configuration"
            repository.mkdir()
            def git(*arguments):
                return subprocess.check_output(["git", "-C", str(repository), *arguments], text=True).strip()
            git("init", "-q")
            for index in (1, 2):
                for name in ("fleet.json", "engine-rollout-evidence.json", "next-engine-rollout-evidence.json"):
                    (repository / name).write_text(json.dumps({"fixture": index}))
                git("add", ".")
                git("-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", f"fixture {index}")
            current, parent = git("rev-parse", "HEAD"), git("rev-parse", "HEAD^")
            common = f"""
repo_root={shlex.quote(str(ROOT))}
temporary={shlex.quote(str(root))}
temp_dir=$temporary
config_source_checkout={shlex.quote(str(repository))}
config_ref={current}
candidate_config=$config_source_checkout/fleet.json
state_file=$temporary/no-state
rendered_env=$temporary/no-env
mode=upgrade
restore_lkg=false
python3() {{ printf '%s\\n' "$*" >>"$temporary/calls"; }}
die() {{ printf '%s\\n' "$*" >&2; exit 7; }}
log_json() {{ :; }}
"""
            remote = (ROOT / "scripts/remote-reconcile.sh").read_text()
            for function, call in ((functions("validate_candidate_config_commit"), "validate_candidate_config_commit"),
                                   (re.search(r"^validate_config\(\) \{.*?^\}", remote, re.M | re.S).group(),
                                    'validate_config "$config_source_checkout" "$config_ref"')):
                script = "set -Eeuo pipefail\n" + common + function + "\n" + call
                result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                calls = (root / "calls").read_text()
                self.assertIn("--previous-config", calls)
                self.assertIn("--next-engine-rollout-evidence", calls)
                self.assertIn("--previous-next-engine-rollout-evidence", calls)
                (root / "calls").unlink()
            # Remove only the temporary fixture's loose parent object.
            (repository / ".git/objects" / parent[:2] / parent[2:]).unlink()
            script = "set -Eeuo pipefail\n" + common + functions("validate_candidate_config_commit") + "\nvalidate_candidate_config_commit"
            result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
            self.assertEqual(result.returncode, 7, result.stderr)
            self.assertIn("history is unavailable", result.stderr)

    def test_cross_ref_fixture_activates_the_captured_runtime_before_relabeling_sources(self):
        fixture = (ROOT / "scripts/test-install-worker-controller.sh").read_text()
        activate = re.search(r"^activate_cross_ref_incumbent\(\) \{.*?^\}", fixture, re.M | re.S).group()
        write = re.search(r"^write_config\(\) \{.*?^\}", fixture, re.M | re.S).group()
        fake_docker = re.search(r'^cat >"\$fake_bin/docker" <<\'EOF\'\n(.*?)^EOF$', fixture, re.M | re.S).group(1)
        self.assertEqual(len(re.findall(r"^activate_cross_ref_incumbent$", fixture, re.M)), 2)
        self.shell(("current_runtime_release", "runtime_release_for_environment", "capture_current_pointer",
                    "capture_legacy_image_ids", "load_checkpoint_images", "make_checkpoint",
                    "portable_managed_images", "validate_candidate_config_commit"), write + "\n" + activate + r"""
root=$temporary/host
tmp=$temporary
install_root=$root/opt/ci-fleet
releases_dir=$install_root/releases
manager_releases=$install_root/manager/releases
current_link=$install_root/current
manager_current=$install_root/manager/current
state_root=$root/var/lib/ci-fleet
state_file=$state_root/install-state.json
rendered_env=$root/etc/ci-fleet/ci-fleet.env
systemd_dir=$temporary/units
config_repo=$temporary/config
config_source_checkout=$config_repo
candidate_config=$config_repo/fleet.json
cross_ref_old=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
cross_ref_runtime=$releases_dir/$cross_ref_old
release_dir=$releases_dir/$engine_ref
mkdir -p "$state_root" "$(dirname "$rendered_env")" "$systemd_dir" "$config_repo" "$temporary/bin"
git -C "$config_repo" init -q
git -C "$config_repo" config user.name fixture
git -C "$config_repo" config user.email fixture@example.invalid
for revision in "$engine_ref" "$cross_ref_old"; do
  mkdir -p "$releases_dir/$revision/deploy" "$manager_releases/$revision"
  printf '%s\n' "$revision" >"$releases_dir/$revision/.ci-fleet-engine-ref"
  printf '%s\n' "$revision" >"$manager_releases/$revision/.ci-fleet-engine-ref"
  printf '{}' >"$releases_dir/$revision/deploy/compose.yaml"
done
export FAKE_DOCKER_STATE=$temporary/controller
export FAKE_ENGINE_REF=$engine_ref
export FAKE_RUNNER_IMAGE=ci-fleet-runner:${engine_ref:0:12}
export FAKE_CONTROLLER_IMAGE=ci-fleet-controller:${engine_ref:0:12}
export FAKE_RUNNER_IMAGE_STATE=$temporary/runner-state
export FAKE_CONTROLLER_IMAGE_STATE=$temporary/controller-state
export FAKE_RUNNER_IMAGE_ID_STATE=$temporary/runner-id
export FAKE_CONTROLLER_IMAGE_ID_STATE=$temporary/controller-id
export FAKE_AVAILABLE_IMAGE_IDS=$temporary/available
export FAKE_CONTROLLER_PROVENANCE_FILE=$temporary/live-revision
export FAKE_CONTROLLER_IMAGE_ID_FILE=$temporary/live-image
export FAKE_CONTROLLER_ENV_FILE=$temporary/live-env
export TEST_SOURCE_ROOT=$repo_root TEST_HOST_ROOT=$root
cat >"$temporary/bin/docker" <<'FAKE_DOCKER'
""" + fake_docker + r"""FAKE_DOCKER
chmod 700 "$temporary/bin/docker"
PATH=$temporary/bin:$PATH
[[ $(command -v docker) == "$temporary/bin/docker" ]]
installer=$temporary/model-runtime
cat >"$installer" <<'MODEL_RUNTIME'
#!/usr/bin/python3
import json, os, sys
from pathlib import Path
sys.path.insert(0, os.environ["TEST_SOURCE_ROOT"] + "/scripts")
from desired_state import build_rendered_env
args = sys.argv[1:]
config = json.loads((Path(args[args.index("--config-repo") + 1]) / "fleet.json").read_text())
name = args[args.index("--controller") + 1]
selected = config["controllers"][name]["engine_ref"]
assert os.environ["FAKE_ENGINE_REF"] == selected
host = Path(os.environ["TEST_HOST_ROOT"])
environment = host / "etc/ci-fleet/ci-fleet.env"
state = host / "var/lib/ci-fleet/install-state.json"
if args[0] == "--upgrade":
    values, metadata = build_rendered_env(config, name, {
        "CI_FLEET_GITHUB_APP_CLIENT_ID": "fixture", "CI_FLEET_GITHUB_APP_INSTALLATION_ID": "123",
        "CI_FLEET_GITHUB_APP_PRIVATE_KEY_FILE": "/fixture-only/key.pem"},
        config_repository="sample-org/fleet-config", config_ref=args[args.index("--ref") + 1], docker_gid=998,
        machine="x86_64", engine_capabilities={"status_reporting_config", "required_status_reporting", "docker_network_policy_config", "docker_network_policy_adapter"})
    environment.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
    environment.chmod(0o600)
    state.write_text(json.dumps(metadata))
    state.chmod(0o600)
    for role in ("RUNNER", "CONTROLLER"):
        Path(os.environ[f"FAKE_{role}_IMAGE_STATE"]).write_text(selected + "\n")
        Path(os.environ[f"FAKE_{role}_IMAGE_ID_STATE"]).write_text(values[f"CI_FLEET_{role}_IMAGE_DIGEST"] + "\n")
    Path(os.environ["FAKE_CONTROLLER_PROVENANCE_FILE"]).write_text(selected + "\n")
    Path(os.environ["FAKE_CONTROLLER_IMAGE_ID_FILE"]).write_text(values["CI_FLEET_CONTROLLER_IMAGE_DIGEST"] + "\n")
    Path(os.environ["FAKE_DOCKER_STATE"]).touch()
    for kind in ("releases", "manager/releases"):
        link = host / "opt/ci-fleet" / ("current" if kind == "releases" else "manager/current")
        link.unlink(missing_ok=True)
        link.symlink_to(host / "opt/ci-fleet" / kind / selected)
else:
    assert args[0] == "--check"
    assert f"CI_FLEET_ENGINE_REF={selected}\n" in environment.read_text()
    assert json.loads(state.read_text())["engine_ref"] == selected
    assert Path(os.environ["FAKE_CONTROLLER_PROVENANCE_FILE"]).read_text().strip() == selected
with (host / "calls").open("a") as output:
    output.write(args[0] + " " + selected + "\n")
MODEL_RUNTIME
chmod 700 "$installer"
base_args=(--config-repo "$config_repo" --controller example-ci-01)
expect_success() { "$@"; }
# Authentication and runtime effects are modeled here; R8 tests cover the real guards.
runtime_release_complete() { [[ -f $1/.ci-fleet-engine-ref && $(<"$1/.ci-fleet-engine-ref") == "$2" ]]; }
manager_release_complete() { runtime_release_complete "$@"; }
release_tree_permissions_trusted() { return 0; }
controller_status() { printf running; }
systemctl() { return 1; }
die() { printf '%s\n' "$*" >&2; exit 7; }
mode=upgrade
restore_lkg=false
testing=1
staging_paths=()
unit_names=()
capacity_unit_names=()
optional_unit_names=()
timer_names=()
original_ref=$(write_config active 1 1)
"$installer" --upgrade "${base_args[@]}" --ref "$original_ref"
for maximum in 2 3; do
  activate_cross_ref_incumbent
  config_ref=$(git -C "$config_repo" rev-parse HEAD)
  validate_candidate_config_commit
  grep -Fxq "CI_FLEET_ENGINE_REF=$cross_ref_old" "$rendered_env"
  [[ $(<"$FAKE_CONTROLLER_PROVENANCE_FILE") == "$cross_ref_old" ]]
  config_ref=$(write_config active "$maximum" "$maximum")
  validate_candidate_config_commit
  # This is the real fixture's pointer restaging, after its incumbent activation.
  ln -sfn "$cross_ref_runtime" "$current_link"
  ln -sfn "$manager_releases/$cross_ref_old" "$manager_current"
  captured_current_state=''
  rm -f "$temporary/current-link"  # Each real installer call has a fresh scratch directory.
  checkpoints_dir=$state_root/checkpoints-$maximum
  make_checkpoint "$release_dir"
  grep -Fxq "$cross_ref_runtime" "$checkpoint_dir/release-target"
  grep -Fxq "$manager_releases/$cross_ref_old" "$checkpoint_dir/manager-target"
  grep -Fxq "CI_FLEET_ENGINE_REF=$cross_ref_old" "$checkpoint_dir/ci-fleet.env"
  [[ -f "$checkpoint_dir/.complete" ]]
  # Return through the modeled activation before discarding any old source context.
  "$installer" --upgrade "${base_args[@]}" --ref "$config_ref"
  unset FAKE_PRIOR_RUNNER_IMAGE FAKE_PRIOR_CONTROLLER_IMAGE FAKE_PRIOR_IMAGE_STATES
done
[[ $(grep -Fc -- "--check $cross_ref_old" "$root/calls") == 2 ]]
""", portable=False)

    def test_pending_checkpoint_fixture_rearms_restage_before_fsync_failure(self):
        fixture = (ROOT / "scripts/test-install-worker-controller.sh").read_text()
        expect_failure = re.search(r"^expect_failure\(\) \{.*?^\}", fixture, re.M | re.S).group()
        alias_check = fixture[fixture.index('ln -s releases "$root/opt/ci-fleet/release-aliases"'):
                              fixture.index('rm "$root/opt/ci-fleet/release-aliases"')]
        fsync_check = fixture[fixture.index('rm "$root/opt/ci-fleet/release-aliases"'):
                              fixture.index('retry_pause=$tmp/interrupted-retry-pause')]
        fake_python = re.search(r'^cat >"\$fake_bin/python3" <<\'EOF\'\n(.*?)^EOF$', fixture, re.M | re.S).group(1)
        self.shell(("repair_pending_checkpoint_authority", "release_tree_permissions_trusted",
                    "raw_link_target_path", "resolve_link_target", "canonical_release_target",
                    "atomic_replace_directory", "release_tree_digest"), expect_failure + r"""
root=$temporary/host
root_prefix=$root
tmp=$temporary
testing=1
releases_dir=$root/opt/ci-fleet/releases
manager_releases=$root/opt/ci-fleet/manager/releases
current_link=$root/opt/ci-fleet/current
manager_current=$root/opt/ci-fleet/manager/current
cross_ref_old=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
stale_checkpoint_release=$releases_dir/$cross_ref_old
stale_checkpoint_manager=$manager_releases/$cross_ref_old
interrupted_checkpoint=$temporary/checkpoint
mkdir -p "$stale_checkpoint_release" "$stale_checkpoint_manager" "$releases_dir/$engine_ref" "$manager_releases/$engine_ref" "$interrupted_checkpoint" "$temporary/archive/scripts" "$temporary/bin"
printf '%s\n' "$cross_ref_old" >"$temporary/archive/.ci-fleet-engine-ref"
printf 'fixture adapter\n' >"$temporary/archive/scripts/docker-network-policy-adapter.sh"
for target in "$stale_checkpoint_release" "$stale_checkpoint_manager"; do
  cp -a "$temporary/archive/." "$target"
  chmod g+w "$target/scripts" "$target/scripts/docker-network-policy-adapter.sh"
done
printf '%s\n' "$stale_checkpoint_release" >"$interrupted_checkpoint/release-target"
printf '%s\n' "$stale_checkpoint_manager" >"$interrupted_checkpoint/manager-target"
: >"$interrupted_checkpoint/current-link"
chmod 600 "$interrupted_checkpoint/"*-target "$interrupted_checkpoint/current-link"
printf '%s\n' "$engine_ref" >"$releases_dir/$engine_ref/.ci-fleet-engine-ref"
printf '%s\n' "$engine_ref" >"$manager_releases/$engine_ref/.ci-fleet-engine-ref"
ln -s "$releases_dir/$engine_ref" "$current_link"
ln -s "$manager_releases/$engine_ref" "$manager_current"
: >"$temporary/restages"
fail() { printf 'FAIL %s\n' "$*" >&2; exit 1; }
# Completeness and immutable input retrieval are modeled; repair, permissions,
# directory exchange and the fixture's fsync injection run unchanged.
runtime_release_complete() { [[ -f $1/.ci-fleet-engine-ref && $(<"$1/.ci-fleet-engine-ref") == "$2" ]]; }
manager_release_complete() { runtime_release_complete "$@"; }
install_release() {
  local ref=$1 target=$2 stage
  [[ $ref == "$cross_ref_old" && $target == "$stale_checkpoint_release" ]] || return 1
  release_tree_permissions_trusted "$target" && return 0
  stage=$(mktemp -d "$releases_dir/.restage.XXXXXX")
  cp -a "$temporary/archive/." "$stage"
  printf 'runtime\n' >>"$temporary/restages"
  atomic_replace_directory "$stage" "$target"
}
install_manager() {
  local ref=$1 source=$2 target=$3 stage
  [[ $ref == "$cross_ref_old" && $source == "$stale_checkpoint_release" && $target == "$stale_checkpoint_manager" ]] || return 1
  stage=$(mktemp -d "$manager_releases/.restage.XXXXXX")
  cp -a "$source/." "$stage"
  printf 'manager\n' >>"$temporary/restages"
  atomic_replace_directory "$stage" "$target"
}
refresh_release_digest() { release_tree_digest "$1" >"$1/.ci-fleet-tree-sha256"; }
retry() {
  repair_pending_checkpoint_authority "$interrupted_checkpoint" || {
    printf 'pending network-policy controller checkpoint is invalid\n' >&2
    return 2
  }
}
installer=retry
base_args=()
interrupted_ref=fixture-config
export REAL_PYTHON3
REAL_PYTHON3=$(command -v python3)
cat >"$temporary/bin/python3" <<'FIXTURE_PYTHON'
""" + fake_python + r"""FIXTURE_PYTHON
chmod 700 "$temporary/bin/python3"
PATH=$temporary/bin:$PATH
[[ $(command -v python3) == "$temporary/bin/python3" ]]
""" + alias_check + r"""
[[ $(cat "$temporary/restages") == $'runtime\nmanager' ]]
release_tree_permissions_trusted "$stale_checkpoint_release"
release_tree_permissions_trusted "$stale_checkpoint_manager"
printf 'alias rejection repaired runtime and manager\n'
daemon_config=$temporary/daemon.json
policy_marker=$temporary/policy.json
printf '{"fixture":"daemon"}\n' >"$daemon_config"
printf '{"fixture":"pending"}\n' >"$policy_marker"
""" + fsync_check + r"""
[[ ! -e "$tmp/pending-fsync-failure" ]]
[[ $(cat "$temporary/restages") == $'runtime\nmanager\nruntime' ]]
[[ $(<"$interrupted_checkpoint/current-link") == '../../unavailable-relative-release' ]]
if release_tree_permissions_trusted "$stale_checkpoint_manager"; then exit 1; fi
retry
[[ $(<"$interrupted_checkpoint/current-link") == "$stale_checkpoint_release" ]]
[[ $(stat -c %a "$interrupted_checkpoint/current-link") == 600 ]]
release_tree_permissions_trusted "$stale_checkpoint_release"
release_tree_permissions_trusted "$stale_checkpoint_manager"
[[ $(readlink "$current_link") == "$releases_dir/$engine_ref" ]]
[[ $(readlink "$manager_current") == "$manager_releases/$engine_ref" ]]
[[ -z $(find "$interrupted_checkpoint" -name '.current-link.*' -print -quit) ]]
""")

    def test_fixture_expected_failure_diagnostic_preserves_output_and_call_site(self):
        fixture = (ROOT / "scripts/test-install-worker-controller.sh").read_text()
        expect_failure = re.search(r"^expect_failure\(\) \{.*?^\}", fixture, re.M | re.S).group()
        self.shell((), expect_failure + r"""
fail() { printf 'FAIL %s\n' "$*" >&2; exit 1; }
refuse() { printf 'fixture refusal\n'; return 2; }
expect_failure 'fixture refusal' refuse
set +e
expected_line=$((LINENO + 1))
(expect_failure 'fixture refusal' printf 'fixture unexpected success\n') >"$temporary/diagnostic" 2>&1
status=$?
set -e
[[ $status == 1 ]]
grep -Fq '[fixture refusal]' "$temporary/diagnostic"
grep -Fq "line $expected_line" "$temporary/diagnostic"
grep -Fq 'fixture unexpected success' "$temporary/diagnostic"
set +e
(expect_failure 'different refusal' refuse) >"$temporary/missing" 2>&1
status=$?
set -e
[[ $status == 1 ]]
grep -Fq 'missing failure [different refusal]: fixture refusal' "$temporary/missing"
""")

    def test_installer_fixture_stages_runtime_evidence_before_policy_reintroduction(self):
        fixture = (ROOT / "scripts/test-install-worker-controller.sh").read_text()
        helper = re.search(r"^write_config\(\) \{.*?^\}", fixture, re.M | re.S).group()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "configuration"
            def git(*arguments):
                return subprocess.check_output(["git", "-C", str(repository), *arguments], text=True).strip()
            repository.mkdir()
            git("init", "-q")
            git("config", "user.name", "fixture")
            git("config", "user.email", "fixture@example.invalid")
            # Model an existing verified runtime, then a verified no-policy engine upgrade.
            script = f"set -Eeuo pipefail\nrepo_root={shlex.quote(str(ROOT))}\nconfig_repo={shlex.quote(str(repository))}\nengine_ref={ENGINE}\n" + helper + f"""
write_config active 1 1
write_config active 1 1 "$engine_ref" false omit
write_config active 1 1
write_config active 1 1 {'b' * 40} false omit
write_config active 1 1 {'b' * 40}
"""
            result = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                                    env=os.environ | {"PYTHONDONTWRITEBYTECODE": "1"})
            self.assertEqual(result.returncode, 0, result.stderr)
            refs = result.stdout.splitlines()
            self.assertEqual(len(refs), 5)
            for before, selected in ((refs[1], refs[2]), (refs[3], refs[4])):
                staged = git("rev-parse", selected + "^")
                self.assertNotEqual(staged, before)
                self.assertEqual(git("diff", "--name-only", before, staged), "engine-rollout-evidence.json")
                self.assertEqual(git("rev-parse", before + ":fleet.json"), git("rev-parse", staged + ":fleet.json"))

            def read(ref, name):
                result = subprocess.run(["git", "-C", str(repository), "show", ref + ":" + name], capture_output=True, text=True)
                return json.loads(result.stdout) if result.returncode == 0 else None

            def validate(previous, current, prior_evidence, evidence, prior_pending=None, pending=None):
                command = ["python3", str(ROOT / "templates/config-repository/scripts/validate.py"), "--strict", "--skip-path-scan"]
                for option, value in (("previous-config", previous), ("config", current),
                                      ("previous-rollout-evidence", prior_evidence), ("rollout-evidence", evidence),
                                      ("previous-next-engine-rollout-evidence", prior_pending), ("next-engine-rollout-evidence", pending)):
                    if value is not None:
                        path = root / option
                        path.write_text(json.dumps(value))
                        command.extend(("--" + option, str(path)))
                return subprocess.run(command, capture_output=True, text=True,
                                      env=os.environ | {"PYTHONDONTWRITEBYTECODE": "1"})

            for ref in git("rev-list", "--reverse", refs[0] + ".." + refs[-1]).splitlines():
                before = git("rev-parse", ref + "^")
                result = validate(read(before, "fleet.json"), read(ref, "fleet.json"),
                                  read(before, "engine-rollout-evidence.json"), read(ref, "engine-rollout-evidence.json"),
                                  read(before, "next-engine-rollout-evidence.json"), read(ref, "next-engine-rollout-evidence.json"))
                self.assertEqual(result.returncode, 0, result.stderr)
            omitted, policy = read(refs[1], "fleet.json"), read(refs[2], "fleet.json")
            unproven, proven = read(refs[1], "engine-rollout-evidence.json"), read(refs[2], "engine-rollout-evidence.json")
            wrong = json.loads(json.dumps(proven))
            wrong["status_reporting_engine_capabilities"]["example-ci-01"]["engine_ref"] = "b" * 40
            for previous_proof, current_proof in ((unproven, proven), (None, proven), (wrong, proven),
                                                  (proven, unproven), (proven, wrong)):
                result = validate(omitted, policy, previous_proof, current_proof)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("capability evidence", result.stderr)

    def test_installed_fixture_fetches_synthetic_engines_without_network(self):
        fixture = (ROOT / "scripts/test-install-worker-controller.sh").read_text()
        wrapper = re.search(r"""cat >"\$fake_bin/git" <<'EOF'\n(.*?)\nEOF""", fixture, re.S).group(1)
        real_git = shutil.which("git")
        self.assertIsNotNone(real_git)
        canonical = "https://github.com/RandomDevelopment/ci-fleet.git"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "engine source with spaces"
            source.mkdir()
            environment = os.environ | {"REAL_GIT": real_git, "FAKE_ENGINE_GIT_SOURCE": str(source),
                "GIT_ALLOW_PROTOCOL": "file", "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1", "GIT_NO_REPLACE_OBJECTS": "1",
                "GIT_OPTIONAL_LOCKS": "0", "PYTHONDONTWRITEBYTECODE": "1"}

            def git(repository, *arguments, executable=real_git, **updates):
                return subprocess.run([str(executable), "-C", str(repository), *arguments],
                                      capture_output=True, text=True, cwd=root, env=environment | updates)

            for arguments in (("init", "-q"), ("config", "user.name", "fixture"),
                              ("config", "user.email", "fixture@example.invalid")):
                result = git(source, *arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
            marker = source / "engine"
            for value in ("prior", "synthetic unpublished engine"):
                marker.write_text(value)
                self.assertEqual(git(source, "add", "engine").returncode, 0)
                self.assertEqual(git(source, "commit", "-qm", "test: " + value).returncode, 0)
            selected = git(source, "rev-parse", "HEAD").stdout.strip()
            wrapper_path = root / "git"
            wrapper_path.write_text(wrapper + "\n")
            wrapper_path.chmod(0o700)
            for depth in (1, 2):
                checkout = root / ("fetch-" + str(depth))
                checkout.mkdir()
                self.assertEqual(git(checkout, "init", "-q").returncode, 0)
                self.assertEqual(git(checkout, "remote", "add", "origin", canonical).returncode, 0)
                arguments = ("fetch", "-q", "--depth=" + str(depth), "origin", selected)
                # The unwrapped fallback cannot contact GitHub in this offline regression.
                self.assertNotEqual(git(checkout, *arguments).returncode, 0)
                result = git(checkout, *arguments, executable=wrapper_path)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(git(checkout, "rev-parse", "FETCH_HEAD").stdout.strip(), selected)
                self.assertEqual(git(checkout, "config", "--get", "remote.origin.url").stdout.strip(), canonical)
                self.assertEqual(git(checkout, *arguments, executable=wrapper_path, FAKE_FAIL_GIT_FETCH="1").returncode, 90)
                self.assertNotEqual(git(checkout, *arguments, executable=wrapper_path,
                                        FAKE_ENGINE_GIT_SOURCE="").returncode, 0)
                self.assertNotEqual(git(checkout, "fetch", "-q", "origin", "0" * 40,
                                        executable=wrapper_path).returncode, 0)
                self.assertEqual(git(checkout, "remote", "set-url", "origin", "https://github.com/example/unrelated.git").returncode, 0)
                self.assertNotEqual(git(checkout, *arguments, executable=wrapper_path).returncode, 0)

    def test_historical_fixture_inputs_remain_fetchable_after_synthetic_transport(self):
        fixture = (ROOT / "scripts/test-install-worker-controller.sh").read_text()
        prepare = re.search(r"^prepare_historical_engine_inputs\(\) \{.*?^\}", fixture, re.M | re.S).group()
        wrapper = re.search(r"""cat >"\$fake_bin/git" <<'EOF'\n(.*?)\nEOF""", fixture, re.S).group(1)
        historical = "8bd27a2e7451806a1e37f79a15eeb7c677a12c3e"
        expected_tree = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", historical + "^{tree}"], text=True).strip()
        seed_call = fixture[fixture.index('prepare_historical_engine_inputs "$repo_root"'):fixture.index("repo_root=$fixture_repo")]
        for ref in (historical, "af9c0c13cd12866ce75dd6c43a4cda01915507e1", "003de44f7e27d7bfe5bb753098efb6eb5f9712f1"):
            self.assertIn(ref, seed_call)
        self.assertLess(fixture.index(seed_call), fixture.index('cat >"$fake_bin/git"'))
        canonical = "https://github.com/RandomDevelopment/ci-fleet.git"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = os.environ | {"GIT_ALLOW_PROTOCOL": "file", "GIT_CONFIG_GLOBAL": os.devnull,
                                        "GIT_CONFIG_NOSYSTEM": "1", "PYTHONDONTWRITEBYTECODE": "1"}
            def git(path, *arguments):
                return subprocess.check_output(["git", "-C", str(path), *arguments], text=True, env=environment).strip()
            source = root / "synthetic-source"
            source.mkdir()
            git(source, "init", "-q")
            (source / "synthetic").write_text("fixture-only source")
            git(source, "add", ".")
            git(source, "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "test: synthetic engine")
            synthetic = git(source, "rev-parse", "HEAD")
            script = "set -Eeuo pipefail\n" + prepare + f"""
prepare_historical_engine_inputs {shlex.quote(str(ROOT))} {shlex.quote(str(source))} {historical}
"""
            result = subprocess.run(["bash", "-c", script], text=True, capture_output=True, env=environment)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(git(source, "rev-parse", "HEAD"), synthetic)
            self.assertEqual(git(source, "rev-parse", historical + "^{tree}"), expected_tree)
            capabilities = json.loads(git(source, "show", historical + ":engine-capabilities.json"))
            self.assertNotIn("capacity_telemetry", capabilities["capabilities"])
            failed = subprocess.run(["bash", "-c", "set -Eeuo pipefail\n" + prepare +
                f"\nprepare_historical_engine_inputs {shlex.quote(str(ROOT))} {shlex.quote(str(source))} {'f' * 40}"],
                capture_output=True, text=True, env=environment)
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn("historical engine fixture input unavailable", failed.stderr)
            executable = root / "git-wrapper"
            executable.write_text(wrapper + "\n")
            executable.chmod(0o700)
            target = root / "installed-fetch"
            target.mkdir()
            git(target, "init", "-q")
            git(target, "remote", "add", "origin", canonical)
            environment.update(REAL_GIT=shutil.which("git"), FAKE_ENGINE_GIT_SOURCE=str(source))
            for ref in (synthetic, historical):
                for depth in (1, 2):
                    args = ["-C", str(target), "fetch", "-q", "--depth=" + str(depth), "origin", ref]
                    self.assertNotEqual(subprocess.run(["git", *args], capture_output=True, env=environment).returncode, 0)
                    result = subprocess.run([str(executable), *args], capture_output=True, text=True, env=environment)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(git(target, "rev-parse", "FETCH_HEAD"), ref)
                    self.assertEqual(git(target, "remote", "get-url", "origin"), canonical)
                    self.assertEqual(subprocess.run([str(executable), *args], capture_output=True,
                        env=environment | {"FAKE_FAIL_GIT_FETCH": "1"}).returncode, 90)
            git(target, "remote", "set-url", "origin", "https://github.com/example/unrelated.git")
            self.assertNotEqual(subprocess.run([str(executable), "-C", str(target), "fetch", "origin", historical],
                capture_output=True, env=environment).returncode, 0)

    def test_cleanup_protects_current_and_checkpoint_c_r_m_without_export(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = root / "etc/ci-fleet/ci-fleet.env"
            current.parent.mkdir(parents=True)
            checkpoint = root / "var/lib/ci-fleet/checkpoints/previous"
            checkpoint.mkdir(parents=True)
            values = f"CI_FLEET_ENGINE_REF={ENGINE}\nCI_FLEET_CONTROLLER_IMAGE=controller:reviewed\nCI_FLEET_RUNNER_IMAGE=runner:reviewed\nCI_FLEET_CONTROLLER_IMAGE_DIGEST={CONTROLLER_CONFIG}\nCI_FLEET_RUNNER_IMAGE_DIGEST={RUNNER_CONFIG}\nCI_FLEET_CONTROLLER_RUNTIME_IMAGE_ID={CONTROLLER_ROOT}\nCI_FLEET_RUNNER_RUNTIME_IMAGE_ID={RUNNER_ROOT}\n"
            current.write_text(values)
            (checkpoint / "ci-fleet.env").write_text(values)
            release = root / "opt/ci-fleet/releases" / ENGINE
            release.mkdir(parents=True)
            (release / "engine-capabilities.json").write_text(json.dumps({"schema_version": 1, "capabilities": {"portable_managed_image_ids": True}}))
            manifest = "sha256:" + "e" * 64
            bound = {"roles": {"controller": dict(config_digest=CONTROLLER_CONFIG, runtime_image_id=CONTROLLER_ROOT, manifest_digest=manifest),
                               "runner": dict(config_digest=RUNNER_CONFIG, runtime_image_id=RUNNER_ROOT, manifest_digest=None)}}
            with patch.dict(os.environ, {"CI_FLEET_ROOT_PREFIX": directory}), patch.object(cleanup, "read_bindings", return_value=bound) as read:
                protected = cleanup.protected_images()
                self.assertTrue({CONTROLLER_CONFIG, RUNNER_CONFIG, CONTROLLER_ROOT, RUNNER_ROOT, manifest} <= protected)
                self.assertEqual(read.call_count, 2)
                self.assertEqual(read.call_args_list[1].args[0], checkpoint / "install-state.json")
            with patch.dict(os.environ, {"CI_FLEET_ROOT_PREFIX": directory}), patch.object(cleanup, "read_bindings", side_effect=ValueError("stale proof")):
                with self.assertRaisesRegex(ValueError, "stale proof"):
                    cleanup.protected_images()


if __name__ == "__main__":
    unittest.main()
