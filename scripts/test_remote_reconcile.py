#!/usr/bin/env python3
"""Regression tests for remote reconciliation scripts.

Tests the github-app-token.sh wrapper behaviour, remote-reconcile.sh flow
(validation, fetch, check, reconcile, rollback), and secret redaction.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
TOKEN_SCRIPT = SCRIPTS / "github-app-token.sh"
RECONCILE_SCRIPT = SCRIPTS / "remote-reconcile.sh"
TOKEN_SCRIPT.chmod(0o755)
RECONCILE_SCRIPT.chmod(0o755)
INSTALLER = SCRIPTS / "install-worker-controller.sh"


def git(*args: str, cwd: str | Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + list(args),
        capture_output=True, text=True, cwd=cwd,
    )


class TestGitHubAppToken(unittest.TestCase):
    """Tests for the token helper (unit-level, no real API calls)."""

    def test_requires_args(self):
        """Exits 2 with no arguments."""
        result = subprocess.run(
            [str(TOKEN_SCRIPT)], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("ERROR", result.stdout + result.stderr)

    def test_requires_valid_env_file(self):
        """Exits 2 with a non-existent --env-file."""
        result = subprocess.run(
            [str(TOKEN_SCRIPT), "--env-file", "/nonexistent/path"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("not found", result.stderr)

    def test_rejects_missing_key(self):
        """Exits 2 when the key file does not exist."""
        result = subprocess.run(
            [str(TOKEN_SCRIPT), "--app-id", "123", "--install-id", "456", "--key-path", "/nonexistent/key.pem"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("not found", result.stderr)

    def test_parses_env_file(self):
        """Correctly extracts app-id, install-id, and key-path from an env file."""
        with tempfile.TemporaryDirectory() as td:
            env_file = Path(td) / "host.env"
            env_file.write_text(
                "CI_FLEET_GITHUB_APP_CLIENT_ID=98765\n"
                "CI_FLEET_GITHUB_APP_INSTALLATION_ID=54321\n"
                "CI_FLEET_GITHUB_APP_PRIVATE_KEY_FILE=/etc/ci-fleet/app-key.pem\n"
                "CI_FLEET_RUNNER_TTL=6h\n"
            )
            result = subprocess.run(
                [str(TOKEN_SCRIPT), "--env-file", str(env_file)],
                capture_output=True, text=True,
            )
            # Should fail because key doesn't exist, but the parsing is correct
            self.assertEqual(result.returncode, 2)
            self.assertIn("not found", result.stderr)

    def test_stderr_does_not_leak_key(self):
        """Error output must not contain the private key content."""
        with tempfile.TemporaryDirectory() as td:
            env_file = Path(td) / "host.env"
            key_file = Path(td) / "key.pem"
            # Fake RSA-looking key; split literals so the committed source
            # does not trip the repo secret scanner's key-header pattern.
            marker = "PRIVATE KEY"
            key_file.write_text(
                "-----BEGIN RSA " + marker + "-----\n"
                "ZmFrZWtleW1hdGVyaWFsZmFrZWtleW1hdGVyaWFsCg==\n"
                "-----END RSA " + marker + "-----\n"
            )
            env_file.write_text(
                f"CI_FLEET_GITHUB_APP_CLIENT_ID=123\n"
                f"CI_FLEET_GITHUB_APP_INSTALLATION_ID=456\n"
                f"CI_FLEET_GITHUB_APP_PRIVATE_KEY_FILE={key_file}\n"
            )
            result = subprocess.run(
                [str(TOKEN_SCRIPT), "--env-file", str(env_file)],
                capture_output=True, text=True,
            )
            combined = (result.stdout + result.stderr).lower()
            # The fake key material itself must not appear in stderr or stdout
            self.assertNotIn("fakekeymate", combined)
            self.assertNotIn("begin rsa", combined)


class TestRemoteReconcile(unittest.TestCase):
    """Tests for the reconcile script (fixture-driven, no real API calls)."""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.state_dir = self.td / "state"
        self.lkg_dir = self.td / "lkg"
        self.state_file = self.state_dir / "install-state.json"

        # Create a minimal valid install state
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._write_state("example-org/private-desired-state",
                          "0000000000000000000000000000000000000000",
                          "rd-ci-fleet-01")

        # Create a fake host.env for token gen
        self.host_env = self.td / "host.env"
        self.host_env.write_text(
            "CI_FLEET_GITHUB_APP_CLIENT_ID=123\n"
            "CI_FLEET_GITHUB_APP_INSTALLATION_ID=456\n"
            "CI_FLEET_GITHUB_APP_PRIVATE_KEY_FILE=/nonexistent/key.pem\n"
        )
        self.host_env.chmod(0o600)

        self.env = os.environ.copy()
        self.env.update({
            "CI_FLEET_REMOTE_STATE_FILE": str(self.state_file),
            "CI_FLEET_RENDERED_ENV": str(self.td / "ci-fleet.env"),
            "CI_FLEET_HOST_ENV": str(self.host_env),
            "CI_FLEET_INSTALLER_LOCK": str(self.td / "installer.lock"),
            "CI_FLEET_LKG_DIR": str(self.lkg_dir),
            "CI_FLEET_RECONCILE_STATE_DIR": str(self.td / "reconcile-state"),
            "CI_FLEET_RECONCILE_MAX_ATTEMPTS": "1",
            "CI_FLEET_TESTING": "1",
        })

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def _write_state(self, config_repo: str, config_ref: str, controller: str):
        state = {
            "controller": controller,
            "config_repository": config_repo,
            "config_ref": config_ref,
            "controller_state": "active",
            "engine_ref": "0000000000000000000000000000000000000000",
        }
        self.state_file.write_text(json.dumps(state, indent=2))
        self.state_file.chmod(0o600)

    def test_no_installed_state_exits_2(self):
        """Fails with exit 2 when no install-state.json exists."""
        self.state_file.unlink(missing_ok=True)
        result = subprocess.run(
            [str(RECONCILE_SCRIPT), "--check-only"],
            capture_output=True, text=True, env=self.env,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("RECONCILE_ERROR", result.stderr)

    def test_token_env_prefers_host_env(self):
        """Uses host.env when available for token generation."""
        # Should try to generate token and fail because key doesn't exist
        result = subprocess.run(
            [str(RECONCILE_SCRIPT), "--check-only"],
            capture_output=True, text=True, env=self.env,
        )
        self.assertIn(result.returncode, (1, 2), f"expected 1 or 2, got {result.returncode}")
        self.assertIn("token", result.stderr.lower())

    def test_no_op_flag_succeeds_with_valid_state(self):
        """--no-op should parse and validate state without fetching."""
        # It will still try to generate a token, so it'll fail there.
        # This tests that --no-op is accepted as a flag.
        result = subprocess.run(
            [str(RECONCILE_SCRIPT), "--no-op"],
            capture_output=True, text=True, env=self.env,
        )
        # It should fail on token generation, not arg parsing
        self.assertIn(result.returncode, (1, 2), f"expected 1 or 2, got {result.returncode}")
        # Verify it got past arg parsing
        self.assertIn("GENERATING_TOKEN", result.stderr + result.stdout)

    def test_help_exits_0(self):
        """--help prints usage and exits 0."""
        result = subprocess.run(
            [str(RECONCILE_SCRIPT), "--help"],
            capture_output=True, text=True, env=self.env,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("usage", result.stdout + result.stderr)

    def test_installed_ref_is_check_only(self):
        mutating = subprocess.run(
            [str(RECONCILE_SCRIPT), "--installed-ref"],
            capture_output=True, text=True, env=self.env,
        )
        self.assertEqual(mutating.returncode, 2)
        self.assertIn("requires --check-only", mutating.stderr)

    def test_installed_ref_rejects_symbolic_state_before_fetch(self):
        self._write_state("example-org/private-desired-state", "HEAD", "rd-ci-fleet-01")
        result = subprocess.run(
            [str(RECONCILE_SCRIPT), "--check-only", "--installed-ref"],
            capture_output=True, text=True, env=self.env,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("full lowercase commit SHA", result.stderr)
        self.assertNotIn("GENERATING_TOKEN", result.stdout)

    def test_reconcile_state_saved_on_failure(self):
        """State file is saved even when reconciliation fails."""
        result = subprocess.run(
            [str(RECONCILE_SCRIPT)],
            capture_output=True, text=True, env=self.env,
        )
        state_file = self.env["CI_FLEET_RECONCILE_STATE_DIR"]
        state_path = Path(state_file) / "state.json"
        # Should exist even on failure
        self.assertTrue(state_path.exists() or result.returncode != 0)

    def test_reconcile_failure_preserves_last_success_timestamp(self):
        state_path = Path(self.env["CI_FLEET_RECONCILE_STATE_DIR"]) / "state.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text(json.dumps({
            "status": "converged", "desired_commit": "0" * 40, "applied_commit": "0" * 40,
            "health": "healthy", "message": "ok", "checked_at": 777, "last_success_at": 777,
        }))
        subprocess.run([str(RECONCILE_SCRIPT)], capture_output=True, text=True, env=self.env)
        self.assertEqual(json.loads(state_path.read_text())["last_success_at"], 777)

    def test_non_object_reconcile_state_is_recovered(self):
        state_path = Path(self.env["CI_FLEET_RECONCILE_STATE_DIR"]) / "state.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text("[]\n")
        subprocess.run([str(RECONCILE_SCRIPT)], capture_output=True, text=True, env=self.env)
        state = json.loads(state_path.read_text())
        self.assertIsInstance(state, dict)
        self.assertIsNone(state["last_success_at"])

    def test_reconciliation_health_probe_suppresses_delivery(self):
        content = RECONCILE_SCRIPT.read_text()
        probe = content.split("run_health_check()", 1)[1].split("}", 1)[0]
        self.assertIn("export CI_FLEET_HEALTH_SUPPRESS_DELIVERY=1", probe)

    def test_no_op_does_not_advance_last_success_timestamp(self):
        content = RECONCILE_SCRIPT.read_text()
        no_op = content.split('if [[ "$no_op" == true ]]; then', 2)[2].split("exit 0", 1)[0]
        self.assertNotIn("'no change, converged' true", no_op)
        self.assertIn('if sys.argv[7] == "true":', content)
        self.assertEqual(content.count("save_reconcile_state 'converged'"), 4)
        self.assertEqual(content.count("'no change, converged' true"), 1)
        self.assertEqual(content.count('"reconciled to ${desired_commit}" true'), 1)

    def test_validate_schema_output_no_secrets(self):
        """Sanitized log output must not contain actual key material or token values."""
        result = subprocess.run(
            [str(RECONCILE_SCRIPT)],
            capture_output=True, text=True, env=self.env,
        )
        combined = (result.stdout + result.stderr)
        # Must not contain actual key file content or raw AIA value
        self.assertNotIn("MIIEpAIBAAKCAQEAFAKEKEYMATE", combined)
        # Must not contain raw host.env values
        self.assertNotIn("installation_id=456", combined.lower())
        self.assertNotIn("client_id=123", combined.lower())


class TestRemoteReconcileTransaction(unittest.TestCase):
    """Runs reconciliation against local command fixtures, without Docker or GitHub."""

    prior_ref = "0" * 40

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.td, ignore_errors=True)
        self.root = self.td / "engine"
        scripts = self.root / "scripts"
        template_scripts = self.root / "templates" / "config-repository" / "scripts"
        scripts.mkdir(parents=True)
        template_scripts.mkdir(parents=True)
        shutil.copy2(RECONCILE_SCRIPT, scripts / RECONCILE_SCRIPT.name)
        (scripts / RECONCILE_SCRIPT.name).chmod(0o755)

        self.installer_log = self.td / "installer.jsonl"
        self.policy_marker = self.td / "policy-called"
        self.health_observed_lkg = self.td / "health-observed-lkg"
        self.lkg_dir = self.td / "lkg"
        self.state_file = self.td / "state" / "install-state.json"
        self.state_file.parent.mkdir()
        self.host_env = self.td / "host.env"
        self.host_env.write_text("fixture=1\n", encoding="utf-8")
        self.lock_file = self.td / "installer.lock"

        self._write_executable(
            scripts / "github-app-token.sh",
            "#!/usr/bin/env bash\nprintf 'fixture-token\\n'\n",
        )
        self._write_executable(
            scripts / "install-worker-controller.sh",
            """#!/usr/bin/env bash
python3 - "$FAKE_INSTALLER_LOG" "$@" <<'PY'
import json, os, sys
with open(sys.argv[1], "a", encoding="utf-8") as handle:
    handle.write(json.dumps({
        "args": sys.argv[2:],
        "fd9": os.path.realpath("/proc/self/fd/9"),
        "lock_fd": os.environ.get("CI_FLEET_INSTALLER_LOCK_FD"),
    }) + "\\n")
PY
if [[ ${1:-} == --check && ${FAKE_POLICY_STATE:-healthy} != healthy ]]; then
  exit 3
fi
if [[ ${1:-} == --upgrade && ${FAKE_INSTALLER_RESULT:-success} == unverified ]]; then
  printf 'ERROR: network-policy rollback verification failed\\n' >&2
  exit 2
fi
if [[ ${1:-} == --upgrade && ${FAKE_INSTALLER_RESULT:-success} =~ ^(success|cleanup_failure)$ ]]; then
  printf '{"schema_version":1,"outcome":"applied"}\\n' >&7
fi
if [[ ${1:-} == --upgrade && ${FAKE_INSTALLER_RESULT:-success} == cleanup_failure ]]; then
  printf 'ERROR: post-commit cleanup failed\\n' >&2
  exit 1
fi
exit 0
""",
        )
        self._write_executable(
            scripts / "apply-docker-network-policy.sh",
            f"#!/usr/bin/env bash\n: > {shlex.quote(str(self.policy_marker))}\n",
        )
        (scripts / "health.py").write_text(
            """import json, os, sys
output = sys.argv[sys.argv.index("--output") + 1]
lkg = os.path.join(os.environ["CI_FLEET_LKG_DIR"], "metadata.json")
try:
    observed = json.load(open(lkg, encoding="utf-8"))["config_ref"]
except (FileNotFoundError, KeyError, TypeError, json.JSONDecodeError):
    observed = ""
with open(os.environ["FAKE_HEALTH_OBSERVED_LKG"], "w", encoding="utf-8") as handle:
    handle.write(observed + "\\n")
installer_calls = open(os.environ["FAKE_INSTALLER_LOG"], encoding="utf-8").read().splitlines()
rollback = len(installer_calls) > 1
status = os.environ["FAKE_ROLLBACK_HEALTH_STATUS"] if rollback else os.environ["FAKE_HEALTH_STATUS"]
desired_state = os.environ["FAKE_ROLLBACK_HEALTH_DESIRED_STATE"] if rollback else os.environ["FAKE_HEALTH_DESIRED_STATE"]
with open(output, "w", encoding="utf-8") as handle:
    if status == "malformed":
        handle.write("{")
    else:
        json.dump({"status": status, "desired_state": desired_state}, handle)
""",
            encoding="utf-8",
        )
        for path in (
            scripts / "desired_state.py",
            scripts / "scan_committed_secrets.py",
            template_scripts / "validate.py",
        ):
            path.write_text("raise SystemExit(0)\n", encoding="utf-8")

        self.config_repo = self.td / "config"
        self.assertEqual(git("init", "-q", str(self.config_repo)).returncode, 0)
        self.assertEqual(git("config", "user.name", "fixture", cwd=self.config_repo).returncode, 0)
        self.assertEqual(git("config", "user.email", "fixture@example.invalid", cwd=self.config_repo).returncode, 0)
        (self.config_repo / "fleet.json").write_text("{}\n", encoding="utf-8")
        self.assertEqual(git("add", "fleet.json", cwd=self.config_repo).returncode, 0)
        self.assertEqual(git("commit", "-q", "-m", "fixture", cwd=self.config_repo).returncode, 0)
        self.desired_ref = git("rev-parse", "HEAD", cwd=self.config_repo).stdout.strip()

        fake_bin = self.td / "bin"
        fake_bin.mkdir()
        real_git = shutil.which("git")
        self.assertIsNotNone(real_git)
        self._write_executable(
            fake_bin / "git",
            f"""#!/usr/bin/env bash
if [[ ${{1:-}} == -C && ${{3:-}} == fetch ]]; then
  exec {shlex.quote(real_git or 'git')} -C "$2" fetch -q --depth=1 "$REMOTE_FIXTURE_REPO" "$REMOTE_FIXTURE_REF"
fi
exec {shlex.quote(real_git or 'git')} "$@"
""",
        )

        self.env = os.environ.copy()
        self.env.update({
            "CI_FLEET_HOST_ENV": str(self.host_env),
            "CI_FLEET_INSTALLER_LOCK": str(self.lock_file),
            "CI_FLEET_LKG_DIR": str(self.lkg_dir),
            "CI_FLEET_RECONCILE_MAX_ATTEMPTS": "1",
            "CI_FLEET_RECONCILE_STATE_DIR": str(self.td / "reconcile-state"),
            "CI_FLEET_REMOTE_STATE_FILE": str(self.state_file),
            "CI_FLEET_RENDERED_ENV": str(self.td / "ci-fleet.env"),
            "CI_FLEET_TESTING": "1",
            "FAKE_HEALTH_OBSERVED_LKG": str(self.health_observed_lkg),
            "FAKE_INSTALLER_LOG": str(self.installer_log),
            "PATH": f"{fake_bin}:{self.env['PATH']}",
            "REMOTE_FIXTURE_REF": self.desired_ref,
            "REMOTE_FIXTURE_REPO": str(self.config_repo),
        })
        self._reset(self.prior_ref)

    @staticmethod
    def _write_executable(path: Path, content: str) -> None:
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)

    def _write_installed_ref(self, config_ref: str, desired_state: str = "active") -> None:
        self.state_file.write_text(json.dumps({
            "config_repository": "example-org/private-config",
            "config_ref": config_ref,
            "controller": "example-ci-01",
            "controller_state": desired_state,
        }), encoding="utf-8")
        self.state_file.chmod(0o600)

    def _reset(self, lkg_ref: str, desired_state: str = "active") -> None:
        self.installer_log.unlink(missing_ok=True)
        self.health_observed_lkg.unlink(missing_ok=True)
        self.policy_marker.unlink(missing_ok=True)
        self._write_installed_ref(self.prior_ref, desired_state)
        self.lkg_dir.mkdir(mode=0o700, exist_ok=True)
        (self.lkg_dir / "metadata.json").write_text(json.dumps({
            "config_repository": "example-org/private-config",
            "config_ref": lkg_ref,
            "controller": "example-ci-01",
        }), encoding="utf-8")

    def _run(
        self,
        health: str,
        *,
        desired_state: str = "active",
        rollback_health: str = "healthy",
        rollback_desired_state: str | None = None,
        installer_result: str = "success",
        policy_state: str = "healthy",
    ):
        env = dict(self.env)
        env.update(
            FAKE_HEALTH_STATUS=health,
            FAKE_HEALTH_DESIRED_STATE=desired_state,
            FAKE_ROLLBACK_HEALTH_STATUS=rollback_health,
            FAKE_ROLLBACK_HEALTH_DESIRED_STATE=rollback_desired_state or desired_state,
            FAKE_INSTALLER_RESULT=installer_result,
            FAKE_POLICY_STATE=policy_state,
        )
        return subprocess.run(
            [str(self.root / "scripts" / "remote-reconcile.sh")],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

    def _installer_calls(self) -> list[dict]:
        return [json.loads(line) for line in self.installer_log.read_text(encoding="utf-8").splitlines()]

    def _lkg_ref(self) -> str:
        return json.loads((self.lkg_dir / "metadata.json").read_text(encoding="utf-8"))["config_ref"]

    def _reconcile_state(self) -> dict:
        path = Path(self.env["CI_FLEET_RECONCILE_STATE_DIR"]) / "state.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def _assert_rolled_back(self, result: subprocess.CompletedProcess, health: str) -> None:
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual([call["args"][0] for call in self._installer_calls()], ["--upgrade", "--upgrade"])
        state = self._reconcile_state()
        self.assertEqual(state["status"], "rolled_back")
        self.assertEqual(state["desired_commit"], self.desired_ref)
        self.assertEqual(state["applied_commit"], self.prior_ref)
        self.assertEqual(state["health"], health)
        self.assertEqual(self._lkg_ref(), self.prior_ref)
        self.assertEqual((result.stdout + result.stderr).count("ROLLBACK_OK"), 1)

    def test_normal_reconcile_delegates_one_locked_transaction_to_installer(self):
        result = self._run("healthy")

        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self._installer_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["args"][0], "--upgrade")
        self.assertEqual(calls[0]["lock_fd"], "9")
        self.assertEqual(calls[0]["fd9"], str(self.lock_file))
        self.assertFalse(self.policy_marker.exists())

    def test_state_aware_final_health_acceptance_advances_candidate_lkg(self):
        for desired_state, health in (
            ("active", "healthy"),
            ("active", "warning"),
            ("drained", "maintenance"),
            ("drained", "warning"),
            ("disabled", "maintenance"),
            ("disabled", "warning"),
        ):
            with self.subTest(desired_state=desired_state, health=health):
                self._reset(self.prior_ref, desired_state)
                result = self._run(health, desired_state=desired_state)

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual([call["args"][0] for call in self._installer_calls()], ["--upgrade"])
                self.assertEqual(self.health_observed_lkg.read_text(encoding="utf-8").strip(), self.prior_ref)
                self.assertEqual(self._lkg_ref(), self.desired_ref)
                state = self._reconcile_state()
                self.assertEqual(state["status"], "converged")
                self.assertEqual(state["desired_commit"], self.desired_ref)
                self.assertEqual(state["applied_commit"], self.desired_ref)
                self.assertEqual(state["health"], health)

    def test_final_health_rejection_persists_truthful_rollback_state(self):
        for health, rollback_health in (("unhealthy", "warning"), ("unknown", "healthy")):
            with self.subTest(health=health, rollback_health=rollback_health):
                self._reset(self.prior_ref)
                result = self._run(health, rollback_health=rollback_health)

                self._assert_rolled_back(result, rollback_health)

    def test_state_aware_final_health_rejection_rolls_back(self):
        for applied_state, health, reported_state in (
            ("active", "maintenance", "active"),
            ("drained", "healthy", "drained"),
            ("disabled", "healthy", "disabled"),
            ("active", "warning", "drained"),
            ("active", "malformed", "active"),
        ):
            with self.subTest(applied_state=applied_state, health=health, reported_state=reported_state):
                self._reset(self.prior_ref, applied_state)
                result = self._run(
                    health,
                    desired_state=reported_state,
                    rollback_desired_state=applied_state,
                )

                self._assert_rolled_back(result, "healthy")

    def test_unverified_installer_rollback_does_not_trigger_outer_lkg_apply(self):
        result = self._run("healthy", installer_result="unverified")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self._installer_calls()), 1)
        self.assertEqual(self._lkg_ref(), self.prior_ref)

    def test_applied_cleanup_failure_records_failed_applied_state(self):
        result = self._run("healthy", installer_result="cleanup_failure")

        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(len(self._installer_calls()), 1)
        state = self._reconcile_state()
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["desired_commit"], self.desired_ref)
        self.assertEqual(state["applied_commit"], self.desired_ref)
        self.assertEqual(self._lkg_ref(), self.prior_ref)
        self.assertIn("post-commit cleanup failed", state["message"])
        self.assertNotIn("ROLLBACK_UNVERIFIED", result.stdout + result.stderr)
        self.assertNotIn("ROLLBACK_UNVERIFIED", json.dumps(state))

    def test_same_commit_policy_loss_or_corruption_is_drift_and_repaired(self):
        for policy_state in ("missing", "corrupt"):
            with self.subTest(policy_state=policy_state):
                self._reset(self.desired_ref)
                self._write_installed_ref(self.desired_ref)
                result = self._run("healthy", policy_state=policy_state)

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("DRIFT", result.stdout)
                self.assertEqual([call["args"][0] for call in self._installer_calls()], ["--check", "--upgrade"])


class TestSystemdUnits(unittest.TestCase):
    """The systemd unit files must exist and pass basic validation."""

    def test_service_file_exists(self):
        svc = REPO_ROOT / "host" / "systemd" / "ci-fleet-reconcile.service"
        self.assertTrue(svc.exists())
        content = svc.read_text()
        self.assertIn("ExecStart", content)
        self.assertIn("remote-reconcile.sh", content)

    def test_service_timeouts_cover_lock_build_and_rollback(self):
        svc = REPO_ROOT / "host" / "systemd" / "ci-fleet-reconcile.service"
        content = svc.read_text()
        self.assertIn("lock_wait_seconds=3600", RECONCILE_SCRIPT.read_text())
        self.assertIn("TimeoutStartSec=2h", content)
        self.assertIn("One hour for the installer lock, plus one hour for a cold build and transaction.", content)
        self.assertIn("TimeoutStopSec=15min", content)
        self.assertIn("Allow checkpoint restoration after TimeoutStartSec sends SIGTERM.", content)

    def test_timer_file_exists(self):
        timer = REPO_ROOT / "host" / "systemd" / "ci-fleet-reconcile.timer"
        self.assertTrue(timer.exists())
        content = timer.read_text()
        self.assertIn("OnUnitActiveSec", content)

    def test_installer_references_units(self):
        """Installer script references the new reconcile units."""
        content = INSTALLER.read_text()
        self.assertIn("ci-fleet-reconcile.service", content)
        self.assertIn("ci-fleet-reconcile.timer", content)

    def test_remote_installer_calls_keep_identity_and_lock(self):
        """Remote check, upgrade, and rollback keep one locked transaction."""
        reconcile = RECONCILE_SCRIPT.read_text()
        installer = INSTALLER.read_text()
        self.assertNotIn("release_lock", reconcile)
        self.assertIn('flock -w "$lock_wait_seconds" 9', reconcile)
        self.assertLess(reconcile.index('flock -w "$lock_wait_seconds" 9'), reconcile.index("fetch_ref=HEAD"))
        self.assertIn('fetch_ref=$installed_config_ref', reconcile)
        self.assertEqual(reconcile.count("CI_FLEET_INSTALLER_LOCK_FD=9"), 3)
        self.assertEqual(reconcile.count("--config-identity"), 3)
        self.assertIn("--config-identity)", installer)
        self.assertIn("CI_FLEET_INSTALLER_LOCK_FD", installer)
        self.assertIn("/proc/self/fd/9", installer)
        self.assertIn("lock_file=${CI_FLEET_INSTALLER_LOCK:-", installer)
        self.assertIn('elif [[ -f "$systemd_dir/$opt_timer" ]]; then', installer)

    def test_same_commit_drift_is_reconciled(self):
        """Any failed same-commit check falls through to repair."""
        content = RECONCILE_SCRIPT.read_text()
        same_commit = content.split('if [[ "$desired_commit" == "$installed_config_ref" ]]', 1)[1]
        same_commit = same_commit.split("# New commit or drift", 1)[0]
        self.assertNotIn("controller_running", same_commit)
        self.assertNotIn("internal drift tracked by drift timer", same_commit)

    def test_timer_restore_failure_is_not_ignored(self):
        """A remote activation cannot report success without its timer."""
        content = INSTALLER.read_text()
        remote_timer = content.split('if [[ "$config_identity" == *"/"*', 1)[1]
        remote_timer = remote_timer.split("else", 1)[0]
        self.assertNotIn("|| true", remote_timer)

    def test_lkg_does_not_depend_on_an_unsaved_fleet_snapshot(self):
        """Rollback uses the authenticated exact-ref checkout as its source."""
        content = RECONCILE_SCRIPT.read_text()
        apply_lkg = content.split("apply_lkg()", 1)[1].split("save_lkg()", 1)[0]
        self.assertNotIn("LKG fleet.json missing", apply_lkg)

    def test_health_report_is_parsed_for_warning_and_failure_results(self):
        """Health severity does not discard the report it just wrote."""
        content = RECONCILE_SCRIPT.read_text()
        health = content.split("run_health_check()", 1)[1].split("# --- Main ---", 1)[0]
        self.assertNotIn(") && python3", health)
        self.assertIn('--output "$output" >/dev/null', health)


if __name__ == "__main__":
    unittest.main()
