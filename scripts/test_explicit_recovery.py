#!/usr/bin/env python3
"""Isolated exact-ref recovery command fixtures; no host or network access."""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import unittest

spec = importlib.util.spec_from_file_location("reconcile_tests", Path(__file__).with_name("test_remote_reconcile.py"))
assert spec is not None and spec.loader is not None
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)


class ExplicitRecovery(fixtures.TestRemoteReconcileTransaction):
    # Reuse fixture construction, not the parent regression cases.
    def setUp(self):
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
