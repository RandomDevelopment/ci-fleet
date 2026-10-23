#!/usr/bin/env python3
"""Isolated exact-ref recovery command fixtures; no host or network access."""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("reconcile_tests", Path(__file__).with_name("test_remote_reconcile.py"))
assert spec is not None and spec.loader is not None
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)


class ExplicitRecovery(fixtures.TestRemoteReconcileTransaction):
    # Reuse fixture construction, not the parent regression cases.
    def setUp(self):
        # The real approval reader rejects world-writable ancestors (e.g. /tmp).
        # Keep this suite's isolated roots under a private home directory.
        private = tempfile.TemporaryDirectory(prefix="ci-fleet-recovery-", dir=Path.home())
        self.addCleanup(private.cleanup)
        with patch.object(tempfile, "tempdir", private.name):
            super().setUp()
        self.engine = "a" * 40
        self.config = {"organization": {"delivery_engine": "RandomDevelopment/ci-fleet"},
                       "runner_pools": {"ordinary": {"capacity_budget": 1}},
                       "controllers": {"example-ci-01": {"state": "drained", "engine_ref": self.engine,
                           "pool": "ordinary", "min_runners": 0, "max_runners": 1,
                           "runner_resources": {"cpu_cores": 1, "memory_mib": 1024}}}}
        self.baseline = self.commit(self.config)
        self.candidate = self.commit(self.config)
        active = json.loads(json.dumps(self.config))
        active["controllers"]["example-ci-01"]["state"] = "active"
        self.default = self.commit(active)
        state = {"config_repository": "example-org/private-config", "config_ref": self.baseline,
                 "controller": "example-ci-01", "controller_state": "drained", "engine_ref": self.engine,
                 "engine_repository": "RandomDevelopment/ci-fleet"}
        self.state_file.write_text(json.dumps(state))
        installer = self.root / "scripts/install-worker-controller.sh"
        installer.write_text(installer.read_text().replace("exit 0\n", '''if [[ ${1:-} == --upgrade && ${FAKE_SKIP_STATE_UPDATE:-0} != 1 ]]; then
  python3 - "$CI_FLEET_REMOTE_STATE_FILE" "$@" <<'PY'
import json, sys
path = sys.argv[1]
value = json.load(open(path))
value["config_ref"] = sys.argv[sys.argv.index("--ref") + 1]
with open(path, "w") as handle:
    json.dump(value, handle)
PY
fi
exit 0
'''))
        self.env["CI_FLEET_ROOT_PREFIX"] = str(self.td)
        runtime = self.td / "opt/ci-fleet/current"
        runtime.mkdir(parents=True)
        (runtime / ".ci-fleet-engine-ref").write_text(self.engine + "\n")
        helper = Path(__file__).with_name("explicit_recovery.py")
        if helper.exists():
            shutil.copy2(helper, self.root / "scripts/explicit_recovery.py")
        real_git = shutil.which("git")
        assert real_git is not None
        self._write_executable(self.td / "bin/git", f'''#!/usr/bin/env bash
if [[ ${{1:-}} == -C && ${{3:-}} == fetch ]]; then
  printf '%s\\n' "${{@: -1}}" >> "$FETCH_LOG"
  exec {shlex.quote(real_git)} -C "$2" fetch -q "$REMOTE_FIXTURE_REPO" "${{@: -1}}"
fi
exec {shlex.quote(real_git)} "$@"
''')
        self.env.update(FETCH_LOG=str(self.td / "fetch-log"), FAKE_HEALTH_STATUS="maintenance",
                        FAKE_HEALTH_DESIRED_STATE="drained", FAKE_ROLLBACK_HEALTH_STATUS="maintenance",
                        FAKE_ROLLBACK_HEALTH_DESIRED_STATE="drained")
        # Preserve a real, accepted baseline metadata record byte-for-byte on failure.
        self.old_lkg = json.dumps({"config_repository": state["config_repository"], "config_ref": self.baseline,
                                  "controller": state["controller"], "saved_at": 1})
        (self.lkg_dir / "metadata.json").write_text(self.old_lkg)
        (self.lkg_dir / "metadata.json").chmod(0o600)

    def commit(self, config):
        (self.config_repo / "fleet.json").write_text(json.dumps(config))
        fixtures.git("add", "fleet.json", cwd=self.config_repo)
        result = fixtures.git("commit", "--allow-empty", "-qm", "fixture", cwd=self.config_repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        return fixtures.git("rev-parse", "HEAD", cwd=self.config_repo).stdout.strip()

    def run_ref(self, *args):
        return subprocess.run([str(self.root / "scripts/remote-reconcile.sh"), *args],
                              env=self.env, text=True, capture_output=True, timeout=30)

    def test_exact_ref_not_default_head(self):
        result = self.run_ref("--ref", self.candidate)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self._lkg_ref(), self.candidate)
        self.assertEqual(fixtures.git("rev-parse", "HEAD", cwd=self.config_repo).stdout.strip(), self.default)
        calls = self._installer_calls()
        self.assertIn(self.candidate, calls[-1]["args"])
        self.assertNotIn("HEAD", (self.td / "fetch-log").read_text().splitlines())


    def test_same_ref_requires_health_and_durable_lkg(self):
        self.env["FAKE_HEALTH_STATUS"] = "unhealthy"
        result = self.run_ref("--ref", self.baseline)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("CONVERGED controller", result.stdout)
        self.assertEqual((self.lkg_dir / "metadata.json").read_text(), self.old_lkg)

    def test_success_without_applied_transaction_is_rejected(self):
        installer = self.root / "scripts/install-worker-controller.sh"
        installer.write_text("\n".join(":" if '"outcome":"applied"' in line else line
                                       for line in installer.read_text().splitlines()) + "\n")
        result = self.run_ref("--ref", self.candidate)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.lkg_dir / "metadata.json").read_text(), self.old_lkg)

    def test_rollback_active_report_is_not_verified(self):
        self.env["FAKE_HEALTH_STATUS"] = "unhealthy"
        self.env["FAKE_ROLLBACK_HEALTH_DESIRED_STATE"] = "active"
        result = self.run_ref("--ref", self.candidate)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotEqual(self._reconcile_state()["status"], "rolled_back")
        self.assertNotIn("ROLLBACK_OK", result.stdout)

    def test_malformed_and_incompatible_refs_before_effects(self):
        cases = [("--ref", value) for value in ("HEAD", "a" * 39, "A" * 40, "g" * 40, "")]
        cases += [("--ref", self.candidate, flag) for flag in ("--installed-ref", "--no-op", "--check-only")]
        cases += [("--ref",), ("--ref", self.candidate, "--ref", self.candidate)]
        self.lock_file.unlink(missing_ok=True)
        for args in cases:
            with self.subTest(args=args):
                result = self.run_ref(*args)
                self.assertEqual(result.returncode, 2)
                self.assertNotIn("GENERATING_TOKEN", result.stdout)
                self.assertFalse(self.lock_file.exists())
                self.assertFalse(self.installer_log.exists())
                self.assertFalse((self.td / "fetch-log").exists())

    def test_policy_changes_rejected_without_installer_or_lkg_mutation(self):
        mutations = [
            ("state", "active"), ("state", "disabled"), ("engine_ref", "b" * 40),
            ("max_runners", 2), ("min_runners", 1), ("pool", "other"),
            ("runner_resources", {"cpu_cores": 2, "memory_mib": 1024}),
            ("scale_set_name", "changed"), ("docker_network_policy", {}),
        ]
        for key, value in mutations:
            with self.subTest(key=key, value=value):
                config = json.loads(json.dumps(self.config))
                config["controllers"]["example-ci-01"][key] = value
                candidate = self.commit(config)
                result = self.run_ref("--ref", candidate)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.installer_log.exists())
                self.assertEqual((self.lkg_dir / "metadata.json").read_text(), self.old_lkg)

    def test_active_installed_baseline_rejected_before_token(self):
        state = json.loads(self.state_file.read_text())
        state["controller_state"] = "active"
        self.state_file.write_text(json.dumps(state))
        result = self.run_ref("--ref", self.candidate)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("GENERATING_TOKEN", result.stdout)

    def test_active_authored_baseline_cannot_be_forged_drained_by_metadata(self):
        state = json.loads(self.state_file.read_text())
        state["config_ref"] = self.default
        self.state_file.write_text(json.dumps(state))
        result = self.run_ref("--ref", self.candidate)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.installer_log.exists())

    def test_current_runtime_engine_mismatch_rejected(self):
        (self.td / "opt/ci-fleet/current/.ci-fleet-engine-ref").write_text("b" * 40)
        result = self.run_ref("--ref", self.candidate)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.installer_log.exists())

    def test_review_denial_preserves_lkg(self):
        self._activate_approval_fixture(reject=True)
        result = self.run_ref("--ref", self.candidate)
        self.assertEqual(result.returncode, 3)
        self.assertFalse(self.installer_log.exists())
        self.assertEqual((self.lkg_dir / "metadata.json").read_text(), self.old_lkg)

    def test_recovery_adds_images_only_for_current_engine(self):
        config = json.loads(json.dumps(self.config))
        config["managed_images"] = {self.engine: {"amd64": {"runner": "sha256:" + "1" * 64,
                                                          "controller": "sha256:" + "2" * 64}}}
        candidate = self.commit(config)
        result = self.run_ref("--ref", candidate)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self._lkg_ref(), candidate)

    def test_unknown_transaction_keeps_old_lkg(self):
        self.env.update(FAKE_INSTALLER_RESULT="unverified")
        result = self.run_ref("--ref", self.candidate)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(len(self._installer_calls()), 1)
        self.assertEqual((self.lkg_dir / "metadata.json").read_text(), self.old_lkg)

    def test_pretransaction_denial_keeps_old_lkg(self):
        self.env.update(FAKE_TRANSACTION_RECORD=json.dumps({"schema_version": 1, "outcome": "rejected",
                        "phase": "pre_transaction", "reason": "installer_precondition"}), FAKE_TRANSACTION_EXIT="2")
        result = self.run_ref("--ref", self.candidate)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(self._reconcile_state()["transaction_phase"], "pre_transaction")
        self.assertEqual((self.lkg_dir / "metadata.json").read_text(), self.old_lkg)

    def install_evaluated_health(self):
        """Keep host collection doubled, but evaluate the live reconcile record."""
        scripts = self.root / "scripts"
        for source, destination in (("health.py", "production_health.py"),
                                    ("status_auth.py", "status_auth.py")):
            shutil.copy2(Path(__file__).with_name(source), scripts / destination)
        (scripts / "health.py").write_text('''import json, os, sys
from pathlib import Path
from production_health import Thresholds, evaluate, _reconcile_state

calls = Path(os.environ["FAKE_INSTALLER_LOG"]).read_text().splitlines()
rollback = len(calls) > 1
record = _reconcile_state(Path(os.environ["CI_FLEET_RECONCILE_STATE_DIR"]) / "state.json")
snapshot = {
    "controller_id": "example-ci-01", "desired_state": "drained",
    "disks": {"root": {"used_percent": 20, "inode_used_percent": 10},
              "docker": {"used_percent": 30, "inode_used_percent": 15}},
    "memory_available_percent": 75, "swap_used_percent": 0, "recent_oom": False,
    "docker_available": rollback or os.environ.get("REJECT_CANDIDATE_HEALTH") != "1",
    "controller": {"state": "missing", "restart_count": 0, "oom_killed": False},
    "configured_capacity": {"min": 0, "max": 1}, "effective_capacity": {"min": 0, "max": 0},
    "managed": {"running": 0, "inactive": 0, "unhealthy": 0, "restarting": 0},
    "stale": {"images": 0, "networks": 0, "volumes": 0, "build_cache_bytes": 0},
    "services": {"cleanup": "ok", "drift": "ok"},
    "timers": {"health": "ok", "cleanup": "ok", "drift": "ok", "updates": "ok"},
    "pending_reboot": False, "failed_packages": False, "clock_synchronized": True,
    "backup": "not_configured", "reconciliation": record,
}
if rollback and os.environ.get("REJECT_ROLLBACK_HEALTH") == "1":
    snapshot["docker_available"] = False
report = evaluate(snapshot, Thresholds())
with open(os.environ["EVALUATED_HEALTH_LOG"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps({"rollback": rollback, "record": record, "report": report}) + "\\n")
Path(sys.argv[sys.argv.index("--output") + 1]).write_text(json.dumps(report), encoding="utf-8")
raise SystemExit(report["exit_code"])
''', encoding="utf-8")
        self.env["EVALUATED_HEALTH_LOG"] = str(self.td / "evaluated-health.jsonl")
        directory = Path(self.env["CI_FLEET_RECONCILE_STATE_DIR"])
        directory.mkdir(mode=0o700, exist_ok=True)
        (directory / "state.json").write_text(json.dumps({"last_success_at": 1}))

    def test_final_health_rollback_uses_production_reconciliation_evaluator(self):
        self.install_evaluated_health()
        self.env["REJECT_CANDIDATE_HEALTH"] = "1"
        result = self.run_ref("--ref", self.candidate)
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn("ROLLBACK_OK", result.stdout, result.stdout + result.stderr)
        state = self._reconcile_state()
        self.assertEqual(state["status"], "rolled_back")
        self.assertEqual(state["applied_commit"], self.baseline)
        self.assertEqual(state["last_success_at"], 1)
        observations = [json.loads(line) for line in
                        Path(self.env["EVALUATED_HEALTH_LOG"]).read_text().splitlines()]
        self.assertEqual(len(observations), 2)
        self.assertTrue(observations[-1]["rollback"])
        self.assertEqual(observations[-1]["record"]["status"], "reconciling")
        self.assertEqual(observations[-1]["record"]["applied_commit"], self.baseline)
        self.assertEqual(observations[-1]["report"]["status"], "warning")
        self.assertEqual((self.lkg_dir / "metadata.json").read_text(), self.old_lkg)

    def test_lkg_failure_rollback_uses_production_reconciliation_evaluator(self):
        for failure in ("write", "missing_write", "readback"):
            with self.subTest(failure=failure):
                self.setUp()
                self.install_evaluated_health()
                self.env["DENY_LKG_REF"] = self.candidate
                real_python = shutil.which("python3")
                assert real_python is not None
                write_outcome = "exit 1" if failure == "write" else "exit 0"
                if failure == "readback":
                    condition = '${1:-} == -c && ${3:-} == "$CI_FLEET_LKG_DIR/metadata.json" && ${5:-} == "$DENY_LKG_REF"'
                    write_outcome = "exit 1"
                else:
                    condition = '${1:-} == - && ${2:-} == "$CI_FLEET_LKG_DIR/metadata.json" && ${4:-} == "$DENY_LKG_REF"'
                self._write_executable(self.td / "bin/python3", f'''#!/usr/bin/env bash
if [[ {condition} ]]; then {write_outcome}; fi
exec {shlex.quote(real_python)} "$@"
''')
                result = self.run_ref("--ref", self.candidate)
                self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
                state = self._reconcile_state()
                self.assertEqual(state["status"], "rolled_back", result.stdout + result.stderr)
                self.assertEqual(state["applied_commit"], self.baseline)
                self.assertEqual(state["last_success_at"], 1)
                self.assertNotIn("RECONCILE_OK", result.stdout)
                self.assertEqual((self.lkg_dir / "metadata.json").read_text(), self.old_lkg)
                observations = [json.loads(line) for line in
                                Path(self.env["EVALUATED_HEALTH_LOG"]).read_text().splitlines()]
                self.assertTrue(observations[-1]["rollback"])
                self.assertEqual(observations[-1]["record"]["status"], "reconciling")
                self.assertEqual(observations[-1]["record"]["applied_commit"], self.baseline)
                self.assertEqual(observations[-1]["report"]["status"], "warning")

    def test_missing_rollback_state_persistence_cannot_verify_recovery(self):
        self.install_evaluated_health()
        self.env["REJECT_CANDIDATE_HEALTH"] = "1"
        real_python = shutil.which("python3")
        assert real_python is not None
        self._write_executable(self.td / "bin/python3", f'''#!/usr/bin/env bash
if [[ ${{1:-}} == - && ${{2:-}} == "$CI_FLEET_RECONCILE_STATE_DIR/state.json" && ${{3:-}} == reconciling ]] && \
   [[ $(wc -l < "$FAKE_INSTALLER_LOG") -gt 1 ]]; then
  rm -f -- "$CI_FLEET_RECONCILE_STATE_DIR/state.json"
  exit 1
fi
exec {shlex.quote(real_python)} "$@"
''')
        result = self.run_ref("--ref", self.candidate)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("ROLLBACK_OK", result.stdout, result.stdout + result.stderr)
        self.assertNotEqual(self._reconcile_state()["status"], "rolled_back")

    def test_final_health_failure_rolls_back_to_drained_baseline(self):
        self.env["FAKE_HEALTH_STATUS"] = "unhealthy"
        result = self.run_ref("--ref", self.candidate)
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertEqual(self._reconcile_state()["status"], "rolled_back")
        self.assertIn(self.baseline, self._installer_calls()[-1]["args"])
        self.assertEqual((self.lkg_dir / "metadata.json").read_text(), self.old_lkg)

    def test_installer_cannot_silently_keep_old_ref(self):
        self.env["FAKE_SKIP_STATE_UPDATE"] = "1"
        result = self.run_ref("--ref", self.candidate)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.lkg_dir / "metadata.json").read_text(), self.old_lkg)

    def test_unrelated_fleet_and_image_changes_are_rejected(self):
        for mutation in ("routing", "budget", "repository", "unrelated_images", "replace_images"):
            with self.subTest(mutation=mutation):
                config = json.loads(json.dumps(self.config))
                if mutation == "routing":
                    config["runner_pools"]["ordinary"]["routing_labels"] = ["changed"]
                elif mutation == "budget":
                    config["runner_pools"]["ordinary"]["capacity_budget"] = 2
                elif mutation == "repository":
                    config["organization"]["delivery_engine"] = "example-org/other-engine"
                elif mutation == "unrelated_images":
                    config["managed_images"] = {"b" * 40: {}}
                else:
                    baseline_config = json.loads(json.dumps(config))
                    baseline_config["managed_images"] = {self.engine: {"amd64": {"runner": "old"}}}
                    baseline = self.commit(baseline_config)
                    state = json.loads(self.state_file.read_text())
                    state["config_ref"] = baseline
                    self.state_file.write_text(json.dumps(state))
                    config["managed_images"] = {self.engine: {"amd64": {"runner": "new"}}}
                result = self.run_ref("--ref", self.commit(config))
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.installer_log.exists())

    def test_lkg_readback_mismatch_must_not_be_success(self):
        real_python = shutil.which("python3")
        assert real_python is not None
        self._write_executable(self.td / "bin/python3", f'''#!/usr/bin/env bash
if [[ ${{1:-}} == - && ${{2:-}} == "$CI_FLEET_LKG_DIR/metadata.json" && ${{4:-}} == "$DENY_LKG_REF" ]]; then
  # Simulate a write helper returning zero without persisting the candidate.
  exit 0
fi
exec {shlex.quote(real_python)} "$@"
''')
        self.env["DENY_LKG_REF"] = self.candidate
        result = self.run_ref("--ref", self.candidate)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.lkg_dir / "metadata.json").read_text(), self.old_lkg)

    def test_malformed_ref_creates_no_transaction_temp(self):
        marker = self.td / "mktemp-called"
        self._write_executable(self.td / "bin/mktemp", f'#!/usr/bin/env bash\ntouch {shlex.quote(str(marker))}\nexit 1\n')
        result = self.run_ref("--ref", "HEAD")
        self.assertEqual(result.returncode, 2)
        self.assertFalse(marker.exists())

    def test_rollback_cannot_claim_success_without_baseline_readback(self):
        installer = self.root / "scripts/install-worker-controller.sh"
        installer.write_text(installer.read_text().replace('${FAKE_SKIP_STATE_UPDATE:-0} != 1',
                             '${FAKE_SKIP_STATE_UPDATE:-0} != 1 && ${2:-} != --restore-last-known-good'))
        self.env["FAKE_HEALTH_STATUS"] = "unhealthy"
        result = self.run_ref("--ref", self.candidate)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotEqual(self._reconcile_state()["status"], "rolled_back")
        self.assertNotIn("ROLLBACK_OK", result.stdout)

    def install_live_recovery_guards(self):
        """Exercise the installer's actual approval and image-free checkpoint guards."""
        source = Path(__file__).with_name("install-worker-controller.sh").read_text()
        guards = source.split("find_bootstrap_lkg_checkpoint() {", 1)[1].split("\nvalidate_candidate_config_commit()", 1)[0]
        approval = source.split("verify_configuration_approval() {", 1)[1].split("\nrequire_configuration_approval_capability()", 1)[0]
        scripts = self.root / "scripts"
        for name in ("verify_config_approval.py", "desired_state.py", "apt_cache.py"):
            shutil.copy2(Path(__file__).with_name(name), scripts / name)
        # Candidate network evidence remains doubled; recovery uses the real CLI.
        real_python = shutil.which("python3")
        assert real_python is not None
        self._write_executable(self.td / "bin/python3", f'''#!/usr/bin/env bash
if [[ ${{1:-}} == */verify_config_approval.py && " $* " != *" --recovery-record "* ]]; then
  exit 0
fi
if [[ ${{1:-}} == */desired_state.py && ${{2:-}} == validate ]]; then exit 0; fi
exec {shlex.quote(real_python)} "$@"
''')
        policy = self.td / "etc/ci-fleet"
        policy.mkdir(parents=True)
        (policy / "config-review-required").write_text("1\n")
        (policy / "config-review-policy.json").write_text(json.dumps({
            "schema_version": 1, "configuration_repository_id": 1, "branch": "main",
            "workflow_id": 1, "required_jobs": ["validate"], "reviewer_ids": [1]}))
        for path in policy.iterdir():
            path.chmod(0o600)
        checkpoints = self.td / "checkpoints"
        checkpoint = checkpoints / "baseline"
        checkpoint.mkdir(parents=True, mode=0o700)
        checkpoints.chmod(0o700)
        (checkpoint / ".complete").write_text("1\n")
        (checkpoint / "install-state.json").write_text(self.state_file.read_text())
        (checkpoint / "ci-fleet.env").write_text(
            f"CI_FLEET_CONFIG_REPOSITORY=example-org/private-config\nCI_FLEET_CONFIG_REF={self.baseline}\nCI_FLEET_INSTANCE=example-ci-01\n")
        for path in checkpoint.iterdir():
            path.chmod(0o600)
        installer = scripts / "install-worker-controller.sh"
        preamble = '''set -e
repo_root=$(cd "$(dirname "$0")/.." && pwd)
config_review_policy=$CI_FLEET_ROOT_PREFIX/etc/ci-fleet/config-review-policy.json
config_review_required=$CI_FLEET_ROOT_PREFIX/etc/ci-fleet/config-review-required
state_file=$CI_FLEET_REMOTE_STATE_FILE
state_root=$(dirname "$state_file")
checkpoints_dir=$CI_FLEET_ROOT_PREFIX/checkpoints
testing=1
die() { echo "$*" >&2; exit 2; }
configuration_approval_required() { return 0; }
'''
        guard_call = '''if [[ ${2:-} == --restore-last-known-good ]]; then
  restore_lkg=true
  args=("$@")
  for ((i=0; i<$#; i++)); do
    case ${args[i]} in
      --ref) config_ref=${args[i+1]} ;;
      --config-identity) config_identity=${args[i+1]} ;;
      --controller) controller_id=${args[i+1]} ;;
    esac
  done
  config_tree=$(git -C "$REMOTE_FIXTURE_REPO" rev-parse "$config_ref^{tree}")
  verify_configuration_approval
  find_bootstrap_lkg_checkpoint > "$CI_FLEET_ROOT_PREFIX/checkpoint-used"
fi
'''
        installer.write_text(installer.read_text().replace("python3 - ", preamble +
            "find_bootstrap_lkg_checkpoint() {" + guards + "\nverify_configuration_approval() {" + approval +
            "\n" + guard_call + "python3 - ", 1))

    def test_candidate_written_then_readback_failure_restores_live_trusted_baseline(self):
        self.install_live_recovery_guards()
        wrapper = self.td / "bin/python3"
        wrapper.write_text(wrapper.read_text().replace("exec ", '''if [[ ${1:-} == -c && ${3:-} == "$CI_FLEET_LKG_DIR/metadata.json" && ${5:-} == "$DENY_LKG_REF" ]]; then
  exit 1
fi
exec ''', 1))
        self.env["DENY_LKG_REF"] = self.candidate
        result = self.run_ref("--ref", self.candidate)
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertEqual(self._reconcile_state()["status"], "rolled_back")
        self.assertEqual(json.loads(self.state_file.read_text())["config_ref"], self.baseline)
        self.assertEqual((self.lkg_dir / "metadata.json").read_text(), self.old_lkg)
        self.assertTrue((self.td / "checkpoint-used").read_text().strip())

    def test_live_recovery_guard_still_rejects_missing_checkpoint(self):
        self.install_live_recovery_guards()
        shutil.rmtree(self.td / "checkpoints")
        self.env["FAKE_HEALTH_STATUS"] = "unhealthy"
        result = self.run_ref("--ref", self.candidate)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotEqual(self._reconcile_state()["status"], "rolled_back")

    def test_live_recovery_guard_still_rejects_untrusted_policy(self):
        self.install_live_recovery_guards()
        (self.td / "etc/ci-fleet/config-review-policy.json").chmod(0o666)
        self.env["FAKE_HEALTH_STATUS"] = "unhealthy"
        result = self.run_ref("--ref", self.candidate)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotEqual(self._reconcile_state()["status"], "rolled_back")

    def test_rollback_probe_failure_without_output_cannot_reuse_candidate_report(self):
        health = self.root / "scripts/health.py"
        health.write_text(health.read_text().replace('with open(output, "w",',
            'if rollback:\n    raise SystemExit(1)\nwith open(output, "w",'))
        real_python = shutil.which("python3")
        assert real_python is not None
        self._write_executable(self.td / "bin/python3", f'''#!/usr/bin/env bash
if [[ ${{1:-}} == - && ${{2:-}} == "$CI_FLEET_LKG_DIR/metadata.json" && ${{4:-}} == "$DENY_LKG_REF" ]]; then exit 1; fi
exec {shlex.quote(real_python)} "$@"
''')
        self.env["DENY_LKG_REF"] = self.candidate
        result = self.run_ref("--ref", self.candidate)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotEqual(self._reconcile_state()["status"], "rolled_back", result.stdout + result.stderr)

    def test_fresh_accepted_report_with_failed_process_is_rejected(self):
        health = self.root / "scripts/health.py"
        health.write_text(health.read_text().replace('raise SystemExit({"healthy": 0, "maintenance": 0, "warning": 1, "unhealthy": 2}.get(status, 2))', 'raise SystemExit(1)'))
        result = self.run_ref("--ref", self.candidate)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotEqual(self._reconcile_state()["status"], "rolled_back")

    def test_warning_probe_exit_one_remains_accepted(self):
        self.env["FAKE_HEALTH_STATUS"] = "warning"
        result = self.run_ref("--ref", self.candidate)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_lkg_write_failure_never_reports_success(self):
        real_python = shutil.which("python3")
        assert real_python is not None
        self._write_executable(self.td / "bin/python3", f'''#!/usr/bin/env bash
if [[ ${{1:-}} == - && ${{2:-}} == "$CI_FLEET_LKG_DIR/metadata.json" && ${{4:-}} == "$DENY_LKG_REF" ]]; then
  exit 1
fi
exec {shlex.quote(real_python)} "$@"
''')
        self.env["DENY_LKG_REF"] = self.candidate
        result = self.run_ref("--ref", self.candidate)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("RECONCILE_OK", result.stdout)
        self.assertEqual(self._reconcile_state()["status"], "rolled_back")
        self.assertEqual((self.lkg_dir / "metadata.json").read_text(), self.old_lkg)


# Inherited fixture regression tests belong to their original suite.
for name in list(vars(fixtures.TestRemoteReconcileTransaction)):
    if name.startswith("test_"):
        setattr(ExplicitRecovery, name, None)

if __name__ == "__main__":
    unittest.main()
