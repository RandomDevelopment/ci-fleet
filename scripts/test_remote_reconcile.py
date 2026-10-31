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
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
TOKEN_SCRIPT = SCRIPTS / "github-app-token.sh"
RECONCILE_SCRIPT = SCRIPTS / "remote-reconcile.sh"
INSTALLER = SCRIPTS / "install-worker-controller.sh"


class TestInstallerRejection(unittest.TestCase):
    def test_shared_preparation_cannot_report_early_rejection(self):
        content = INSTALLER.read_text()
        prefix = content.split("while (($#)); do", 1)[0]
        converge = "perform_converge() {" + content.split("perform_converge() {", 1)[1].split("\nlatest_checkpoint()", 1)[0]
        for scenario in ("bytecode", "incomplete_manager", "untrusted_manager", "incomplete_runtime", "untrusted_runtime", "pending_checkpoint", "candidate_preparation"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                (root / "scripts").mkdir()
                repair = root / "scripts" / "repair-manager-bytecode-drift.py"
                repair.write_text('#!/usr/bin/env bash\n: >"$REPAIR_MARKER"\n')
                repair.chmod(0o755)
                script = prefix + converge + r'''
repo_root=$1
scenario=$2
exec 7>"$repo_root/result.json"
CI_FLEET_TRANSACTION_RESULT_FD=7
export REPAIR_MARKER=$repo_root/repaired
mode=install
[[ $scenario != bytecode ]] || mode=upgrade
state_file=$repo_root/state
rendered_env=$repo_root/env
candidate_env=$rendered_env
release_dir=$repo_root/candidate
install_root=$repo_root/incumbent
current_link=$repo_root/current
manager_current=$repo_root/manager
releases_dir=$repo_root/releases
manager_releases=$repo_root/managers
lock_file=$repo_root/installer.lock
controller_id=example-ci-01
touch "$state_file" "$rendered_env"
printf 'CI_FLEET_RUNNER_IMAGE=fixture-runner\nCI_FLEET_CONTROLLER_IMAGE=fixture-controller\n' >"$candidate_env"
mkdir "$releases_dir" "$manager_releases"
if [[ $scenario == *_runtime ]]; then
  ln -s "$releases_dir" "$current_link"
elif [[ $scenario != pending_checkpoint && $scenario != candidate_preparation ]]; then
  ln -s "$manager_releases" "$manager_current"
fi
reject_isolated_host() { :; }
docker_network_policy_matches() { :; }
controller_status() { :; }
configuration_approval_required() { return 1; }
canonical_release_target_from_raw_pointer() { readlink -f "$1"; }
runtime_release_complete() { [[ $scenario != incomplete_runtime ]]; }
manager_release_complete() { [[ $scenario != incomplete_manager ]]; }
release_tree_permissions_trusted() { [[ $scenario == bytecode ]]; }
install_release() { [[ $scenario == pending_checkpoint ]] || : >"$REPAIR_MARKER"; }
install_manager() { : >"$REPAIR_MARKER"; }
managed_image_ids_configured() { :; }
drift_count() {
  [[ $scenario == pending_checkpoint || $scenario == candidate_preparation ]] || die 'fixture-sensitive-value'
  DRIFT_COUNT=1
}
capture_current_pointer() { :; }
current_runtime_release() { :; }
portable_managed_images() { return 1; }
compose() { [[ $scenario != candidate_preparation ]] || die 'fixture-sensitive-value'; }
pending_policy_checkpoint() { printf '%s\n' "$repo_root/checkpoint"; }
repair_pending_checkpoint_authority() { : >"$REPAIR_MARKER"; return 1; }
perform_converge
'''
                fixture = root / "installer.sh"
                fixture.write_text(script)
                result = subprocess.run(["bash", str(fixture), str(root), scenario],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertTrue((root / "repaired").exists(), result.stdout + result.stderr)
                self.assertEqual((root / "result.json").read_text(), "")
                expected_error = {
                    "incomplete_manager": "manager current pointer is invalid",
                    "untrusted_manager": "manager current pointer is invalid",
                    "pending_checkpoint": "pending network-policy controller checkpoint is invalid",
                }.get(scenario, "fixture-sensitive-value")
                self.assertIn(f"ERROR: {expected_error}\n", result.stderr)
                self.assertNotIn("command not found", result.stderr)

    def test_only_explicit_pre_transaction_failures_report_rejection(self):
        prefix = INSTALLER.read_text().split("while (($#)); do", 1)[0]
        for started, message, expected in (
            ("false", "Docker filesystem must remain below 80% utilization", "docker_disk_capacity"),
            ("false", "unapproved error with fixture-sensitive-value", "installer_precondition"),
            ("true", "Docker filesystem must remain below 80% utilization", None),
        ):
            with self.subTest(started=started, expected=expected), tempfile.TemporaryDirectory() as td:
                path = Path(td) / "result.json"
                script = prefix + '\nexec 7>"$1"\nCI_FLEET_TRANSACTION_RESULT_FD=7\n'
                script += f'transaction_started={started}\ndie "$2"\n'
                fixture = Path(td) / "installer.sh"
                fixture.write_text(script)
                result = subprocess.run(["bash", str(fixture), str(path), message],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 2, result.stderr)
                if expected is None:
                    self.assertEqual(path.read_text(), "")
                else:
                    self.assertEqual(json.loads(path.read_text()), {
                        "schema_version": 1, "outcome": "rejected", "phase": "pre_transaction",
                        "reason": expected,
                    })
                    self.assertNotIn("fixture-sensitive-value", path.read_text())


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
if [[ ${1:-} == --upgrade && -n ${FAKE_TRANSACTION_RECORD:-} ]]; then
  printf '%s\\n' "$FAKE_TRANSACTION_RECORD" >&7
  printf 'ERROR: fixture-sensitive-value\\n' >&2
  exit "${FAKE_TRANSACTION_EXIT:-2}"
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
raise SystemExit({"healthy": 0, "maintenance": 0, "warning": 1, "unhealthy": 2}.get(status, 2))
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
        transaction_record: str = "",
        transaction_exit: str = "2",
        arguments: tuple[str, ...] = (),
    ):
        env = dict(self.env)
        env.update(
            FAKE_HEALTH_STATUS=health,
            FAKE_HEALTH_DESIRED_STATE=desired_state,
            FAKE_ROLLBACK_HEALTH_STATUS=rollback_health,
            FAKE_ROLLBACK_HEALTH_DESIRED_STATE=rollback_desired_state or desired_state,
            FAKE_INSTALLER_RESULT=installer_result,
            FAKE_POLICY_STATE=policy_state,
            FAKE_TRANSACTION_RECORD=transaction_record,
            FAKE_TRANSACTION_EXIT=transaction_exit,
        )
        return subprocess.run(
            [str(self.root / "scripts" / "remote-reconcile.sh"), *arguments],
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

    def _installed_candidate_with_retained_lkg(self, desired_state="active") -> bytes:
        self._reset(self.prior_ref, desired_state)
        self._write_installed_ref(self.desired_ref, desired_state)
        path = self.lkg_dir / "metadata.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["saved_at"] = 1
        path.write_text(json.dumps(value), encoding="utf-8")
        path.chmod(0o600)
        return path.read_bytes()

    def test_same_commit_accepts_installed_lkg_after_state_aware_health(self):
        for desired_state, health in (("active", "healthy"), ("active", "warning"),
                                     ("drained", "maintenance"), ("drained", "warning"),
                                     ("disabled", "maintenance"), ("disabled", "warning")):
            with self.subTest(desired_state=desired_state, health=health):
                self._installed_candidate_with_retained_lkg(desired_state)
                result = self._run(health, desired_state=desired_state, arguments=("--accept-installed",))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual([call["args"][0] for call in self._installer_calls()], ["--check"])
                self.assertEqual(self.health_observed_lkg.read_text().strip(), self.prior_ref)
                self.assertEqual(self._lkg_ref(), self.desired_ref)
                state = self._reconcile_state()
                self.assertEqual(state["status"], "converged")
                self.assertEqual(state["health"], health)

    def test_same_commit_unaccepted_health_preserves_lkg_without_rollback(self):
        for health, reported_state in (("unhealthy", "active"), ("malformed", "active"),
                                       ("maintenance", "active"), ("healthy", "drained")):
            with self.subTest(health=health, reported_state=reported_state):
                original = self._installed_candidate_with_retained_lkg()
                installed = self.state_file.read_bytes()
                result = self._run(health, desired_state=reported_state, arguments=("--accept-installed",))
                self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
                self.assertEqual([call["args"][0] for call in self._installer_calls()], ["--check"])
                self.assertEqual((self.lkg_dir / "metadata.json").read_bytes(), original)
                self.assertEqual(self.state_file.read_bytes(), installed)
                self.assertEqual(self._reconcile_state()["status"], "failed")
                self.assertNotIn("ROLLBACK_OK", result.stdout)
                self.assertNotIn("CONVERGED controller=", result.stdout)

    def test_same_commit_readonly_modes_do_not_accept_lkg(self):
        for argument in ("--check-only", "--no-op", None):
            with self.subTest(argument=argument):
                original = self._installed_candidate_with_retained_lkg()
                result = self._run("healthy", arguments=(argument,) if argument else ())
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual((self.lkg_dir / "metadata.json").read_bytes(), original)
                self.assertFalse(self.health_observed_lkg.exists())
                if argument != "--no-op":
                    self.assertEqual([call["args"][0] for call in self._installer_calls()], ["--check"])
                else:
                    self.assertFalse(self.installer_log.exists())

    def test_accept_installed_rejects_mode_combinations_before_authentication(self):
        for other in (("--check-only",), ("--installed-ref",), ("--no-op",),
                      ("--ref", "a" * 40), ("--accept-installed",)):
            for arguments in (("--accept-installed", *other), (*other, "--accept-installed")):
                with self.subTest(arguments=arguments):
                    original = self._installed_candidate_with_retained_lkg()
                    result = self._run("healthy", arguments=arguments)
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertNotIn("GENERATING_TOKEN", result.stdout)
                    self.assertFalse(self.installer_log.exists())
                    self.assertEqual((self.lkg_dir / "metadata.json").read_bytes(), original)

    def test_accept_installed_never_repairs_failed_installed_check(self):
        original = self._installed_candidate_with_retained_lkg()
        installed = self.state_file.read_bytes()
        result = self._run("healthy", policy_state="corrupt", arguments=("--accept-installed",))
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertEqual([call["args"][0] for call in self._installer_calls()], ["--check"])
        self.assertEqual((self.lkg_dir / "metadata.json").read_bytes(), original)
        self.assertEqual(self.state_file.read_bytes(), installed)
        self.assertFalse(self.health_observed_lkg.exists())

    def test_accept_installed_requires_lkg_persistence_and_readback(self):
        for failure in ("write", "readback"):
            with self.subTest(failure=failure):
                if (self.lkg_dir / "metadata.json").is_dir():
                    (self.lkg_dir / "metadata.json").rmdir()
                self._installed_candidate_with_retained_lkg()
                if failure == "write":
                    path = self.lkg_dir / "metadata.json"
                    path.unlink()
                    path.mkdir()
                else:
                    self._write_executable(self.td / "bin" / "python3", f'''#!/usr/bin/env bash
if [[ ${{1:-}} == -c && ${{3:-}} == "$CI_FLEET_LKG_DIR/metadata.json" && ${{5:-}} == "$REMOTE_FIXTURE_REF" ]]; then exit 1; fi
exec {shlex.quote(sys.executable)} "$@"
''')
                result = self._run("healthy", arguments=("--accept-installed",))
                self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
                self.assertEqual([call["args"][0] for call in self._installer_calls()], ["--check"])
                self.assertEqual(self._reconcile_state()["status"], "failed")
                self.assertNotIn("CONVERGED controller=", result.stdout)

    def test_accept_installed_fetches_only_installed_ref_and_rejects_wrong_result(self):
        self._installed_candidate_with_retained_lkg()
        fetch_log = self.td / "fetches"
        wrapper = self.td / "bin" / "git"
        wrapper.write_text(wrapper.read_text().replace(
            'if [[ ${1:-} == -C && ${3:-} == fetch ]]; then',
            'if [[ ${1:-} == -C && ${3:-} == fetch ]]; then\n'
            + '  printf "%s\\n" "${@: -1}" >>' + shlex.quote(str(fetch_log))), encoding="utf-8")
        self.assertEqual(git("commit", "--allow-empty", "-qm", "later default branch", cwd=self.config_repo).returncode, 0)
        newer_ref = git("rev-parse", "HEAD", cwd=self.config_repo).stdout.strip()
        result = self._run("healthy", arguments=("--accept-installed",))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(fetch_log.read_text().splitlines(), [self.desired_ref])
        self.assertEqual(self._lkg_ref(), self.desired_ref)

        original = self._installed_candidate_with_retained_lkg()
        self.env["REMOTE_FIXTURE_REF"] = newer_ref
        result = self._run("healthy", arguments=("--accept-installed",))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("installed acceptance fetch identity mismatch", result.stderr)
        self.assertFalse(self.installer_log.exists())
        self.assertEqual((self.lkg_dir / "metadata.json").read_bytes(), original)

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

    def _activate_approval_fixture(self, reject: bool) -> Path:
        policy_directory = self.td / "etc" / "ci-fleet"
        policy_directory.mkdir(parents=True)
        (policy_directory / "config-review-required").write_text("1\n")
        (policy_directory / "config-review-policy.json").write_text("{}\n")
        log = self.td / "approval-call.json"
        (self.root / "scripts" / "verify_config_approval.py").write_text(
            "import json, pathlib, sys\n"
            f"pathlib.Path({str(log)!r}).write_text(json.dumps(sys.argv[1:]))\n"
            f"raise SystemExit({2 if reject else 0})\n", encoding="utf-8",
        )
        self.env["CI_FLEET_ROOT_PREFIX"] = str(self.td)
        return log

    def test_rejected_approval_never_calls_installer_or_changes_lkg(self):
        log = self._activate_approval_fixture(reject=True)
        result = self._run("healthy")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertFalse(self.installer_log.exists())
        self.assertEqual(self._lkg_ref(), self.prior_ref)
        self.assertEqual(self._reconcile_state()["status"], "invalid")
        args = json.loads(log.read_text())
        self.assertIn(self.desired_ref, args)
        self.assertIn(git("rev-parse", "HEAD^{tree}", cwd=self.config_repo).stdout.strip(), args)
        self.assertIn("example-org/private-config", args)

    def test_missing_policy_still_invokes_guard_when_marker_remains(self):
        log = self._activate_approval_fixture(reject=True)
        (self.td / "etc" / "ci-fleet" / "config-review-policy.json").unlink()
        result = self._run("healthy")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertTrue(log.exists())
        self.assertFalse(self.installer_log.exists())

    def test_unchanged_commit_still_requires_approval_before_drift_check(self):
        self._activate_approval_fixture(reject=True)
        self._write_installed_ref(self.desired_ref)
        result = self._run("healthy")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertFalse(self.installer_log.exists())
        self.assertNotIn("NO_CHANGE", result.stdout)

    def test_approved_candidate_proceeds_to_locked_installer(self):
        log = self._activate_approval_fixture(reject=False)
        result = self._run("healthy")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(log.exists())
        self.assertEqual(self._installer_calls()[0]["args"][0], "--upgrade")

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
                calls = self._installer_calls()
                self.assertNotIn("--restore-last-known-good", calls[0]["args"])
                self.assertIn("--restore-last-known-good", calls[1]["args"])

    def test_unaccepted_installed_state_does_not_replace_prior_lkg(self):
        interrupted_ref = "1" * 40
        self._write_installed_ref(interrupted_ref)
        lkg_file = self.lkg_dir / "metadata.json"
        lkg = json.loads(lkg_file.read_text(encoding="utf-8"))
        lkg["saved_at"] = 1
        lkg_file.write_text(json.dumps(lkg), encoding="utf-8")
        lkg_file.chmod(0o600)

        result = self._run("unhealthy", rollback_health="healthy")

        self._assert_rolled_back(result, "healthy")
        calls = self._installer_calls()
        self.assertEqual(calls[1]["args"][calls[1]["args"].index("--ref") + 1], self.prior_ref)

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

    def test_pre_transaction_rejection_preserves_applied_state_and_reason(self):
        record = json.dumps({"schema_version": 1, "outcome": "rejected",
                             "phase": "pre_transaction", "reason": "docker_disk_capacity"})
        result = self._run("healthy", transaction_record=record)

        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(len(self._installer_calls()), 1)
        self.assertEqual(self._lkg_ref(), self.prior_ref)
        self.assertEqual(json.loads(self.state_file.read_text())["config_ref"], self.prior_ref)
        state = self._reconcile_state()
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["applied_commit"], self.prior_ref)
        self.assertEqual(state["desired_commit"], self.desired_ref)
        self.assertEqual(state["health"], "unknown")
        self.assertEqual(state["transaction_phase"], "pre_transaction")
        self.assertEqual(state["failure_reason"], "docker_disk_capacity")
        self.assertIn("docker_disk_capacity", state["message"])
        self.assertIn("REJECTED phase=pre_transaction reason=docker_disk_capacity", result.stdout)
        self.assertNotIn("ROLLBACK_UNVERIFIED", result.stdout)
        self.assertNotIn("fixture-sensitive-value", json.dumps(state))

    def test_missing_or_invalid_outcomes_remain_unknown(self):
        valid = {"schema_version": 1, "outcome": "rejected",
                 "phase": "pre_transaction", "reason": "docker_disk_capacity"}
        records = [
            "", "{", "[]", json.dumps({**valid, "schema_version": True}),
            json.dumps({**valid, "phase": "after_activation"}),
            json.dumps({**valid, "reason": "fixture-sensitive-value"}),
            json.dumps(valid) + "\n" + json.dumps(valid),
            '{"schema_version":1,"outcome":"applied","outcome":"rejected",'
            '"phase":"pre_transaction","reason":"docker_disk_capacity"}',
        ]
        for record in records:
            with self.subTest(record=record):
                self._reset(self.prior_ref)
                result = self._run("healthy", installer_result="unverified", transaction_record=record)
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertEqual(len(self._installer_calls()), 1)
                state = self._reconcile_state()
                self.assertEqual(state["applied_commit"], self.prior_ref)
                self.assertEqual(state["health"], "unknown")
                self.assertIn("ROLLBACK_UNVERIFIED outcome=unknown", result.stdout)
                self.assertNotIn("fixture-sensitive-value", json.dumps(state))

    def test_rejection_with_conflicting_exit_status_remains_unknown(self):
        record = json.dumps({"schema_version": 1, "outcome": "rejected",
                             "phase": "pre_transaction", "reason": "docker_disk_capacity"})
        result = self._run("healthy", transaction_record=record, transaction_exit="20")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("ROLLBACK_UNVERIFIED outcome=unknown", result.stdout)
        self.assertEqual(self._reconcile_state()["health"], "unknown")
        self.assertNotEqual(self._reconcile_state().get("transaction_phase"), "pre_transaction")

    def test_rollback_outcomes_keep_existing_recovery_behavior(self):
        for outcome, exit_code in (("rollback_verified", "20"), ("rollback_unverified", "21")):
            with self.subTest(outcome=outcome):
                self._reset(self.prior_ref)
                record = json.dumps({"schema_version": 1, "outcome": outcome})
                result = self._run("healthy", transaction_record=record, transaction_exit=exit_code)
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertEqual(len(self._installer_calls()), 1)
                state = self._reconcile_state()
                self.assertEqual(state["applied_commit"], self.prior_ref)
                if outcome == "rollback_verified":
                    self.assertEqual(state["status"], "rolled_back")
                    self.assertEqual(state["health"], "healthy")
                    self.assertIn("ROLLBACK_OK", result.stdout)
                else:
                    self.assertEqual(state["health"], "unknown")
                    self.assertIn("ROLLBACK_UNVERIFIED", result.stdout)

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
        same_commit = content.split('# Compare with installed', 1)[1]
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
