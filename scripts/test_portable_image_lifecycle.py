#!/usr/bin/env python3
"""Offline caller regressions. No Docker, services, locks or installer entrypoint."""

import json
import os
from pathlib import Path
import re
import shlex
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
