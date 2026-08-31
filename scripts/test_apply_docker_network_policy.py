#!/usr/bin/env python3
"""Tests for the Docker daemon network policy apply stage.

Validates the daemon.json rendering helper and the apply script's
transactional behavior: validation before mutation, preservation of
unrelated daemon JSON keys, rollback on failure, and failure evidence
without secrets.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import pwd
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Scripts directory contains both desired_state.py and the apply script.
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from desired_state import (  # noqa: E402  (import after sys.path adjustment)
    DesiredStateError,
    build_rendered_env,
    load_and_validate_config,
    render_docker_daemon_config,
    validate_docker_network_policy,
)


CONFIG_COMMIT = "1" * 40


def config() -> dict:
    return json.loads((ROOT / "templates" / "config-repository" / "fleet.json").read_text(encoding="utf-8"))


def host_values() -> dict[str, str]:
    return {
        "CI_FLEET_GITHUB_APP_CLIENT_ID": "Iv1.EXAMPLE",
        "CI_FLEET_GITHUB_APP_INSTALLATION_ID": "123456",
        "CI_FLEET_GITHUB_APP_PRIVATE_KEY_FILE": "/etc/ci-fleet/secrets/github-app.pem",
        "CI_FLEET_RUNNER_TTL": "6h",
    }


def docker_network_policy() -> dict:
    return {
        "default_address_pools": [
            {"base": "198.51.100.0/24", "size": 29},
            {"base": "203.0.113.0/24", "size": 29},
        ],
        "networks_per_runner": 1,
        "reserve_subnets": 1,
    }


class RenderDaemonConfigTests(unittest.TestCase):
    """RED: render_docker_daemon_config does not exist yet."""

    def test_renders_default_address_pools_from_rendered_env(self) -> None:
        value = config()
        policy = docker_network_policy()
        value["controllers"]["example-ci-01"]["docker_network_policy"] = policy
        capabilities = {"status_reporting_config", "required_status_reporting", "docker_network_policy_config"}
        rendered, _ = build_rendered_env(
            value,
            "example-ci-01",
            host_values(),
            config_repository="example-org/example-fleet-config",
            config_ref=CONFIG_COMMIT,
            docker_gid=998,
            engine_capabilities=capabilities,
        )
        daemon = render_docker_daemon_config(rendered)
        self.assertIn("default-address-pools", daemon)
        pools = daemon["default-address-pools"]
        self.assertEqual(len(pools), 2)
        self.assertEqual(pools[0], {"base": "198.51.100.0/24", "size": 29})
        self.assertEqual(pools[1], {"base": "203.0.113.0/24", "size": 29})

    def test_rejects_negative_pool_count(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be non-negative"):
            render_docker_daemon_config({
                "CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_COUNT": "-1",
            })

    def test_rejects_pool_count_above_repository_limit_before_expansion(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not exceed 64"):
            render_docker_daemon_config({
                "CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_COUNT": "65",
            })

    def test_rejects_pool_entries_outside_declared_count(self) -> None:
        rendered = self._complete_rendered_policy()
        rendered["CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_COUNT"] = "1"

        with self.assertRaisesRegex(ValueError, "indexed fields must match"):
            render_docker_daemon_config(rendered)

    def test_rejects_unknown_rendered_controller_state(self) -> None:
        rendered = self._complete_rendered_policy()
        rendered["CI_FLEET_CONTROLLER_STATE"] = "disable"

        with self.assertRaisesRegex(ValueError, "active, drained, or disabled"):
            render_docker_daemon_config(rendered)

    @staticmethod
    def _complete_rendered_policy() -> dict[str, str]:
        value = config()
        value["controllers"]["example-ci-01"]["docker_network_policy"] = docker_network_policy()
        rendered, _ = build_rendered_env(
            value,
            "example-ci-01",
            host_values(),
            config_repository="example-org/example-fleet-config",
            config_ref=CONFIG_COMMIT,
            docker_gid=998,
            engine_capabilities={"status_reporting_config", "required_status_reporting", "docker_network_policy_config"},
        )
        return rendered

    def test_rejects_negative_rendered_runner_capacity(self) -> None:
        value = config()
        value["controllers"]["example-ci-01"]["docker_network_policy"] = docker_network_policy()
        rendered, _ = build_rendered_env(
            value,
            "example-ci-01",
            host_values(),
            config_repository="example-org/example-fleet-config",
            config_ref=CONFIG_COMMIT,
            docker_gid=998,
            engine_capabilities={"status_reporting_config", "required_status_reporting", "docker_network_policy_config"},
        )
        rendered["CI_FLEET_CONFIGURED_MAX_RUNNERS"] = "-1"

        with self.assertRaisesRegex(ValueError, "positive integer"):
            render_docker_daemon_config(rendered)

    def test_rejects_malformed_pool_index(self) -> None:
        env = {
            "CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_COUNT": "1",
            "CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_0_BASE": "not-a-cidr",
            "CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_0_SIZE": "29",
        }
        with self.assertRaisesRegex(ValueError, "malformed"):
            render_docker_daemon_config(env)

    def test_rejects_mismatched_pool_size(self) -> None:
        env = {
            "CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_COUNT": "1",
            "CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_0_BASE": "198.51.100.0/24",
            "CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_0_SIZE": "not-int",
        }
        with self.assertRaisesRegex(ValueError, "must be an integer"):
            render_docker_daemon_config(env)

    def test_renderer_failure_does_not_disclose_pool_base(self) -> None:
        base = "10.123.45.0/24"
        env = {
            "CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_COUNT": "1",
            "CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_0_BASE": base,
            "CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_0_SIZE": "16",
        }

        with self.assertRaises(ValueError) as raised:
            render_docker_daemon_config(env)

        self.assertNotIn(base, str(raised.exception))

    def test_rejects_ipv6_pool(self) -> None:
        env = {
            "CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_COUNT": "1",
            "CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_0_BASE": "2001:db8::/29",
            "CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_0_SIZE": "29",
        }
        with self.assertRaisesRegex(ValueError, "must be IPv4"):
            render_docker_daemon_config(env)

    def test_not_configured_returns_empty(self) -> None:
        # No network policy rendered -> no pools key.
        daemon = render_docker_daemon_config({})
        self.assertNotIn("default-address-pools", daemon)


class ValidationTests(unittest.TestCase):
    def test_validate_runs_docker_network_policy_suite(self) -> None:
        validate = (SCRIPTS / "validate.sh").read_text(encoding="utf-8")
        self.assertIn("python3 scripts/test_apply_docker_network_policy.py", validate)


class HealthcheckScriptTests(unittest.TestCase):
    def test_env_argument_sources_candidate_rendered_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            installed = root / "etc" / "ci-fleet" / "ci-fleet.env"
            installed.parent.mkdir(parents=True)
            installed.write_text("ENV_MARKER=stale\n", encoding="utf-8")
            candidate = root / "candidate.env"
            candidate.write_text("ENV_MARKER=candidate\n", encoding="utf-8")
            fake_bin = root / "bin"
            fake_bin.mkdir()
            python = fake_bin / "python3"
            python.write_text(
                "#!/usr/bin/env bash\n"
                "[[ ${ENV_MARKER:-} == candidate ]] || exit 1\n",
                encoding="utf-8",
            )
            python.chmod(0o755)
            env = dict(os.environ)
            env.update(
                CI_FLEET_TESTING="1",
                CI_FLEET_ROOT_PREFIX=tmp,
                PATH=f"{fake_bin}:{env['PATH']}",
            )

            result = subprocess.run(
                [str(SCRIPTS / "healthcheck.sh"), "--env", str(candidate)],
                capture_output=True,
                text=True,
                env=env,
                timeout=30,
            )

            self.assertEqual(result.returncode, 0, result.stderr)

    def test_selected_env_does_not_inherit_removed_pool_variables(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidate = root / "candidate.env"
            candidate.write_text(
                f"CI_FLEET_TESTING=1\nCI_FLEET_ROOT_PREFIX={tmp}\n",
                encoding="utf-8",
            )
            fake_bin = root / "bin"
            fake_bin.mkdir()
            python = fake_bin / "python3"
            python.write_text(
                "#!/usr/bin/env bash\n"
                "[[ -z ${CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_COUNT+x} ]] || exit 1\n",
                encoding="utf-8",
            )
            python.chmod(0o755)
            env = dict(os.environ)
            env.update(
                CI_FLEET_TESTING="1",
                CI_FLEET_ROOT_PREFIX=tmp,
                CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_COUNT="2",
                PATH=f"{fake_bin}:{env['PATH']}",
            )

            result = subprocess.run(
                [str(SCRIPTS / "healthcheck.sh"), "--env", str(candidate)],
                capture_output=True,
                text=True,
                env=env,
                timeout=30,
            )

            self.assertEqual(result.returncode, 0, result.stderr)

    def test_selected_env_preserves_health_delivery_suppression(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidate = root / "candidate.env"
            candidate.write_text(
                f"CI_FLEET_TESTING=1\nCI_FLEET_ROOT_PREFIX={tmp}\nCI_FLEET_HEALTH_SUPPRESS_DELIVERY=0\n",
                encoding="utf-8",
            )
            fake_bin = root / "bin"
            fake_bin.mkdir()
            python = fake_bin / "python3"
            python.write_text(
                "#!/usr/bin/env bash\n"
                "[[ ${CI_FLEET_HEALTH_SUPPRESS_DELIVERY:-} == 1 ]]\n",
                encoding="utf-8",
            )
            python.chmod(0o755)
            env = dict(os.environ)
            env.update(
                CI_FLEET_HEALTH_SUPPRESS_DELIVERY="1",
                CI_FLEET_TESTING="1",
                CI_FLEET_ROOT_PREFIX=tmp,
                PATH=f"{fake_bin}:{env['PATH']}",
            )

            result = subprocess.run(
                [str(SCRIPTS / "healthcheck.sh"), "--env", str(candidate)],
                capture_output=True,
                text=True,
                env=env,
                timeout=30,
            )

            self.assertEqual(result.returncode, 0, result.stderr)


class ApplyScriptTests(unittest.TestCase):
    """Integration tests for scripts/apply-docker-network-policy.sh.

    Uses real temp files and injected commands (no Docker daemon required).
    """

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (Path(self.tmp) / "run").mkdir()
        self.daemon_dir = Path(self.tmp) / "etc" / "docker"
        self.daemon_dir.mkdir(parents=True)
        self.installed_env = Path(self.tmp) / "etc" / "ci-fleet" / "ci-fleet.env"
        self.installed_env.parent.mkdir(parents=True)
        self.installed_env.write_text("ENV_GENERATION=prior\n", encoding="utf-8")
        self.installed_env.chmod(0o600)
        self.drain_command = Path(self.tmp) / "drain.sh"
        self.drain_command.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        self.drain_command.chmod(0o755)
        resume_command = Path(self.tmp) / "resume.sh"
        resume_command.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        resume_command.chmod(0o755)

    def _write_daemon(self, content: str) -> Path:
        path = self.daemon_dir / "daemon.json"
        path.write_text(content, encoding="utf-8")
        return path

    def _env(self, **extra: str) -> dict[str, str]:
        env = dict(os.environ)
        env["CI_FLEET_TESTING"] = "1"
        env["CI_FLEET_DOCKER_DAEMON_CONFIG"] = str(self.daemon_dir / "daemon.json")
        env["CI_FLEET_DOCKER_DRAIN_COMMAND"] = str(self.drain_command)
        env["CI_FLEET_DOCKER_RESTART_COMMAND"] = str(Path(self.tmp) / "restart.sh")
        env["CI_FLEET_CONTROLLER_RESUME_COMMAND"] = str(Path(self.tmp) / "resume.sh")
        env["CI_FLEET_DOCKER_NETWORK_PROBE"] = str(Path(self.tmp) / "probe.sh")
        env["CI_FLEET_HEALTH_CHECK_COMMAND"] = str(Path(self.tmp) / "health.sh")
        env["CI_FLEET_ROOT_PREFIX"] = self.tmp
        for key, value in extra.items():
            env[key] = value
        return env

    def _write_env_file(self, rendered: dict[str, str]) -> Path:
        rendered = {
            "CI_FLEET_CAPACITY_BUDGET": "1",
            "CI_FLEET_CONFIGURED_MAX_RUNNERS": "1",
            "CI_FLEET_CONTROLLER_STATE": "active",
            "CI_FLEET_DESIRED_STATE_SCHEMA": "3",
            "CI_FLEET_MAX_RUNNERS": "1",
            "CI_FLEET_MIN_RUNNERS": "0",
            **rendered,
        }
        path = Path(self.tmp) / "ci-fleet.env"
        path.write_text("".join(f"{k}={v}\n" for k, v in sorted(rendered.items())), encoding="utf-8")
        return path

    def _rendered_with_policy(self) -> dict[str, str]:
        value = config()
        value["controllers"]["example-ci-01"]["docker_network_policy"] = docker_network_policy()
        capabilities = {"status_reporting_config", "required_status_reporting", "docker_network_policy_config"}
        rendered, _ = build_rendered_env(
            value,
            "example-ci-01",
            host_values(),
            config_repository="example-org/example-fleet-config",
            config_ref=CONFIG_COMMIT,
            docker_gid=998,
            engine_capabilities=capabilities,
        )
        return rendered

    def _run(self, env_file: str, checkpoint_dir: str | None = None, expected_rc: int = 0) -> subprocess.CompletedProcess:
        script = str(SCRIPTS / "apply-docker-network-policy.sh")
        args = [script]
        if checkpoint_dir is None:
            checkpoint_dir = str(Path(self.tmp) / "checkpoint-default")
        if checkpoint_dir:
            args += ["--checkpoint", checkpoint_dir]
        args += ["--env", env_file]
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            env=self._env(),
            timeout=30,
        )

    def _write_success_commands(self) -> None:
        for name in ("restart.sh", "resume.sh", "probe.sh", "health.sh"):
            command = Path(self.tmp) / name
            command.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            command.chmod(0o755)

    def _assert_raw_candidate_rejected_before_operational_side_effects(
        self,
        rendered: dict[str, str],
        label: str,
    ) -> None:
        daemon = self._write_daemon('{"live-restore":true}\n')
        prior = daemon.read_bytes()
        checkpoint = Path(self.tmp) / f"checkpoint-raw-{label}"
        command_log = Path(self.tmp) / f"commands-raw-{label}.log"
        env_file = Path(self.tmp) / f"candidate-raw-{label}.env"
        env_file.write_text(
            "".join(f"{key}={value}\n" for key, value in sorted(rendered.items())),
            encoding="utf-8",
        )
        for name in ("drain.sh", "restart.sh", "probe.sh", "resume.sh", "health.sh"):
            command = Path(self.tmp) / name
            command.write_text(f"#!/usr/bin/env bash\necho {name} >> {command_log}\n", encoding="utf-8")
            command.chmod(0o755)

        result = self._run(str(env_file), checkpoint_dir=str(checkpoint), expected_rc=1)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "ERROR: daemon policy rendering failed\n")
        self.assertFalse(checkpoint.exists())
        self.assertFalse(command_log.exists())
        self.assertEqual(daemon.read_bytes(), prior)

    def test_rejects_raw_minimum_capacity_before_operational_side_effects(self) -> None:
        for policy_configured in (True, False):
            for state in ("active", "drained", "disabled"):
                for label, value in (("missing", None), ("malformed", "none"), ("nonzero", "1")):
                    with self.subTest(policy_configured=policy_configured, state=state, value=label):
                        rendered = self._rendered_with_policy()
                        if not policy_configured:
                            rendered = {
                                key: item
                                for key, item in rendered.items()
                                if not key.startswith("CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_")
                                and key not in {"CI_FLEET_DOCKER_NETWORKS_PER_RUNNER", "CI_FLEET_DOCKER_NETWORK_RESERVE_SUBNETS"}
                            }
                        rendered["CI_FLEET_CONTROLLER_STATE"] = state
                        rendered["CI_FLEET_MAX_RUNNERS"] = "1" if state == "active" else "0"
                        if value is None:
                            rendered.pop("CI_FLEET_MIN_RUNNERS")
                        else:
                            rendered["CI_FLEET_MIN_RUNNERS"] = value
                        self._assert_raw_candidate_rejected_before_operational_side_effects(
                            rendered,
                            f"minimum-{policy_configured}-{state}-{label}",
                        )

    def test_rejects_raw_capacity_budget_before_operational_side_effects(self) -> None:
        for policy_configured in (True, False):
            for state in ("active", "drained", "disabled"):
                for label, value in (("missing", None), ("malformed", "none"), ("zero", "0"), ("negative", "-1")):
                    with self.subTest(policy_configured=policy_configured, state=state, value=label):
                        rendered = self._rendered_with_policy()
                        if not policy_configured:
                            rendered = {
                                key: item
                                for key, item in rendered.items()
                                if not key.startswith("CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_")
                                and key not in {"CI_FLEET_DOCKER_NETWORKS_PER_RUNNER", "CI_FLEET_DOCKER_NETWORK_RESERVE_SUBNETS"}
                            }
                        rendered["CI_FLEET_CONTROLLER_STATE"] = state
                        rendered["CI_FLEET_MAX_RUNNERS"] = "1" if state == "active" else "0"
                        if value is None:
                            rendered.pop("CI_FLEET_CAPACITY_BUDGET")
                        else:
                            rendered["CI_FLEET_CAPACITY_BUDGET"] = value
                        self._assert_raw_candidate_rejected_before_operational_side_effects(
                            rendered,
                            f"budget-{policy_configured}-{state}-{label}",
                        )
            for state in ("active", "drained"):
                with self.subTest(policy_configured=policy_configured, state=state, value="over-budget"):
                    rendered = self._rendered_with_policy()
                    if not policy_configured:
                        rendered = {
                            key: item
                            for key, item in rendered.items()
                            if not key.startswith("CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_")
                            and key not in {"CI_FLEET_DOCKER_NETWORKS_PER_RUNNER", "CI_FLEET_DOCKER_NETWORK_RESERVE_SUBNETS"}
                        }
                    rendered["CI_FLEET_CONTROLLER_STATE"] = state
                    rendered["CI_FLEET_CONFIGURED_MAX_RUNNERS"] = "3"
                    rendered["CI_FLEET_MAX_RUNNERS"] = "3" if state == "active" else "0"
                    rendered["CI_FLEET_CAPACITY_BUDGET"] = "2"
                    self._assert_raw_candidate_rejected_before_operational_side_effects(
                        rendered,
                        f"budget-{policy_configured}-{state}-over-budget",
                    )

    def test_rejects_raw_disabled_nonpositive_configured_maximum_before_operational_side_effects(self) -> None:
        for policy_configured in (True, False):
            for label, value in (("missing", None), ("malformed", "none"), ("zero", "0"), ("negative", "-1")):
                with self.subTest(policy_configured=policy_configured, value=label):
                    rendered = self._rendered_with_policy()
                    if not policy_configured:
                        rendered = {
                            key: item
                            for key, item in rendered.items()
                            if not key.startswith("CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_")
                            and key not in {"CI_FLEET_DOCKER_NETWORKS_PER_RUNNER", "CI_FLEET_DOCKER_NETWORK_RESERVE_SUBNETS"}
                        }
                    rendered["CI_FLEET_CONTROLLER_STATE"] = "disabled"
                    if value is None:
                        rendered.pop("CI_FLEET_CONFIGURED_MAX_RUNNERS")
                    else:
                        rendered["CI_FLEET_CONFIGURED_MAX_RUNNERS"] = value
                    rendered["CI_FLEET_MAX_RUNNERS"] = "0"
                    self._assert_raw_candidate_rejected_before_operational_side_effects(
                        rendered,
                        f"disabled-configured-maximum-{policy_configured}-{label}",
                    )

    def test_rejects_untrusted_transaction_temp_parent(self) -> None:
        self._write_daemon("{}\n")
        env_file = self._write_env_file(self._rendered_with_policy())
        self._write_success_commands()
        temp_parent = Path(self.tmp) / "untrusted-temp"
        temp_parent.mkdir(mode=0o777)
        temp_parent.chmod(0o777)

        result = subprocess.run(
            [
                str(SCRIPTS / "apply-docker-network-policy.sh"),
                "--checkpoint",
                str(Path(self.tmp) / "checkpoint-untrusted-temp"),
                "--env",
                str(env_file),
            ],
            capture_output=True,
            text=True,
            env=self._env(CI_FLEET_TEMP_DIR=str(temp_parent)),
            timeout=30,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("transaction temp directory must be a trusted root-owned path", result.stderr)

    @unittest.skipUnless(os.geteuid() == 0 and shutil.which("runuser"), "requires root and runuser")
    def test_testing_mode_accepts_default_tmp_as_non_root(self) -> None:
        self._write_daemon("{}\n")
        env_file = self._write_env_file(self._rendered_with_policy())
        self._write_success_commands()
        test_scripts = Path(self.tmp) / "repo" / "scripts"
        test_scripts.mkdir(parents=True)
        for name in ("apply-docker-network-policy.sh", "desired_state.py"):
            shutil.copy2(SCRIPTS / name, test_scripts / name)
        nobody = pwd.getpwnam("nobody")
        for path in [Path(self.tmp), *Path(self.tmp).rglob("*")]:
            os.chown(path, nobody.pw_uid, nobody.pw_gid)

        result = subprocess.run(
            [
                "runuser",
                "-u",
                "nobody",
                "--",
                str(test_scripts / "apply-docker-network-policy.sh"),
                "--checkpoint",
                str(Path(self.tmp) / "checkpoint-non-root"),
                "--env",
                str(env_file),
            ],
            capture_output=True,
            text=True,
            env=self._env(),
            timeout=30,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rejects_installed_env_under_untrusted_directory(self) -> None:
        self._write_daemon("{}\n")
        env_file = self._write_env_file(self._rendered_with_policy())
        self._write_success_commands()
        self.installed_env.parent.chmod(0o777)

        result = self._run(str(env_file))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("installed rendered env must be a trusted root-owned path", result.stderr)

    def test_rejects_daemon_config_inside_recovery_before_side_effects(self) -> None:
        checkpoint = Path(self.tmp) / "checkpoint-contained-daemon"
        checkpoint.mkdir(mode=0o700)
        recovery = checkpoint / "recovery.review"
        recovery.mkdir(mode=0o700)
        daemon = recovery / "daemon.json.before"
        daemon.write_text('{"live-restore":true}\n', encoding="utf-8")
        daemon.chmod(0o600)
        prior = daemon.read_bytes()
        prior_env = recovery / "prior-ci-fleet.env"
        prior_env.write_bytes(self.installed_env.read_bytes())
        prior_env.chmod(0o600)
        env_file = self._write_env_file(self._rendered_with_policy())
        command_log = Path(self.tmp) / "contained-daemon-commands.log"
        for command in (
            self.drain_command,
            Path(self.tmp) / "restart.sh",
            Path(self.tmp) / "probe.sh",
            Path(self.tmp) / "resume.sh",
            Path(self.tmp) / "health.sh",
        ):
            command.write_text(f"#!/usr/bin/env bash\necho side-effect >> {command_log}\n", encoding="utf-8")
            command.chmod(0o755)

        result = subprocess.run(
            [
                str(SCRIPTS / "apply-docker-network-policy.sh"),
                "--checkpoint",
                str(checkpoint),
                "--env",
                str(env_file),
            ],
            capture_output=True,
            text=True,
            env=self._env(CI_FLEET_DOCKER_DAEMON_CONFIG=str(daemon)),
            timeout=30,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "ERROR: daemon config must be outside checkpoint directory\n")
        self.assertFalse(command_log.exists())
        self.assertFalse((checkpoint / "docker-network-policy.json").exists())
        self.assertEqual(daemon.read_bytes(), prior)
        self.assertTrue(recovery.is_dir())

    def test_successful_apply_restarts_probes_resumes_then_checks_health(self) -> None:
        self._write_daemon("{}\n")
        env_file = self._write_env_file(self._rendered_with_policy())
        command_log = Path(self.tmp) / "activation.log"
        self.drain_command.write_text(f"#!/usr/bin/env bash\necho drain >> {command_log}\n", encoding="utf-8")
        for name in ("restart", "probe", "resume", "health"):
            command = Path(self.tmp) / f"{name}.sh"
            command.write_text(f"#!/usr/bin/env bash\necho {name} >> {command_log}\n", encoding="utf-8")
            command.chmod(0o755)

        result = self._run(str(env_file))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(command_log.read_text(encoding="utf-8").splitlines(), ["drain", "restart", "probe", "resume", "health"])

    def test_leading_whitespace_policy_assignments_are_applied(self) -> None:
        daemon = self._write_daemon("{}\n")
        rendered = self._rendered_with_policy()
        env_file = Path(self.tmp) / "leading-whitespace-policy.env"
        env_file.write_text(
            "".join(
                f"{'  ' if key.startswith('CI_FLEET_DOCKER_') else ''}{key}={value}\n"
                for key, value in sorted(rendered.items())
            ),
            encoding="utf-8",
        )
        self._write_success_commands()

        result = self._run(str(env_file))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("default-address-pools", json.loads(daemon.read_text(encoding="utf-8")))

    def test_effective_capacity_mismatch_is_rejected_before_drain(self) -> None:
        self._write_daemon("{}\n")
        self._write_success_commands()
        for configured in (True, False):
            with self.subTest(configured=configured):
                rendered = self._rendered_with_policy()
                if not configured:
                    rendered = {
                        key: value
                        for key, value in rendered.items()
                        if not key.startswith("CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_")
                        and key not in {"CI_FLEET_DOCKER_NETWORKS_PER_RUNNER", "CI_FLEET_DOCKER_NETWORK_RESERVE_SUBNETS"}
                    }
                rendered["CI_FLEET_MAX_RUNNERS"] = "1000"
                env_file = self._write_env_file(rendered)
                drain_marker = Path(self.tmp) / f"capacity-mismatch-{configured}.marker"
                self.drain_command.write_text(f"#!/usr/bin/env bash\ntouch {drain_marker}\n", encoding="utf-8")

                result = self._run(str(env_file), checkpoint_dir=str(Path(self.tmp) / f"checkpoint-capacity-{configured}"))

                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stderr, "ERROR: daemon policy rendering failed\n")
                self.assertFalse(drain_marker.exists())

    def test_rejects_missing_malformed_or_non_v3_schema_before_checkpoint_or_drain(self) -> None:
        self._write_daemon("{}\n")
        self._write_success_commands()
        for configured in (True, False):
            for label, schema in (("missing", None), ("malformed", "three"), ("non-v3", "2")):
                with self.subTest(configured=configured, schema=label):
                    checkpoint = Path(self.tmp) / f"checkpoint-schema-{configured}-{label}"
                    drain_marker = Path(self.tmp) / f"drain-schema-{configured}-{label}.marker"
                    self.drain_command.write_text(
                        f"#!/usr/bin/env bash\ntouch {drain_marker}\n",
                        encoding="utf-8",
                    )
                    rendered = self._rendered_with_policy() if configured else {"CI_FLEET_INSTANCE": "example-ci-01"}
                    if schema is None:
                        rendered.pop("CI_FLEET_DESIRED_STATE_SCHEMA", None)
                        env_file = Path(self.tmp) / f"schema-{configured}-{label}.env"
                        env_file.write_text(
                            "".join(f"{key}={value}\n" for key, value in sorted(rendered.items())),
                            encoding="utf-8",
                        )
                    else:
                        rendered["CI_FLEET_DESIRED_STATE_SCHEMA"] = schema
                        env_file = self._write_env_file(rendered)

                    result = self._run(str(env_file), checkpoint_dir=str(checkpoint))

                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("schema 3", result.stderr)
                    self.assertFalse(checkpoint.exists())
                    self.assertFalse(drain_marker.exists())

    def test_failed_apply_restarts_resumes_then_checks_rollback_health(self) -> None:
        self._write_daemon("{}\n")
        env_file = self._write_env_file(self._rendered_with_policy())
        command_log = Path(self.tmp) / "rollback-activation.log"
        self.drain_command.write_text(f"#!/usr/bin/env bash\necho drain >> {command_log}\n", encoding="utf-8")
        for name in ("restart", "resume", "health"):
            command = Path(self.tmp) / f"{name}.sh"
            command.write_text(f"#!/usr/bin/env bash\necho {name} >> {command_log}\n", encoding="utf-8")
            command.chmod(0o755)
        probe = Path(self.tmp) / "probe.sh"
        probe.write_text(f"#!/usr/bin/env bash\necho probe >> {command_log}\nexit 1\n", encoding="utf-8")
        probe.chmod(0o755)

        result = self._run(str(env_file), expected_rc=1)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(command_log.read_text(encoding="utf-8").splitlines(), ["drain", "restart", "probe", "restart", "resume", "health"])

    def test_apply_health_failure_redrains_before_rollback_restart(self) -> None:
        self._write_daemon("{}\n")
        env_file = self._write_env_file(self._rendered_with_policy())
        command_log = Path(self.tmp) / "apply-health-rollback.log"
        self.drain_command.write_text(f"#!/usr/bin/env bash\necho drain >> {command_log}\n", encoding="utf-8")
        for name in ("restart", "probe", "resume"):
            command = Path(self.tmp) / f"{name}.sh"
            command.write_text(f"#!/usr/bin/env bash\necho {name} >> {command_log}\n", encoding="utf-8")
            command.chmod(0o755)
        health = Path(self.tmp) / "health.sh"
        health.write_text(
            "#!/usr/bin/env bash\n"
            f"echo health >> {command_log}\n"
            'grep -Fqx "ENV_GENERATION=prior" "$2" || exit 2\n',
            encoding="utf-8",
        )
        health.chmod(0o755)

        result = self._run(str(env_file), expected_rc=1)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            command_log.read_text(encoding="utf-8").splitlines(),
            ["drain", "restart", "probe", "resume", "health", "drain", "restart", "resume", "health"],
        )

    def test_failed_apply_uses_pretransaction_installed_env_for_rollback(self) -> None:
        self._write_daemon("{}\n")
        env_file = self._write_env_file(self._rendered_with_policy())
        self.drain_command.write_text(
            f"#!/usr/bin/env bash\nprintf 'ENV_GENERATION=changed-after-drain\\n' > {self.installed_env}\n",
            encoding="utf-8",
        )
        for name in ("restart.sh",):
            command = Path(self.tmp) / name
            command.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            command.chmod(0o755)
        probe = Path(self.tmp) / "probe.sh"
        probe.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
        probe.chmod(0o755)
        rollback_env_log = Path(self.tmp) / "rollback-env.log"
        for name in ("resume", "health"):
            command = Path(self.tmp) / f"{name}.sh"
            command.write_text(
                "#!/usr/bin/env bash\n"
                f"printf '%s\\n' \"${{2:-missing}}\" >> {rollback_env_log}\n"
                '[[ $1 == --env && -f $2 ]] || exit 2\n'
                'grep -Fqx "ENV_GENERATION=prior" "$2" || exit 2\n',
                encoding="utf-8",
            )
            command.chmod(0o755)

        result = self._run(str(env_file), expected_rc=1)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("prior daemon.json restored", result.stderr)
        self.assertNotIn("rollback verification failed", result.stderr)
        rollback_envs = rollback_env_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(rollback_envs), 2)
        self.assertEqual(len(set(rollback_envs)), 1)
        self.assertNotEqual(rollback_envs[0], str(self.installed_env))

    def test_checkpoint_state_failure_stops_before_drain(self) -> None:
        daemon = self._write_daemon("{}\n")
        prior = daemon.read_bytes()
        checkpoint = Path(self.tmp) / "checkpoint-post-drain-failure"
        env_file = self._write_env_file(self._rendered_with_policy())
        command_log = Path(self.tmp) / "post-drain-failure.log"
        self.drain_command.write_text(f"#!/usr/bin/env bash\necho drain >> {command_log}\n", encoding="utf-8")
        for name in ("restart", "resume", "health"):
            command = Path(self.tmp) / f"{name}.sh"
            command.write_text(f"#!/usr/bin/env bash\necho {name} >> {command_log}\n", encoding="utf-8")
            command.chmod(0o755)
        probe = Path(self.tmp) / "probe.sh"
        probe.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        probe.chmod(0o755)
        fake_bin = Path(self.tmp) / "post-drain-bin"
        fake_bin.mkdir()
        python = fake_bin / "python3"
        python.write_text(
            "#!/usr/bin/env bash\n"
            "[[ ${2:-} != /proc/self/fd/*/docker-network-policy.json ]] || exit 1\n"
            f"exec {shutil.which('python3')} \"$@\"\n",
            encoding="utf-8",
        )
        python.chmod(0o755)

        result = subprocess.run(
            [str(SCRIPTS / "apply-docker-network-policy.sh"), "--checkpoint", str(checkpoint), "--env", str(env_file)],
            capture_output=True,
            text=True,
            env=self._env(PATH=f"{fake_bin}:{os.environ['PATH']}"),
            timeout=30,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(daemon.read_bytes(), prior)
        self.assertFalse(command_log.exists())

    def test_drain_timeout_resumes_prior_controller_and_preserves_failure(self) -> None:
        self._write_daemon("{}\n")
        env_file = self._write_env_file(self._rendered_with_policy())
        command_log = Path(self.tmp) / "failed-drain.log"
        self.drain_command.write_text(
            f"#!/usr/bin/env bash\necho drain >> {command_log}\nsleep 10\n",
            encoding="utf-8",
        )
        resume = Path(self.tmp) / "resume.sh"
        resume.write_text(
            "#!/usr/bin/env bash\n"
            f"echo resume >> {command_log}\n"
            'grep -Fqx "ENV_GENERATION=prior" "$2" || exit 2\n'
            "exit 1\n",
            encoding="utf-8",
        )
        resume.chmod(0o755)
        for name in ("restart.sh", "probe.sh", "health.sh"):
            command = Path(self.tmp) / name
            command.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            command.chmod(0o755)

        result = subprocess.run(
            [
                str(SCRIPTS / "apply-docker-network-policy.sh"),
                "--checkpoint",
                str(Path(self.tmp) / "checkpoint-failed-drain"),
                "--env",
                str(env_file),
            ],
            capture_output=True,
            text=True,
            env=self._env(CI_FLEET_COMMAND_TIMEOUT_SECONDS="1"),
            timeout=15,
        )

        self.assertEqual(result.returncode, 124)
        self.assertEqual(command_log.read_text(encoding="utf-8").splitlines(), ["drain", "resume"])
        self.assertEqual(
            result.stderr,
            "ERROR: drain command failed before network-policy apply; controller resume command failed\n",
        )

    def test_daemon_change_during_drain_aborts_and_preserves_update(self) -> None:
        daemon = self._write_daemon('{"bip":"172.17.0.1/16"}\n')
        changed = b'{"bip":"172.17.0.1/16","log-level":"debug"}\n'
        env_file = self._write_env_file(self._rendered_with_policy())
        checkpoint = Path(self.tmp) / "checkpoint-daemon-conflict"
        command_log = Path(self.tmp) / "daemon-conflict.log"
        self.drain_command.write_text(
            "#!/usr/bin/env bash\n"
            f"echo drain >> {command_log}\n"
            f"printf '%s\\n' '{{\"bip\":\"172.17.0.1/16\",\"log-level\":\"debug\"}}' > {daemon}\n",
            encoding="utf-8",
        )
        for name in ("restart", "probe", "resume", "health"):
            command = Path(self.tmp) / f"{name}.sh"
            command.write_text(f"#!/usr/bin/env bash\necho {name} >> {command_log}\n", encoding="utf-8")
            command.chmod(0o755)

        result = self._run(str(env_file), checkpoint_dir=str(checkpoint))

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "ERROR: daemon.json changed during network-policy apply\n")
        self.assertEqual(command_log.read_text(encoding="utf-8").splitlines(), ["drain", "resume", "health"])
        self.assertEqual(daemon.read_bytes(), changed)
        self.assertFalse((checkpoint / "docker-network-policy.json").exists())

    def test_apply_stages_from_the_conflict_detection_snapshot(self) -> None:
        daemon = self._write_daemon('{"live-restore":true}\n')
        env_file = self._write_env_file(self._rendered_with_policy())
        self._write_success_commands()
        fake_bin = Path(self.tmp) / "snapshot-cp-bin"
        fake_bin.mkdir()
        cp = fake_bin / "cp"
        cp.write_text(
            "#!/usr/bin/env bash\n"
            f"if [[ $2 == {daemon} ]]; then printf '%s\\n' '{{\"live-restore\":true,\"log-level\":\"debug\"}}' > {daemon}; fi\n"
            f"exec {shutil.which('cp')} \"$@\"\n",
            encoding="utf-8",
        )
        cp.chmod(0o755)

        result = subprocess.run(
            [
                str(SCRIPTS / "apply-docker-network-policy.sh"),
                "--checkpoint",
                str(Path(self.tmp) / "checkpoint-snapshot-stage"),
                "--env",
                str(env_file),
            ],
            capture_output=True,
            text=True,
            env=self._env(PATH=f"{fake_bin}:{os.environ['PATH']}"),
            timeout=30,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(daemon.read_text(encoding="utf-8"))["log-level"], "debug")

    def test_post_drain_conflict_retains_recovery_when_prior_resume_fails(self) -> None:
        daemon = self._write_daemon('{"live-restore":true}\n')
        checkpoint = Path(self.tmp) / "checkpoint-post-drain-recovery"
        self._write_success_commands()
        prior_env = self._write_env_file(self._rendered_with_policy())
        applied = self._run(str(prior_env), checkpoint_dir=str(checkpoint))
        self.assertEqual(applied.returncode, 0, applied.stderr)
        prior_env_bytes = prior_env.read_bytes()
        self.installed_env.write_bytes(prior_env_bytes)
        prior_daemon = daemon.read_bytes()
        candidate = self._rendered_with_policy()
        candidate["CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_0_BASE"] = "192.0.2.0/24"
        candidate_env = self._write_env_file(candidate)
        self.drain_command.write_text(
            f"#!/usr/bin/env bash\nprintf '%s\\n' '{{\"debug\":true}}' > {daemon}\n",
            encoding="utf-8",
        )
        resume = Path(self.tmp) / "resume.sh"
        resume.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
        resume.chmod(0o755)

        result = self._run(str(candidate_env), checkpoint_dir=str(checkpoint))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("controller resume command failed", result.stderr)
        recoveries = list(checkpoint.glob("recovery.*"))
        self.assertEqual(len(recoveries), 1)
        self.assertEqual((recoveries[0] / "daemon.json.before").read_bytes(), prior_daemon)
        self.assertEqual((recoveries[0] / "prior-ci-fleet.env").read_bytes(), prior_env_bytes)

    def test_daemon_metadata_change_during_drain_aborts(self) -> None:
        alternate_gid = 1 if os.geteuid() == 0 else next((gid for gid in os.getgroups() if gid != os.getgid()), None)
        cases = [("mode", "chmod 600")]
        if alternate_gid is not None:
            cases.append(("group", f"chgrp {alternate_gid}"))
        for field, command in cases:
            with self.subTest(field=field):
                daemon = self._write_daemon('{"bip":"172.17.0.1/16"}\n')
                daemon.chmod(0o644)
                checkpoint = Path(self.tmp) / f"checkpoint-daemon-{field}-conflict"
                self.drain_command.write_text(
                    f"#!/usr/bin/env bash\n{command} {daemon}\n",
                    encoding="utf-8",
                )
                self._write_success_commands()
                env_file = self._write_env_file(self._rendered_with_policy())

                result = self._run(str(env_file), checkpoint_dir=str(checkpoint))

                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stderr, "ERROR: daemon.json changed during network-policy apply\n")
                if field == "mode":
                    self.assertEqual(daemon.stat().st_mode & 0o777, 0o600)
                else:
                    self.assertEqual(daemon.stat().st_gid, alternate_gid)

    def test_failed_managed_reapply_restores_prior_verified_generation(self) -> None:
        daemon = self._write_daemon("{}\n")
        checkpoint = Path(self.tmp) / "checkpoint-reapply-generation"
        self._write_success_commands()
        prior_env = self._write_env_file(self._rendered_with_policy())
        applied = self._run(str(prior_env), checkpoint_dir=str(checkpoint))
        self.assertEqual(applied.returncode, 0, applied.stderr)
        state_file = checkpoint / "docker-network-policy.json"
        prior_generation = json.loads(state_file.read_text(encoding="utf-8"))["verified_generation"]
        self.installed_env.write_bytes(prior_env.read_bytes())
        candidate = self._rendered_with_policy()
        candidate["CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_0_BASE"] = "192.0.2.0/24"
        candidate_env = Path(self.tmp) / "candidate-reapply.env"
        candidate_env.write_text("".join(f"{key}={value}\n" for key, value in sorted(candidate.items())), encoding="utf-8")
        probe = Path(self.tmp) / "probe.sh"
        probe.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
        probe.chmod(0o755)

        result = self._run(str(candidate_env), checkpoint_dir=str(checkpoint), expected_rc=1)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(state_file.read_text(encoding="utf-8"))["verified_generation"], prior_generation)
        self.assertEqual(hashlib.sha256(daemon.read_bytes()).hexdigest(), prior_generation)

    def test_managed_reapply_marks_generation_pending_before_drain(self) -> None:
        self._write_daemon("{}\n")
        checkpoint = Path(self.tmp) / "checkpoint-reapply-pending-before-drain"
        self._write_success_commands()
        prior_env = self._write_env_file(self._rendered_with_policy())
        applied = self._run(str(prior_env), checkpoint_dir=str(checkpoint))
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.installed_env.write_bytes(prior_env.read_bytes())
        state_file = checkpoint / "docker-network-policy.json"
        self.drain_command.write_text(
            "#!/usr/bin/env bash\n"
            f"grep -q '\"verified_generation\": null' {state_file}\n",
            encoding="utf-8",
        )
        candidate = self._rendered_with_policy()
        candidate["CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_0_BASE"] = "192.0.2.0/24"
        candidate_env = self._write_env_file(candidate)

        result = self._run(str(candidate_env), checkpoint_dir=str(checkpoint))

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_managed_reapply_recovers_when_daemon_snapshot_is_absent(self) -> None:
        daemon = self.daemon_dir / "daemon.json"
        checkpoint = Path(self.tmp) / "checkpoint-reapply-absent-daemon"
        self._write_success_commands()
        prior_env = self._write_env_file(self._rendered_with_policy())
        applied = self._run(str(prior_env), checkpoint_dir=str(checkpoint))
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.installed_env.write_bytes(prior_env.read_bytes())
        daemon.unlink()
        candidate = self._rendered_with_policy()
        candidate["CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_0_BASE"] = "192.0.2.0/24"
        candidate_env = Path(self.tmp) / "candidate-reapply-absent-daemon.env"
        candidate_env.write_text("".join(f"{key}={value}\n" for key, value in sorted(candidate.items())), encoding="utf-8")

        result = self._run(str(candidate_env), checkpoint_dir=str(checkpoint))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(daemon.is_file())
        self.assertFalse(list(checkpoint.glob("recovery.*")))

    def test_failed_managed_reapply_leaves_generation_unverified_when_rollback_health_fails(self) -> None:
        self._write_daemon("{}\n")
        checkpoint = Path(self.tmp) / "checkpoint-failed-rollback-generation"
        self._write_success_commands()
        prior_env = self._write_env_file(self._rendered_with_policy())
        applied = self._run(str(prior_env), checkpoint_dir=str(checkpoint))
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.installed_env.write_bytes(prior_env.read_bytes())
        candidate = self._rendered_with_policy()
        candidate["CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_0_BASE"] = "192.0.2.0/24"
        candidate_env = Path(self.tmp) / "candidate-failed-rollback.env"
        candidate_env.write_text("".join(f"{key}={value}\n" for key, value in sorted(candidate.items())), encoding="utf-8")
        (Path(self.tmp) / "probe.sh").write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
        (Path(self.tmp) / "probe.sh").chmod(0o755)
        (Path(self.tmp) / "health.sh").write_text("#!/usr/bin/env bash\nexit 2\n", encoding="utf-8")
        (Path(self.tmp) / "health.sh").chmod(0o755)

        result = self._run(str(candidate_env), checkpoint_dir=str(checkpoint), expected_rc=1)

        self.assertNotEqual(result.returncode, 0)
        state = json.loads((checkpoint / "docker-network-policy.json").read_text(encoding="utf-8"))
        self.assertIsNone(state["verified_generation"])

    def test_failed_unverified_retry_restores_durable_baseline(self) -> None:
        rendered = self._rendered_with_policy()
        daemon = self._write_daemon(json.dumps({
            "default-address-pools": render_docker_daemon_config(rendered)["default-address-pools"],
            "live-restore": True,
        }))
        checkpoint = Path(self.tmp) / "checkpoint-unverified-retry"
        checkpoint.mkdir(mode=0o700)
        state_file = checkpoint / "docker-network-policy.json"
        state_file.write_text(
            json.dumps({
                "managed": True,
                "prior_default_address_pools": None,
                "prior_default_address_pools_present": False,
                "prior_mode": "600",
                "prior_present": True,
                "verified_generation": None,
            }),
            encoding="utf-8",
        )
        state_file.chmod(0o600)
        candidate = dict(rendered)
        candidate["CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_0_BASE"] = "192.0.2.0/24"
        env_file = self._write_env_file(candidate)
        self._write_success_commands()
        probe = Path(self.tmp) / "probe.sh"
        probe.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
        probe.chmod(0o755)

        result = self._run(str(env_file), checkpoint_dir=str(checkpoint))

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(daemon.read_text(encoding="utf-8")), {"live-restore": True})

    def test_failed_interrupted_reapply_retry_restores_immediate_snapshot(self) -> None:
        daemon = self._write_daemon('{"live-restore":true}\n')
        checkpoint = Path(self.tmp) / "checkpoint-interrupted-reapply"
        self._write_success_commands()
        policy_a = self._rendered_with_policy()
        env_a = self._write_env_file(policy_a)
        applied = self._run(str(env_a), checkpoint_dir=str(checkpoint))
        self.assertEqual(applied.returncode, 0, applied.stderr)
        daemon_a = daemon.read_bytes()
        self.installed_env.write_bytes(env_a.read_bytes())
        state_file = checkpoint / "docker-network-policy.json"
        state = json.loads(state_file.read_text(encoding="utf-8"))
        state["verified_generation"] = None
        state_file.write_text(json.dumps(state), encoding="utf-8")
        state_file.chmod(0o600)
        recovery = checkpoint / "recovery.interrupted"
        recovery.mkdir(mode=0o700)
        (recovery / "daemon.json.before").write_bytes(daemon_a)
        (recovery / "prior-ci-fleet.env").write_bytes(env_a.read_bytes())
        for path in recovery.iterdir():
            path.chmod(0o600)
        policy_b = dict(policy_a)
        policy_b["CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_0_BASE"] = "192.0.2.0/24"
        daemon.write_text(json.dumps(render_docker_daemon_config(policy_b)), encoding="utf-8")
        policy_c = dict(policy_a)
        policy_c["CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_0_BASE"] = "192.0.3.0/24"
        env_c = self._write_env_file(policy_c)
        (Path(self.tmp) / "probe.sh").write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
        (Path(self.tmp) / "probe.sh").chmod(0o755)

        result = self._run(str(env_c), checkpoint_dir=str(checkpoint))

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(daemon.read_bytes(), daemon_a)

    def test_failed_unverified_retry_preserves_new_unrelated_keys_after_absent_baseline(self) -> None:
        rendered = self._rendered_with_policy()
        daemon = self._write_daemon(json.dumps({
            **render_docker_daemon_config(rendered),
            "live-restore": True,
        }))
        checkpoint = Path(self.tmp) / "checkpoint-unverified-absent-baseline"
        checkpoint.mkdir(mode=0o700)
        state_file = checkpoint / "docker-network-policy.json"
        state_file.write_text(
            json.dumps({
                "managed": True,
                "prior_default_address_pools": None,
                "prior_default_address_pools_present": False,
                "prior_mode": None,
                "prior_present": False,
                "verified_generation": None,
            }),
            encoding="utf-8",
        )
        state_file.chmod(0o600)
        candidate = dict(rendered)
        candidate["CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_0_BASE"] = "192.0.2.0/24"
        env_file = self._write_env_file(candidate)
        self._write_success_commands()
        probe = Path(self.tmp) / "probe.sh"
        probe.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
        probe.chmod(0o755)

        result = self._run(str(env_file), checkpoint_dir=str(checkpoint))

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(daemon.read_text(encoding="utf-8")), {"live-restore": True})

    def test_unverified_retry_preserves_current_snapshot_presence(self) -> None:
        rendered = self._rendered_with_policy()
        daemon = self._write_daemon(json.dumps(render_docker_daemon_config(rendered)))
        checkpoint = Path(self.tmp) / "checkpoint-unverified-current-present"
        checkpoint.mkdir(mode=0o700)
        state_file = checkpoint / "docker-network-policy.json"
        state_file.write_text(
            json.dumps({
                "managed": True,
                "prior_default_address_pools": None,
                "prior_default_address_pools_present": False,
                "prior_mode": None,
                "prior_present": False,
                "verified_generation": None,
            }),
            encoding="utf-8",
        )
        state_file.chmod(0o600)
        self._write_success_commands()
        env_file = self._write_env_file(rendered)

        result = self._run(str(env_file), checkpoint_dir=str(checkpoint))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"NETWORK_POLICY_APPLIED daemon_config={daemon}\n")
        state = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertEqual(state["verified_generation"], hashlib.sha256(daemon.read_bytes()).hexdigest())

    def test_policy_apply_requires_checkpoint_before_drain_or_mutation(self) -> None:
        prior = b'{"bip":"172.17.0.1/16"}\n'
        daemon = self.daemon_dir / "daemon.json"
        daemon.write_bytes(prior)
        drain_marker = Path(self.tmp) / "drain.marker"
        self.drain_command.write_text(f"#!/usr/bin/env bash\ntouch {drain_marker}\n", encoding="utf-8")
        self._write_success_commands()
        env_file = self._write_env_file(self._rendered_with_policy())

        result = self._run(str(env_file), checkpoint_dir="")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--checkpoint is required", result.stderr)
        self.assertEqual(daemon.read_bytes(), prior)
        self.assertFalse(drain_marker.exists())

    def test_policy_removal_requires_checkpoint_before_noop(self) -> None:
        prior = b'{"default-address-pools":[{"base":"192.0.2.0/24","size":28}]}\n'
        daemon = self._write_daemon(prior.decode())
        drain_marker = Path(self.tmp) / "removal-without-checkpoint-drain.marker"
        self.drain_command.write_text(f"#!/usr/bin/env bash\ntouch {drain_marker}\n", encoding="utf-8")
        self._write_success_commands()
        env_file = self._write_env_file({"CI_FLEET_INSTANCE": "example-ci-01"})

        result = self._run(str(env_file), checkpoint_dir="")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--checkpoint is required", result.stderr)
        self.assertNotIn("NETWORK_POLICY_NOOP", result.stdout)
        self.assertEqual(daemon.read_bytes(), prior)
        self.assertFalse(drain_marker.exists())

    def test_partial_no_policy_snapshot_is_rejected_before_removal(self) -> None:
        daemon = self._write_daemon('{"default-address-pools":[{"base":"192.0.2.0/24","size":28}]}\n')
        prior = daemon.read_bytes()
        drain_marker = Path(self.tmp) / "partial-policy-drain.marker"
        self.drain_command.write_text(f"#!/usr/bin/env bash\ntouch {drain_marker}\n", encoding="utf-8")
        self._write_success_commands()
        env_file = self._write_env_file({"CI_FLEET_DOCKER_NETWORKS_PER_RUNNER": "1"})

        result = self._run(str(env_file))

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "ERROR: daemon policy rendering failed\n")
        self.assertEqual(daemon.read_bytes(), prior)
        self.assertFalse(drain_marker.exists())

    def test_creates_and_validates_checkpoint_before_drain(self) -> None:
        self._write_daemon("{}\n")
        self._write_success_commands()
        checkpoint = Path(self.tmp) / "checkpoint-before-drain"
        state_file = checkpoint / "docker-network-policy.json"
        drain_marker = Path(self.tmp) / "checkpoint-before-drain.marker"
        self.drain_command.write_text(
            "#!/usr/bin/env bash\n"
            f"[[ -d {checkpoint} && ! -L {checkpoint} ]] || exit 1\n"
            f"[[ -s {state_file} ]] || exit 1\n"
            f"touch {drain_marker}\n",
            encoding="utf-8",
        )
        env_file = self._write_env_file(self._rendered_with_policy())

        result = self._run(str(env_file), checkpoint_dir=str(checkpoint))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(drain_marker.exists())
        self.assertEqual(checkpoint.stat().st_mode & 0o777, 0o700)

    def test_checkpoint_creation_fsyncs_parent_directory(self) -> None:
        self._write_daemon("{}\n")
        self._write_success_commands()
        checkpoint = Path(self.tmp) / "checkpoint-parent-fsync"
        audit_log = Path(self.tmp) / "checkpoint-parent-fsync.log"
        audit_dir = Path(self.tmp) / "checkpoint-parent-fsync-audit"
        audit_dir.mkdir()
        (audit_dir / "sitecustomize.py").write_text(
            "import os\n"
            "_mkdir = os.mkdir\n"
            "_fsync = os.fsync\n"
            "_log = os.environ['FSYNC_AUDIT_LOG']\n"
            "def record(value):\n"
            "    with open(_log, 'a', encoding='utf-8') as handle: handle.write(value + '\\n')\n"
            "def mkdir(path, mode=0o777, *, dir_fd=None):\n"
            "    result = _mkdir(path, mode, dir_fd=dir_fd)\n"
            "    parent = os.path.realpath(f'/proc/self/fd/{dir_fd}') if dir_fd is not None else os.getcwd()\n"
            "    created = os.path.realpath(os.path.join(parent, path))\n"
            "    if created == os.environ['CHECKPOINT']: record('M ' + created)\n"
            "    return result\n"
            "def fsync(fd):\n"
            "    record('F ' + os.path.realpath(f'/proc/self/fd/{fd}'))\n"
            "    return _fsync(fd)\n"
            "os.mkdir = mkdir\n"
            "os.fsync = fsync\n",
            encoding="utf-8",
        )
        env_file = self._write_env_file(self._rendered_with_policy())

        result = subprocess.run(
            [str(SCRIPTS / "apply-docker-network-policy.sh"), "--checkpoint", str(checkpoint), "--env", str(env_file)],
            capture_output=True,
            text=True,
            env=self._env(
                PYTHONPATH=str(audit_dir),
                FSYNC_AUDIT_LOG=str(audit_log),
                CHECKPOINT=str(checkpoint),
            ),
            timeout=30,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        events = audit_log.read_text(encoding="utf-8").splitlines()
        mkdir = events.index(f"M {checkpoint}")
        self.assertIn(f"F {checkpoint.parent}", events[mkdir + 1 :])

    def test_checkpoint_swap_between_check_and_state_write_does_not_redirect_state(self) -> None:
        self._write_daemon("{}\n")
        self._write_success_commands()
        checkpoint = Path(self.tmp) / "checkpoint-check-use-race"
        displaced = Path(self.tmp) / "checkpoint-check-use-original"
        attacker = Path(self.tmp) / "checkpoint-check-use-attacker"
        attacker.mkdir(mode=0o700)
        audit_dir = Path(self.tmp) / "checkpoint-race-audit"
        audit_dir.mkdir()
        (audit_dir / "sitecustomize.py").write_text(
            "import os, tempfile\n"
            "_mkstemp = tempfile.mkstemp\n"
            "_replace = os.replace\n"
            "def mkstemp(*args, **kwargs):\n"
            "    if kwargs.get('prefix') == '.docker-network-policy.' and not os.path.exists(os.environ['SWAP_DONE']):\n"
            "        open(os.environ['SWAP_DONE'], 'w').close()\n"
            "        os.rename(os.environ['CHECKPOINT'], os.environ['DISPLACED'])\n"
            "        os.symlink(os.environ['ATTACKER'], os.environ['CHECKPOINT'])\n"
            "    return _mkstemp(*args, **kwargs)\n"
            "def replace(source, target):\n"
            "    if target.endswith('/docker-network-policy.json') and os.path.realpath(os.path.dirname(target)) == os.environ['ATTACKER']:\n"
            "        open(os.environ['RACE_MARKER'], 'w').close()\n"
            "    return _replace(source, target)\n"
            "tempfile.mkstemp = mkstemp\n"
            "os.replace = replace\n",
            encoding="utf-8",
        )
        env_file = self._write_env_file(self._rendered_with_policy())

        result = subprocess.run(
            [
                str(SCRIPTS / "apply-docker-network-policy.sh"),
                "--checkpoint",
                str(checkpoint),
                "--env",
                str(env_file),
            ],
            capture_output=True,
            text=True,
            env=self._env(
                PYTHONPATH=str(audit_dir),
                CHECKPOINT=str(checkpoint),
                DISPLACED=str(displaced),
                ATTACKER=str(attacker),
                SWAP_DONE=str(Path(self.tmp) / "checkpoint-swap.done"),
                RACE_MARKER=str(Path(self.tmp) / "checkpoint-race.marker"),
            ),
            timeout=30,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((Path(self.tmp) / "checkpoint-race.marker").exists())
        self.assertFalse((attacker / "docker-network-policy.json").exists())

    def test_checkpoint_symlink_swap_does_not_redirect_state(self) -> None:
        self._write_daemon("{}\n")
        self._write_success_commands()
        checkpoint = Path(self.tmp) / "checkpoint-symlink-race"
        attacker = Path(self.tmp) / "attacker-checkpoint"
        attacker.mkdir(mode=0o700)
        self.drain_command.write_text(
            "#!/usr/bin/env bash\n"
            f"rm -rf {checkpoint}\n"
            f"ln -s {attacker} {checkpoint}\n",
            encoding="utf-8",
        )
        env_file = self._write_env_file(self._rendered_with_policy())

        result = self._run(str(env_file), checkpoint_dir=str(checkpoint))

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((attacker / "docker-network-policy.json").exists())

    def test_rejects_checkpoint_below_group_writable_parent_before_drain(self) -> None:
        daemon = self._write_daemon("{}\n")
        self._write_success_commands()
        parent = Path(self.tmp) / "writable-checkpoint-parent"
        parent.mkdir(mode=0o777)
        parent.chmod(0o777)
        checkpoint = parent / "checkpoint"
        drain_marker = Path(self.tmp) / "writable-checkpoint-drain.marker"
        self.drain_command.write_text(f"#!/usr/bin/env bash\ntouch {drain_marker}\n", encoding="utf-8")
        env_file = self._write_env_file(self._rendered_with_policy())

        result = self._run(str(env_file), checkpoint_dir=str(checkpoint))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("trusted root-owned path", result.stderr)
        self.assertEqual(daemon.read_text(encoding="utf-8"), "{}\n")
        self.assertFalse(drain_marker.exists())

    def test_rejects_unsafe_preexisting_checkpoint_before_drain(self) -> None:
        daemon = self._write_daemon("{}\n")
        checkpoint = Path(self.tmp) / "unsafe-checkpoint"
        checkpoint.mkdir(mode=0o755)
        drain_marker = Path(self.tmp) / "unsafe-checkpoint-drain.marker"
        self.drain_command.write_text(f"#!/usr/bin/env bash\ntouch {drain_marker}\n", encoding="utf-8")
        self._write_success_commands()
        env_file = self._write_env_file(self._rendered_with_policy())

        result = self._run(str(env_file), checkpoint_dir=str(checkpoint))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("checkpoint directory must be a trusted root-owned path", result.stderr)
        self.assertEqual(daemon.read_text(encoding="utf-8"), "{}\n")
        self.assertFalse(drain_marker.exists())

    def test_successful_apply_records_managed_original_presence_and_mode(self) -> None:
        for prior_present in (False, True):
            with self.subTest(prior_present=prior_present):
                daemon = self.daemon_dir / "daemon.json"
                daemon.unlink(missing_ok=True)
                if prior_present:
                    daemon.write_bytes(b'{"icc":false}\n')
                    daemon.chmod(0o600)
                checkpoint = Path(self.tmp) / f"checkpoint-{prior_present}"
                self._write_success_commands()
                env_file = self._write_env_file(self._rendered_with_policy())

                result = self._run(str(env_file), checkpoint_dir=str(checkpoint))

                self.assertEqual(result.returncode, 0, result.stderr)
                state = json.loads((checkpoint / "docker-network-policy.json").read_text(encoding="utf-8"))
                self.assertEqual(
                    state,
                    {
                        "managed": True,
                        "prior_default_address_pools": None,
                        "prior_default_address_pools_present": False,
                        "prior_mode": "600" if prior_present else None,
                        "prior_present": prior_present,
                        "verified_generation": hashlib.sha256(daemon.read_bytes()).hexdigest(),
                    },
                )
                self.assertEqual((checkpoint / "docker-network-policy.json").stat().st_mode & 0o777, 0o600)
                self.assertFalse((checkpoint / "daemon.json").exists())

    def test_first_apply_rejects_invalid_baseline_pools_before_checkpoint_or_drain(self) -> None:
        daemon = self._write_daemon('{"default-address-pools":[],"live-restore":true}\n')
        prior = daemon.read_bytes()
        checkpoint = Path(self.tmp) / "checkpoint-invalid-baseline-pools"
        self._write_success_commands()
        drain_marker = Path(self.tmp) / "invalid-baseline-pools-drain.marker"
        self.drain_command.write_text(f"#!/usr/bin/env bash\ntouch {drain_marker}\n", encoding="utf-8")
        env_file = self._write_env_file(self._rendered_with_policy())

        result = self._run(str(env_file), checkpoint_dir=str(checkpoint))

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(daemon.read_bytes(), prior)
        self.assertFalse((checkpoint / "docker-network-policy.json").exists())
        self.assertFalse(drain_marker.exists())

    def test_reapply_rejects_invalid_baseline_pools_before_drain(self) -> None:
        rendered = self._rendered_with_policy()
        daemon = self._write_daemon(json.dumps(render_docker_daemon_config(rendered)))
        checkpoint = Path(self.tmp) / "checkpoint-invalid-reapply-baseline"
        checkpoint.mkdir(mode=0o700)
        state_file = checkpoint / "docker-network-policy.json"
        state_file.write_text(
            json.dumps({
                "managed": True,
                "prior_default_address_pools": [],
                "prior_default_address_pools_present": True,
                "prior_mode": "600",
                "prior_present": True,
                "verified_generation": hashlib.sha256(daemon.read_bytes()).hexdigest(),
            }),
            encoding="utf-8",
        )
        state_file.chmod(0o600)
        drain_marker = Path(self.tmp) / "invalid-reapply-baseline-drain.marker"
        self.drain_command.write_text(f"#!/usr/bin/env bash\ntouch {drain_marker}\n", encoding="utf-8")
        self._write_success_commands()
        env_file = self._write_env_file(rendered)

        result = self._run(str(env_file), checkpoint_dir=str(checkpoint))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("checkpoint state is invalid", result.stderr)
        self.assertFalse(drain_marker.exists())

    def test_policy_reapply_fails_closed_when_managed_key_provenance_is_missing(self) -> None:
        rendered = self._rendered_with_policy()
        daemon = self._write_daemon(json.dumps(render_docker_daemon_config(rendered)))
        prior = daemon.read_bytes()
        checkpoint = Path(self.tmp) / "checkpoint-missing-backup"
        checkpoint.mkdir(mode=0o700)
        state_file = checkpoint / "docker-network-policy.json"
        state_file.write_text(
            json.dumps({"managed": True, "prior_mode": "600", "prior_present": True}),
            encoding="utf-8",
        )
        state_file.chmod(0o600)
        markers = []
        for name in ("drain.sh", "restart.sh", "probe.sh", "health.sh"):
            marker = Path(self.tmp) / f"missing-backup-{name}.marker"
            markers.append(marker)
            command = Path(self.tmp) / name
            command.write_text(f"#!/usr/bin/env bash\ntouch {marker}\n", encoding="utf-8")
            command.chmod(0o755)
        env_file = self._write_env_file(rendered)

        result = self._run(str(env_file), checkpoint_dir=str(checkpoint))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("checkpoint state is invalid", result.stderr)
        self.assertEqual(daemon.read_bytes(), prior)
        self.assertTrue(all(not marker.exists() for marker in markers))

    def test_policy_reapply_rejects_persistent_recovery_backup(self) -> None:
        rendered = self._rendered_with_policy()
        daemon = self._write_daemon(json.dumps(render_docker_daemon_config(rendered)))
        prior = daemon.read_bytes()
        checkpoint = Path(self.tmp) / "checkpoint-unexpected-backup"
        checkpoint.mkdir(mode=0o700)
        state_file = checkpoint / "docker-network-policy.json"
        state_file.write_text(
            json.dumps({"managed": True, "prior_mode": None, "prior_present": False}),
            encoding="utf-8",
        )
        state_file.chmod(0o600)
        backup_file = checkpoint / "daemon.json"
        backup_file.write_text("{}\n", encoding="utf-8")
        backup_file.chmod(0o600)
        markers = []
        for name in ("drain.sh", "restart.sh", "probe.sh", "health.sh"):
            marker = Path(self.tmp) / f"unexpected-backup-{name}.marker"
            markers.append(marker)
            command = Path(self.tmp) / name
            command.write_text(f"#!/usr/bin/env bash\ntouch {marker}\n", encoding="utf-8")
            command.chmod(0o755)
        env_file = self._write_env_file(rendered)

        result = self._run(str(env_file), checkpoint_dir=str(checkpoint))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("checkpoint state is invalid", result.stderr)
        self.assertEqual(daemon.read_bytes(), prior)
        self.assertTrue(all(not marker.exists() for marker in markers))

    def test_rejects_installer_lock_at_checkpoint_marker_before_drain(self) -> None:
        self._write_daemon("{}\n")
        self._write_success_commands()
        checkpoint = Path(self.tmp) / "checkpoint-lock-marker-alias"
        checkpoint.mkdir(mode=0o700)
        marker = checkpoint / "docker-network-policy.json"
        marker.write_text("{}\n", encoding="utf-8")
        marker.chmod(0o600)
        drain_marker = Path(self.tmp) / "lock-marker-alias-drain.marker"
        self.drain_command.write_text(f"#!/usr/bin/env bash\ntouch {drain_marker}\n", encoding="utf-8")
        env_file = self._write_env_file(self._rendered_with_policy())

        result = subprocess.run(
            [
                str(SCRIPTS / "apply-docker-network-policy.sh"),
                "--checkpoint",
                str(checkpoint),
                "--env",
                str(env_file),
            ],
            capture_output=True,
            text=True,
            env=self._env(CI_FLEET_INSTALLER_LOCK=str(marker)),
            timeout=30,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("installer lock and checkpoint state paths must be separate", result.stderr)
        self.assertFalse(drain_marker.exists())

    def test_rejects_installer_lock_hardlink_to_checkpoint_marker_before_drain(self) -> None:
        self._write_daemon("{}\n")
        self._write_success_commands()
        checkpoint = Path(self.tmp) / "checkpoint-lock-marker-hardlink"
        checkpoint.mkdir(mode=0o700)
        marker = checkpoint / "docker-network-policy.json"
        marker.write_text("{}\n", encoding="utf-8")
        marker.chmod(0o600)
        lock = Path(self.tmp) / "network-policy-marker-hardlink.lock"
        os.link(marker, lock)
        drain_marker = Path(self.tmp) / "lock-marker-hardlink-drain.marker"
        self.drain_command.write_text(f"#!/usr/bin/env bash\ntouch {drain_marker}\n", encoding="utf-8")
        env_file = self._write_env_file(self._rendered_with_policy())

        result = subprocess.run(
            [
                str(SCRIPTS / "apply-docker-network-policy.sh"),
                "--checkpoint",
                str(checkpoint),
                "--env",
                str(env_file),
            ],
            capture_output=True,
            text=True,
            env=self._env(CI_FLEET_INSTALLER_LOCK=str(lock)),
            timeout=30,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("installer lock and checkpoint state paths must be separate", result.stderr)
        self.assertFalse(drain_marker.exists())

    def test_rejects_absent_daemon_config_in_checkpoint_after_lock_before_drain(self) -> None:
        self.daemon_dir.chmod(0o700)
        self._write_success_commands()
        env_file = self._write_env_file(self._rendered_with_policy())
        lock = Path(self.tmp) / "network-policy.lock"
        drain_marker = Path(self.tmp) / "drain.marker"
        self.drain_command.write_text(f"#!/usr/bin/env bash\ntouch {drain_marker}\n", encoding="utf-8")

        result = subprocess.run(
            [
                str(SCRIPTS / "apply-docker-network-policy.sh"),
                "--checkpoint",
                str(self.daemon_dir),
                "--env",
                str(env_file),
            ],
            capture_output=True,
            text=True,
            env=self._env(CI_FLEET_INSTALLER_LOCK=str(lock)),
            timeout=30,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("daemon config must be outside checkpoint directory", result.stderr)
        self.assertFalse((self.daemon_dir / "daemon.json").exists())
        self.assertTrue(lock.exists())
        self.assertFalse(drain_marker.exists())

    def test_rejects_relative_daemon_config_before_drain(self) -> None:
        self._write_success_commands()
        env_file = self._write_env_file(self._rendered_with_policy())
        lock = Path(self.tmp) / "network-policy.lock"

        result = subprocess.run(
            [
                str(SCRIPTS / "apply-docker-network-policy.sh"),
                "--checkpoint",
                str(Path(self.tmp) / "checkpoint-relative-daemon"),
                "--env",
                str(env_file),
            ],
            capture_output=True,
            text=True,
            env=self._env(
                CI_FLEET_DOCKER_DAEMON_CONFIG="relative/daemon.json",
                CI_FLEET_INSTALLER_LOCK=str(lock),
            ),
            timeout=30,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("trusted root-owned path", result.stderr)
        self.assertTrue(lock.exists())

    def test_rejects_fifo_daemon_config_before_drain(self) -> None:
        daemon = self.daemon_dir / "daemon.json"
        os.mkfifo(daemon)
        self._write_success_commands()
        env_file = self._write_env_file(self._rendered_with_policy())
        lock = Path(self.tmp) / "network-policy.lock"
        writer = subprocess.Popen(["bash", "-c", 'printf "{}\\n" >"$1"', "fifo-writer", str(daemon)])
        self.addCleanup(writer.kill)

        result = subprocess.run(
            [
                str(SCRIPTS / "apply-docker-network-policy.sh"),
                "--checkpoint",
                str(Path(self.tmp) / "checkpoint-fifo-daemon"),
                "--env",
                str(env_file),
            ],
            capture_output=True,
            text=True,
            env=self._env(CI_FLEET_INSTALLER_LOCK=str(lock)),
            timeout=30,
        )
        if writer.poll() is None:
            writer.terminate()
        writer.wait(timeout=5)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("trusted root-owned path", result.stderr)
        self.assertTrue(lock.exists())

    def test_rejects_group_writable_daemon_file_before_drain(self) -> None:
        daemon = self._write_daemon("{}\n")
        daemon.chmod(0o664)
        self._write_success_commands()
        env_file = self._write_env_file(self._rendered_with_policy())
        lock = Path(self.tmp) / "network-policy.lock"

        result = subprocess.run(
            [
                str(SCRIPTS / "apply-docker-network-policy.sh"),
                "--checkpoint",
                str(Path(self.tmp) / "checkpoint-writable-daemon"),
                "--env",
                str(env_file),
            ],
            capture_output=True,
            text=True,
            env=self._env(CI_FLEET_INSTALLER_LOCK=str(lock)),
            timeout=30,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("trusted root-owned path", result.stderr)
        self.assertTrue(lock.exists())

    def test_rejects_group_writable_daemon_directory_before_drain(self) -> None:
        self._write_daemon("{}\n")
        self._write_success_commands()
        self.daemon_dir.chmod(0o775)
        env_file = self._write_env_file(self._rendered_with_policy())
        lock = Path(self.tmp) / "network-policy.lock"

        result = subprocess.run(
            [
                str(SCRIPTS / "apply-docker-network-policy.sh"),
                "--checkpoint",
                str(Path(self.tmp) / "checkpoint-writable-directory"),
                "--env",
                str(env_file),
            ],
            capture_output=True,
            text=True,
            env=self._env(CI_FLEET_INSTALLER_LOCK=str(lock)),
            timeout=30,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("trusted root-owned path", result.stderr)
        self.assertTrue(lock.exists())

    def test_rejects_hook_below_group_writable_parent_before_drain(self) -> None:
        self._write_daemon("{}\n")
        self._write_success_commands()
        hook_dir = Path(self.tmp) / "writable-hook-parent"
        hook_dir.mkdir(mode=0o777)
        hook_dir.chmod(0o777)
        hook = hook_dir / "drain.sh"
        hook.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        hook.chmod(0o755)
        env_file = self._write_env_file(self._rendered_with_policy())
        lock = Path(self.tmp) / "network-policy.lock"

        result = subprocess.run(
            [
                str(SCRIPTS / "apply-docker-network-policy.sh"),
                "--checkpoint",
                str(Path(self.tmp) / "checkpoint-writable-hook-parent"),
                "--env",
                str(env_file),
            ],
            capture_output=True,
            text=True,
            env=self._env(
                CI_FLEET_DOCKER_DRAIN_COMMAND=str(hook),
                CI_FLEET_INSTALLER_LOCK=str(lock),
            ),
            timeout=30,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("trusted root-owned path", result.stderr)
        self.assertTrue(lock.exists())

    def test_rejects_group_writable_injected_hook_before_drain(self) -> None:
        self._write_daemon("{}\n")
        self._write_success_commands()
        self.drain_command.chmod(0o775)
        env_file = self._write_env_file(self._rendered_with_policy())
        lock = Path(self.tmp) / "network-policy.lock"

        result = subprocess.run(
            [
                str(SCRIPTS / "apply-docker-network-policy.sh"),
                "--checkpoint",
                str(Path(self.tmp) / "checkpoint-writable-hook"),
                "--env",
                str(env_file),
            ],
            capture_output=True,
            text=True,
            env=self._env(CI_FLEET_INSTALLER_LOCK=str(lock)),
            timeout=30,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("trusted root-owned path", result.stderr)
        self.assertTrue(lock.exists())

    def test_injected_hooks_require_absolute_canonical_regular_executables(self) -> None:
        env_file = self._write_env_file(self._rendered_with_policy())
        hook_names = (
            "CI_FLEET_DOCKER_DRAIN_COMMAND",
            "CI_FLEET_DOCKER_RESTART_COMMAND",
            "CI_FLEET_CONTROLLER_RESUME_COMMAND",
            "CI_FLEET_DOCKER_NETWORK_PROBE",
            "CI_FLEET_HEALTH_CHECK_COMMAND",
        )
        self._write_success_commands()
        target = Path(self.tmp) / "hook-target.sh"
        target.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        target.chmod(0o755)
        for hook_name in hook_names:
            with self.subTest(hook_name=hook_name):
                daemon = self._write_daemon("{}\n")
                link = Path(self.tmp) / f"{hook_name}.sh"
                link.symlink_to(target)
                checkpoint = Path(self.tmp) / f"checkpoint-{hook_name}"

                result = subprocess.run(
                    [
                        str(SCRIPTS / "apply-docker-network-policy.sh"),
                        "--checkpoint",
                        str(checkpoint),
                        "--env",
                        str(env_file),
                    ],
                    capture_output=True,
                    text=True,
                    env=self._env(**{hook_name: str(link)}),
                    timeout=30,
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(daemon.read_text(encoding="utf-8"), "{}\n")
                self.assertFalse(checkpoint.exists())
                link.unlink()

    def test_all_hooks_use_kill_grace_and_suppress_output(self) -> None:
        self._write_daemon("{}\n")
        env_file = self._write_env_file(self._rendered_with_policy())
        timeout_log = Path(self.tmp) / "timeout.log"
        hook_log = Path(self.tmp) / "hooks.log"
        fake_bin = Path(self.tmp) / "fake-bin"
        fake_bin.mkdir()
        fake_timeout = fake_bin / "timeout"
        fake_timeout.write_text(
            "#!/usr/bin/env bash\n"
            f"printf '%s\\n' \"$*\" >> {timeout_log}\n"
            "[[ $1 != --kill-after=* ]] || shift\n"
            "shift\n"
            '"$@"\n',
            encoding="utf-8",
        )
        fake_timeout.chmod(0o755)
        hooks = {
            "CI_FLEET_DOCKER_DRAIN_COMMAND": self.drain_command,
            "CI_FLEET_DOCKER_RESTART_COMMAND": Path(self.tmp) / "restart.sh",
            "CI_FLEET_CONTROLLER_RESUME_COMMAND": Path(self.tmp) / "resume.sh",
            "CI_FLEET_DOCKER_NETWORK_PROBE": Path(self.tmp) / "probe.sh",
            "CI_FLEET_HEALTH_CHECK_COMMAND": Path(self.tmp) / "health.sh",
        }
        for path in hooks.values():
            path.write_text(
                "#!/usr/bin/env bash\n"
                f"echo {path.name} >> {hook_log}\n"
                "echo private-hook-output\n"
                "echo private-hook-error >&2\n",
                encoding="utf-8",
            )
            path.chmod(0o755)

        run_env = self._env(PATH=f"{fake_bin}:{os.environ['PATH']}", **{name: str(path) for name, path in hooks.items()})
        result = subprocess.run(
            [
                str(SCRIPTS / "apply-docker-network-policy.sh"),
                "--checkpoint",
                str(Path(self.tmp) / "checkpoint-timeout"),
                "--env",
                str(env_file),
            ],
            capture_output=True,
            text=True,
            env=run_env,
            timeout=30,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("private-hook", result.stdout + result.stderr)
        lines = timeout_log.read_text(encoding="utf-8").splitlines()
        for path in hooks.values():
            self.assertTrue(any(line.startswith(f"--kill-after=5 300 {path}") for line in lines), path)
        self.assertEqual(sorted(hook_log.read_text(encoding="utf-8").splitlines()), sorted(path.name for path in hooks.values()))

    def test_health_accepts_only_success_and_warning_results(self) -> None:
        env_file = self._write_env_file(self._rendered_with_policy())
        cases = {
            "healthy": ("#!/usr/bin/env bash\nexit 0\n", True),
            "warning": ("#!/usr/bin/env bash\nexit 1\n", True),
            "critical": ("#!/usr/bin/env bash\nexit 2\n", False),
            "signal": ("#!/usr/bin/env bash\nkill -TERM $$\n", False),
            "timeout": ("#!/usr/bin/env bash\nsleep 10\n", False),
            "execution": ("not an executable format\n", False),
        }
        for name, (script, accepted) in cases.items():
            with self.subTest(name=name):
                self._write_daemon("{}\n")
                checkpoint = Path(self.tmp) / f"checkpoint-health-{name}"
                shutil.rmtree(checkpoint, ignore_errors=True)
                self._write_success_commands()
                health = Path(self.tmp) / "health.sh"
                health.write_text(script, encoding="utf-8")
                health.chmod(0o755)

                result = subprocess.run(
                    [
                        str(SCRIPTS / "apply-docker-network-policy.sh"),
                        "--checkpoint",
                        str(checkpoint),
                        "--env",
                        str(env_file),
                    ],
                    capture_output=True,
                    text=True,
                    env=self._env(CI_FLEET_COMMAND_TIMEOUT_SECONDS="1"),
                    timeout=15,
                )

                self.assertEqual(result.returncode == 0, accepted, result.stderr)

    def test_transactional_health_suppresses_delivery(self) -> None:
        self._write_daemon("{}\n")
        self._write_success_commands()
        health_log = Path(self.tmp) / "health-suppression.log"
        health = Path(self.tmp) / "health.sh"
        health.write_text(
            "#!/usr/bin/env bash\n"
            f"printf '%s\\n' \"${{CI_FLEET_HEALTH_SUPPRESS_DELIVERY:-unset}}\" >> {health_log}\n"
            f"(( $(wc -l < {health_log}) > 1 )) || exit 2\n",
            encoding="utf-8",
        )
        health.chmod(0o755)
        env_file = self._write_env_file(self._rendered_with_policy())

        result = self._run(str(env_file), expected_rc=1)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(health_log.read_text(encoding="utf-8").splitlines(), ["1", "1"])

    def test_env_file_is_trusted_and_snapshotted_before_mutation(self) -> None:
        self._write_success_commands()
        env_file = self._write_env_file(self._rendered_with_policy())
        env_file.chmod(0o666)
        drain_marker = Path(self.tmp) / "untrusted-drain.marker"
        self.drain_command.write_text(f"#!/usr/bin/env bash\ntouch {drain_marker}\n", encoding="utf-8")

        rejected = self._run(str(env_file))

        self.assertNotEqual(rejected.returncode, 0)
        self.assertFalse(drain_marker.exists())

        env_file.chmod(0o600)
        health_log = Path(self.tmp) / "snapshot-health.log"
        self.drain_command.write_text(
            "#!/usr/bin/env bash\n"
            f"printf 'CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_COUNT=0\\n' > {env_file}\n",
            encoding="utf-8",
        )
        health = Path(self.tmp) / "health.sh"
        health.write_text(
            "#!/usr/bin/env bash\n"
            f"[[ $1 == --env && $2 != {env_file} ]] || exit 2\n"
            '[[ $(stat -c %a "$2") == 600 && $(stat -c %a "$(dirname "$2")") == 700 ]] || exit 2\n'
            'grep -Fqx "CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_COUNT=2" "$2" || exit 2\n'
            f"printf '%s\\n' \"$2\" > {health_log}\n",
            encoding="utf-8",
        )
        health.chmod(0o755)

        applied = self._run(str(env_file), checkpoint_dir=str(Path(self.tmp) / "checkpoint-snapshot"))

        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertNotEqual(health_log.read_text(encoding="utf-8").strip(), str(env_file))
        daemon = json.loads((self.daemon_dir / "daemon.json").read_text(encoding="utf-8"))
        self.assertEqual(len(daemon["default-address-pools"]), 2)

    def test_candidate_is_snapshotted_before_rendering_under_lock(self) -> None:
        self._write_daemon("{}\n")
        self._write_success_commands()
        rendered = self._rendered_with_policy()
        env_file = self._write_env_file(rendered)
        replacement = dict(rendered)
        replacement["CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_0_BASE"] = "192.0.2.0/24"
        replacement_file = Path(self.tmp) / "replacement.env"
        replacement_file.write_text(
            "".join(f"{key}={value}\n" for key, value in sorted(replacement.items())),
            encoding="utf-8",
        )
        fake_bin = Path(self.tmp) / "candidate-snapshot-bin"
        fake_bin.mkdir()
        install = fake_bin / "install"
        install.write_text(
            "#!/usr/bin/env bash\n"
            f"if [[ ${{*: -1}} == */ci-fleet.env ]]; then {shutil.which('cp')} {replacement_file} {env_file}; fi\n"
            f"exec {shutil.which('install')} \"$@\"\n",
            encoding="utf-8",
        )
        install.chmod(0o755)

        result = subprocess.run(
            [
                str(SCRIPTS / "apply-docker-network-policy.sh"),
                "--checkpoint",
                str(Path(self.tmp) / "checkpoint-candidate-snapshot"),
                "--env",
                str(env_file),
            ],
            capture_output=True,
            text=True,
            env=self._env(PATH=f"{fake_bin}:{os.environ['PATH']}"),
            timeout=30,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        pools = json.loads((self.daemon_dir / "daemon.json").read_text(encoding="utf-8"))["default-address-pools"]
        self.assertEqual(pools[0]["base"], "192.0.2.0/24")

    def test_env_file_below_writable_parent_is_rejected_before_side_effects(self) -> None:
        self._write_daemon("{}\n")
        self._write_success_commands()
        untrusted_parent = Path(self.tmp) / "writable-parent"
        untrusted_parent.mkdir()
        untrusted_parent.chmod(0o777)
        env_file = untrusted_parent / "ci-fleet.env"
        env_file.write_text(
            "".join(f"{key}={value}\n" for key, value in sorted(self._rendered_with_policy().items())),
            encoding="utf-8",
        )
        env_file.chmod(0o600)
        checkpoint = Path(self.tmp) / "checkpoint-untrusted-env-parent"
        lock = Path(self.tmp) / "untrusted-env-parent.lock"
        drain_marker = Path(self.tmp) / "untrusted-env-parent-drain.marker"
        snapshot_marker = Path(self.tmp) / "untrusted-env-parent-snapshot.marker"
        self.drain_command.write_text(f"#!/usr/bin/env bash\ntouch {drain_marker}\n", encoding="utf-8")
        fake_bin = Path(self.tmp) / "untrusted-env-parent-bin"
        fake_bin.mkdir()
        install = fake_bin / "install"
        install.write_text(
            "#!/usr/bin/env bash\n"
            f"touch {snapshot_marker}\n"
            f"exec {shutil.which('install')} \"$@\"\n",
            encoding="utf-8",
        )
        install.chmod(0o755)

        result = subprocess.run(
            [str(SCRIPTS / "apply-docker-network-policy.sh"), "--checkpoint", str(checkpoint), "--env", str(env_file)],
            capture_output=True,
            text=True,
            env=self._env(CI_FLEET_INSTALLER_LOCK=str(lock), PATH=f"{fake_bin}:{os.environ['PATH']}"),
            timeout=30,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("trusted root-owned path", result.stderr)
        self.assertFalse(snapshot_marker.exists())
        self.assertFalse(checkpoint.exists())
        self.assertFalse(lock.exists())
        self.assertFalse(drain_marker.exists())

    def test_renderer_failure_suppresses_private_stderr(self) -> None:
        daemon = self._write_daemon("{}\n")
        prior = daemon.read_bytes()
        self._write_success_commands()
        private_pool = "10.77.88.0/24"
        env_file = self._write_env_file(
            {
                "CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_COUNT": "1",
                "CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_0_BASE": private_pool,
                "CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_0_SIZE": "16",
            }
        )
        drain_marker = Path(self.tmp) / "renderer-drain.marker"
        self.drain_command.write_text(f"#!/usr/bin/env bash\ntouch {drain_marker}\n", encoding="utf-8")

        result = self._run(str(env_file))

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(private_pool, result.stdout + result.stderr)
        self.assertEqual(daemon.read_bytes(), prior)
        self.assertFalse(drain_marker.exists())

    def test_policy_requires_drain_command(self) -> None:
        prior = b'{"bip":"172.17.0.1/16"}\n'
        daemon = self.daemon_dir / "daemon.json"
        daemon.write_bytes(prior)
        restart_marker = Path(self.tmp) / "restart.marker"
        restart = Path(self.tmp) / "restart.sh"
        restart.write_text(f"#!/usr/bin/env bash\ntouch {restart_marker}\n", encoding="utf-8")
        restart.chmod(0o755)
        for name in ("probe.sh", "health.sh"):
            command = Path(self.tmp) / name
            command.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            command.chmod(0o755)
        env_file = self._write_env_file(self._rendered_with_policy())
        env = self._env()
        env.pop("CI_FLEET_DOCKER_DRAIN_COMMAND")

        result = subprocess.run(
            [str(SCRIPTS / "apply-docker-network-policy.sh"), "--checkpoint", str(Path(self.tmp) / "checkpoint-direct"), "--env", str(env_file)],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(daemon.read_bytes(), prior)
        self.assertFalse(restart_marker.exists())

    def test_existing_installer_lock_blocks_before_drain_or_checkpoint(self) -> None:
        daemon = self._write_daemon("{}\n")
        prior = daemon.read_bytes()
        drain_marker = Path(self.tmp) / "drain.marker"
        drain = Path(self.tmp) / "drain-marker.sh"
        drain.write_text(f"#!/usr/bin/env bash\ntouch {drain_marker}\n", encoding="utf-8")
        drain.chmod(0o755)
        for name in ("restart.sh", "probe.sh", "health.sh"):
            command = Path(self.tmp) / name
            command.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            command.chmod(0o755)
        lock = Path(self.tmp) / "run" / "ci-fleet-installer.lock"
        lock.parent.mkdir(exist_ok=True)
        checkpoint = Path(self.tmp) / "checkpoint"
        env_file = self._write_env_file(self._rendered_with_policy())

        with lock.open("w") as lock_handle:
            fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = subprocess.run(
                [
                    str(SCRIPTS / "apply-docker-network-policy.sh"),
                    "--checkpoint",
                    str(checkpoint),
                    "--env",
                    str(env_file),
                ],
                capture_output=True,
                text=True,
                env=self._env(
                    CI_FLEET_DOCKER_DRAIN_COMMAND=str(drain),
                    CI_FLEET_INSTALLER_LOCK=str(lock),
                ),
                timeout=30,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(daemon.read_bytes(), prior)
        self.assertFalse(drain_marker.exists())
        self.assertFalse(checkpoint.exists())

    def test_installer_lock_cannot_alias_daemon_config(self) -> None:
        daemon = self._write_daemon("{}\n")
        prior = daemon.read_bytes()
        self._write_success_commands()
        drain_marker = Path(self.tmp) / "aliased-lock-drain.marker"
        self.drain_command.write_text(f"#!/usr/bin/env bash\ntouch {drain_marker}\n", encoding="utf-8")
        checkpoint = Path(self.tmp) / "checkpoint-aliased-lock"
        env_file = self._write_env_file(self._rendered_with_policy())

        result = subprocess.run(
            [str(SCRIPTS / "apply-docker-network-policy.sh"), "--checkpoint", str(checkpoint), "--env", str(env_file)],
            capture_output=True,
            text=True,
            env=self._env(CI_FLEET_INSTALLER_LOCK=str(daemon)),
            timeout=30,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("lock and daemon paths must be separate", result.stderr)
        self.assertEqual(daemon.read_bytes(), prior)
        self.assertFalse(drain_marker.exists())
        self.assertFalse(checkpoint.exists())

    def test_hook_descendants_do_not_retain_installer_lock(self) -> None:
        self._write_daemon("{}\n")
        self._write_success_commands()
        lock = Path(self.tmp) / "hook-descendant.lock"
        child_pid = Path(self.tmp) / "hook-descendant.pid"
        self.drain_command.write_text(
            "#!/usr/bin/env bash\n"
            "sleep 30 &\n"
            f"printf '%s\\n' \"$!\" > {child_pid}\n",
            encoding="utf-8",
        )
        env_file = self._write_env_file(self._rendered_with_policy())

        result = subprocess.run(
            [str(SCRIPTS / "apply-docker-network-policy.sh"), "--checkpoint", str(Path(self.tmp) / "checkpoint-hook-descendant"), "--env", str(env_file)],
            capture_output=True,
            text=True,
            env=self._env(CI_FLEET_INSTALLER_LOCK=str(lock)),
            timeout=30,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        pid = int(child_pid.read_text(encoding="utf-8"))
        try:
            with lock.open("w") as lock_handle:
                try:
                    fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except BlockingIOError:
                    acquired = False
            self.assertTrue(acquired)
        finally:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def test_removal_noop_requires_installer_lock(self) -> None:
        self._write_daemon("{}\n")
        self._write_success_commands()
        env_file = self._write_env_file({"CI_FLEET_INSTANCE": "example-ci-01"})
        lock = Path(self.tmp) / "removal-noop.lock"
        checkpoint = Path(self.tmp) / "checkpoint-removal-noop-lock"

        with lock.open("w") as lock_handle:
            fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = subprocess.run(
                [
                    str(SCRIPTS / "apply-docker-network-policy.sh"),
                    "--checkpoint",
                    str(checkpoint),
                    "--env",
                    str(env_file),
                ],
                capture_output=True,
                text=True,
                env=self._env(CI_FLEET_INSTALLER_LOCK=str(lock)),
                timeout=30,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("NETWORK_POLICY_NOOP", result.stdout)

    def test_symlinked_installer_lock_below_writable_parent_is_rejected_without_side_effects(self) -> None:
        daemon = self._write_daemon("{}\n")
        prior = daemon.read_bytes()
        self._write_success_commands()
        env_file = self._write_env_file(self._rendered_with_policy())
        lock_dir = Path(self.tmp) / "writable-lock-parent"
        lock_dir.mkdir(mode=0o777)
        lock_dir.chmod(0o777)
        victim = Path(self.tmp) / "lock-victim"
        victim.write_text("do not truncate\n", encoding="utf-8")
        lock = lock_dir / "installer.lock"
        lock.symlink_to(victim)
        drain_marker = Path(self.tmp) / "symlink-lock-drain.marker"
        self.drain_command.write_text(f"#!/usr/bin/env bash\ntouch {drain_marker}\n", encoding="utf-8")

        result = subprocess.run(
            [
                str(SCRIPTS / "apply-docker-network-policy.sh"),
                "--checkpoint",
                str(Path(self.tmp) / "checkpoint-symlink-lock"),
                "--env",
                str(env_file),
            ],
            capture_output=True,
            text=True,
            env=self._env(CI_FLEET_INSTALLER_LOCK=str(lock)),
            timeout=30,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("trusted root-owned path", result.stderr)
        self.assertEqual(victim.read_text(encoding="utf-8"), "do not truncate\n")
        self.assertEqual(daemon.read_bytes(), prior)
        self.assertFalse(drain_marker.exists())

    def test_group_writable_installer_lock_parent_fails_before_side_effects(self) -> None:
        daemon = self._write_daemon("{}\n")
        prior = daemon.read_bytes()
        self._write_success_commands()
        env_file = self._write_env_file(self._rendered_with_policy())
        lock_dir = Path(self.tmp) / "group-writable-lock-parent"
        lock_dir.mkdir(mode=0o770)
        lock_dir.chmod(0o770)
        lock = lock_dir / "installer.lock"
        drain_marker = Path(self.tmp) / "writable-lock-parent-drain.marker"
        self.drain_command.write_text(f"#!/usr/bin/env bash\ntouch {drain_marker}\n", encoding="utf-8")

        result = subprocess.run(
            [
                str(SCRIPTS / "apply-docker-network-policy.sh"),
                "--checkpoint",
                str(Path(self.tmp) / "checkpoint-writable-lock-parent"),
                "--env",
                str(env_file),
            ],
            capture_output=True,
            text=True,
            env=self._env(CI_FLEET_INSTALLER_LOCK=str(lock)),
            timeout=30,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("trusted root-owned path", result.stderr)
        self.assertEqual(daemon.read_bytes(), prior)
        self.assertFalse(lock.exists())
        self.assertFalse(drain_marker.exists())

    def test_symlinked_daemon_config_is_rejected_before_drain_or_checkpoint(self) -> None:
        referent = Path(self.tmp) / "referent.json"
        prior = b'{"bip":"172.17.0.1/16"}\n'
        referent.write_bytes(prior)
        daemon = self.daemon_dir / "daemon.json"
        daemon.symlink_to(referent)
        drain_marker = Path(self.tmp) / "drain.marker"
        drain = Path(self.tmp) / "drain-marker.sh"
        drain.write_text(f"#!/usr/bin/env bash\ntouch {drain_marker}\n", encoding="utf-8")
        drain.chmod(0o755)
        for name in ("restart.sh", "probe.sh", "health.sh"):
            command = Path(self.tmp) / name
            command.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            command.chmod(0o755)
        checkpoint = Path(self.tmp) / "checkpoint"
        env_file = self._write_env_file(self._rendered_with_policy())

        result = subprocess.run(
            [
                str(SCRIPTS / "apply-docker-network-policy.sh"),
                "--checkpoint",
                str(checkpoint),
                "--env",
                str(env_file),
            ],
            capture_output=True,
            text=True,
            env=self._env(CI_FLEET_DOCKER_DRAIN_COMMAND=str(drain)),
            timeout=30,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(daemon.is_symlink())
        self.assertEqual(referent.read_bytes(), prior)
        self.assertFalse(drain_marker.exists())
        self.assertFalse(checkpoint.exists())

    def test_invalid_existing_config_does_not_drain(self) -> None:
        daemon = self._write_daemon("{not-json\n")
        drain_marker = Path(self.tmp) / "drain.marker"
        drain = Path(self.tmp) / "drain-marker.sh"
        drain.write_text(f"#!/usr/bin/env bash\ntouch {drain_marker}\n", encoding="utf-8")
        drain.chmod(0o755)
        for name in ("restart.sh", "probe.sh", "health.sh"):
            command = Path(self.tmp) / name
            command.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            command.chmod(0o755)
        env_file = self._write_env_file(self._rendered_with_policy())

        result = subprocess.run(
            [str(SCRIPTS / "apply-docker-network-policy.sh"), "--checkpoint", str(Path(self.tmp) / "checkpoint-direct"), "--env", str(env_file)],
            capture_output=True,
            text=True,
            env=self._env(CI_FLEET_DOCKER_DRAIN_COMMAND=str(drain)),
            timeout=30,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(daemon.read_text(encoding="utf-8"), "{not-json\n")
        self.assertFalse(drain_marker.exists())

    def test_semantically_equal_verified_daemon_config_is_no_change_before_side_effects(self) -> None:
        rendered = self._rendered_with_policy()
        desired = render_docker_daemon_config(rendered)
        daemon = self._write_daemon(json.dumps(desired, separators=(",", ":")))
        prior = daemon.read_bytes()
        markers = []
        for name in ("drain.sh", "restart.sh", "probe.sh", "resume.sh", "health.sh"):
            marker = Path(self.tmp) / f"{name}.marker"
            markers.append(marker)
            command = Path(self.tmp) / name
            command.write_text(f"#!/usr/bin/env bash\ntouch {marker}\n", encoding="utf-8")
            command.chmod(0o755)
        env_file = self._write_env_file(rendered)
        checkpoint = Path(self.tmp) / "checkpoint"
        checkpoint.mkdir(mode=0o700)
        state_file = checkpoint / "docker-network-policy.json"
        state_file.write_text(
            json.dumps({
                "managed": True,
                "prior_default_address_pools": None,
                "prior_default_address_pools_present": False,
                "prior_mode": None,
                "prior_present": False,
                "verified_generation": hashlib.sha256(prior).hexdigest(),
            }),
            encoding="utf-8",
        )
        state_file.chmod(0o600)

        result = self._run(str(env_file), checkpoint_dir=str(checkpoint))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "NETWORK_POLICY_NO_CHANGE\n")
        self.assertEqual(daemon.read_bytes(), prior)
        self.assertTrue(checkpoint.exists())
        self.assertTrue(all(not marker.exists() for marker in markers))

    def test_matching_file_without_verified_generation_runs_activation_and_marks_verified(self) -> None:
        self._write_daemon("{}\n")
        checkpoint = Path(self.tmp) / "checkpoint-pending-generation"
        self._write_success_commands()
        rendered = self._rendered_with_policy()
        env_file = self._write_env_file(rendered)
        applied = self._run(str(env_file), checkpoint_dir=str(checkpoint))
        self.assertEqual(applied.returncode, 0, applied.stderr)
        state_file = checkpoint / "docker-network-policy.json"
        state = json.loads(state_file.read_text(encoding="utf-8"))
        state.pop("verified_generation", None)
        state_file.write_text(json.dumps(state), encoding="utf-8")
        state_file.chmod(0o600)
        command_log = Path(self.tmp) / "pending-generation.log"
        command_log.touch()
        self.drain_command.write_text(f"#!/usr/bin/env bash\necho drain >> {command_log}\n", encoding="utf-8")
        for name in ("restart", "probe", "resume", "health"):
            command = Path(self.tmp) / f"{name}.sh"
            command.write_text(f"#!/usr/bin/env bash\necho {name} >> {command_log}\n", encoding="utf-8")
            command.chmod(0o755)

        reconciled = self._run(str(env_file), checkpoint_dir=str(checkpoint))

        self.assertEqual(reconciled.returncode, 0, reconciled.stderr)
        self.assertEqual(command_log.read_text(encoding="utf-8").splitlines(), ["drain", "restart", "probe", "resume", "health"])
        verified = json.loads(state_file.read_text(encoding="utf-8"))["verified_generation"]
        self.assertEqual(verified, hashlib.sha256((self.daemon_dir / "daemon.json").read_bytes()).hexdigest())

    def test_rejects_explicit_zero_on_managed_host_after_lock_before_checkpoint_or_drain(self) -> None:
        daemon = self._write_daemon("{}\n")
        checkpoint = Path(self.tmp) / "checkpoint-explicit-zero"
        self._write_success_commands()
        policy_env = self._write_env_file(self._rendered_with_policy())
        applied = self._run(str(policy_env), checkpoint_dir=str(checkpoint))
        self.assertEqual(applied.returncode, 0, applied.stderr)
        managed = daemon.read_bytes()
        state_file = checkpoint / "docker-network-policy.json"
        state = state_file.read_bytes()
        zero_env = self._write_env_file({"CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_COUNT": "0"})
        lock = Path(self.tmp) / "network-policy-zero.lock"
        drain_marker = Path(self.tmp) / "explicit-zero-drain.marker"
        self.drain_command.write_text(f"#!/usr/bin/env bash\ntouch {drain_marker}\n", encoding="utf-8")

        rejected = subprocess.run(
            [str(SCRIPTS / "apply-docker-network-policy.sh"), "--checkpoint", str(checkpoint), "--env", str(zero_env)],
            capture_output=True,
            text=True,
            env=self._env(CI_FLEET_INSTALLER_LOCK=str(lock)),
            timeout=30,
        )

        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(rejected.stderr, "ERROR: daemon policy rendering failed\n")
        self.assertEqual(daemon.read_bytes(), managed)
        self.assertEqual(state_file.read_bytes(), state)
        self.assertTrue(lock.exists())
        self.assertFalse(drain_marker.exists())

    def test_rejects_overlapping_rendered_pools_after_lock_before_drain(self) -> None:
        prior = b'{"bip":"172.17.0.1/16"}\n'
        daemon = self._write_daemon(prior.decode())
        self._write_success_commands()
        rendered = self._rendered_with_policy()
        rendered["CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_1_BASE"] = "198.51.100.128/25"
        env_file = self._write_env_file(rendered)
        lock = Path(self.tmp) / "network-policy.lock"
        drain_marker = Path(self.tmp) / "drain.marker"
        self.drain_command.write_text(f"#!/usr/bin/env bash\ntouch {drain_marker}\n", encoding="utf-8")

        result = subprocess.run(
            [str(SCRIPTS / "apply-docker-network-policy.sh"), "--checkpoint", str(Path(self.tmp) / "checkpoint-overlap"), "--env", str(env_file)],
            capture_output=True,
            text=True,
            env=self._env(CI_FLEET_INSTALLER_LOCK=str(lock)),
            timeout=30,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "ERROR: daemon policy rendering failed\n")
        self.assertEqual(daemon.read_bytes(), prior)
        self.assertTrue(lock.exists())
        self.assertFalse(drain_marker.exists())

    def test_rejects_insufficient_rendered_capacity_after_lock_before_drain(self) -> None:
        prior = b'{"bip":"172.17.0.1/16"}\n'
        daemon = self._write_daemon(prior.decode())
        self._write_success_commands()
        rendered = self._rendered_with_policy()
        rendered["CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_COUNT"] = "1"
        rendered["CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_0_BASE"] = "198.51.100.0/28"
        rendered["CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_0_SIZE"] = "29"
        env_file = self._write_env_file(rendered)
        lock = Path(self.tmp) / "network-policy.lock"
        drain_marker = Path(self.tmp) / "drain.marker"
        self.drain_command.write_text(f"#!/usr/bin/env bash\ntouch {drain_marker}\n", encoding="utf-8")

        result = subprocess.run(
            [str(SCRIPTS / "apply-docker-network-policy.sh"), "--checkpoint", str(Path(self.tmp) / "checkpoint-capacity"), "--env", str(env_file)],
            capture_output=True,
            text=True,
            env=self._env(CI_FLEET_INSTALLER_LOCK=str(lock)),
            timeout=30,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "ERROR: daemon policy rendering failed\n")
        self.assertEqual(daemon.read_bytes(), prior)
        self.assertTrue(lock.exists())
        self.assertFalse(drain_marker.exists())

    def test_validates_policy_before_mutation(self) -> None:
        prior = b'{"bip":"172.17.0.1/16"}\n'
        daemon = self.daemon_dir / "daemon.json"
        daemon.write_bytes(prior)
        bad_env = Path(self.tmp) / "bad.env"
        bad_env.write_text(
            "CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_COUNT=1\n"
            "CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_0_BASE=not-a-cidr\n"
            "CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_0_SIZE=29\n",
            encoding="utf-8",
        )
        drain_marker = Path(self.tmp) / "drain.marker"
        restart_marker = Path(self.tmp) / "restart.marker"
        for name, marker in (("drain.sh", drain_marker), ("restart.sh", restart_marker)):
            command = Path(self.tmp) / name
            command.write_text(f"#!/usr/bin/env bash\ntouch {marker}\n", encoding="utf-8")
            command.chmod(0o755)
        for name in ("probe.sh", "health.sh"):
            command = Path(self.tmp) / name
            command.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            command.chmod(0o755)
        result = subprocess.run(
            [str(SCRIPTS / "apply-docker-network-policy.sh"), "--checkpoint", str(Path(self.tmp) / "checkpoint-bad-env"), "--env", str(bad_env)],
            capture_output=True,
            text=True,
            env=self._env(CI_FLEET_DOCKER_DRAIN_COMMAND=str(Path(self.tmp) / "drain.sh")),
            timeout=30,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(daemon.read_bytes(), prior)
        self.assertFalse(drain_marker.exists())
        self.assertFalse(restart_marker.exists())

    def test_drain_failure_prevents_mutation_and_restart(self) -> None:
        prior = b'{"bip":"172.17.0.1/16"}\n'
        daemon = self.daemon_dir / "daemon.json"
        daemon.write_bytes(prior)
        drain = Path(self.tmp) / "drain.sh"
        drain.write_text(
            "#!/usr/bin/env bash\n"
            "echo '198.51.100.0/24 https://secret.example.invalid token=credential'\n"
            "echo 'drain-secret-error' >&2\n"
            "exit 1\n",
            encoding="utf-8",
        )
        drain.chmod(0o755)
        restart_marker = Path(self.tmp) / "restart.marker"
        restart = Path(self.tmp) / "restart.sh"
        restart.write_text(f"#!/usr/bin/env bash\ntouch {restart_marker}\n", encoding="utf-8")
        restart.chmod(0o755)
        for name in ("probe.sh", "health.sh"):
            command = Path(self.tmp) / name
            command.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            command.chmod(0o755)
        env_file = self._write_env_file(self._rendered_with_policy())
        result = subprocess.run(
            [str(SCRIPTS / "apply-docker-network-policy.sh"), "--checkpoint", str(Path(self.tmp) / "checkpoint-direct"), "--env", str(env_file)],
            capture_output=True,
            text=True,
            env=self._env(CI_FLEET_DOCKER_DRAIN_COMMAND=str(drain)),
            timeout=30,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(daemon.read_bytes(), prior)
        self.assertFalse(restart_marker.exists())
        combined = result.stdout + result.stderr
        self.assertEqual(combined, "ERROR: drain command failed before network-policy apply\n")
        self.assertNotIn("198.51.100.0/24", combined)
        self.assertNotIn("secret.example.invalid", combined)
        self.assertNotIn("credential", combined)
        self.assertNotIn("drain-secret-error", combined)

    def test_probe_failure_restores_absent_config_and_restarts(self) -> None:
        restart_log = Path(self.tmp) / "restart.log"
        restart = Path(self.tmp) / "restart.sh"
        restart.write_text(f"#!/usr/bin/env bash\necho restart >> {restart_log}\n", encoding="utf-8")
        restart.chmod(0o755)
        probe = Path(self.tmp) / "probe.sh"
        probe.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
        probe.chmod(0o755)
        health_log = Path(self.tmp) / "health.log"
        health = Path(self.tmp) / "health.sh"
        health.write_text(f"#!/usr/bin/env bash\necho health >> {health_log}\n", encoding="utf-8")
        health.chmod(0o755)
        env_file = self._write_env_file(self._rendered_with_policy())
        result = self._run(str(env_file), expected_rc=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.daemon_dir / "daemon.json").exists())
        self.assertEqual(restart_log.read_text(encoding="utf-8").splitlines(), ["restart", "restart"])
        self.assertEqual(health_log.read_text(encoding="utf-8").splitlines(), ["health"])

    def test_absent_apply_rollback_fsyncs_daemon_dir_after_unlink(self) -> None:
        self._write_success_commands()
        (Path(self.tmp) / "probe.sh").write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
        (Path(self.tmp) / "probe.sh").chmod(0o755)
        audit_log = Path(self.tmp) / "absent-rollback-fsync.log"
        audit_dir = Path(self.tmp) / "absent-rollback-audit"
        audit_dir.mkdir()
        (audit_dir / "sitecustomize.py").write_text(
            "import os\n"
            "_fsync = os.fsync\n"
            "_log = os.environ['FSYNC_AUDIT_LOG']\n"
            "def fsync(fd):\n"
            "    with open(_log, 'a', encoding='utf-8') as handle:\n"
            "        handle.write('F ' + os.path.realpath(f'/proc/self/fd/{fd}') + '\\n')\n"
            "    return _fsync(fd)\n"
            "os.fsync = fsync\n",
            encoding="utf-8",
        )
        fake_bin = Path(self.tmp) / "absent-rollback-bin"
        fake_bin.mkdir()
        (fake_bin / "rm").write_text(
            "#!/usr/bin/env bash\n"
            f"if [[ ${{*: -1}} == {self.daemon_dir / 'daemon.json'} ]]; then\n"
            f"  {shutil.which('rm')} \"$@\"\n"
            f"  printf 'U %s\\n' {self.daemon_dir / 'daemon.json'} >> {audit_log}\n"
            "  exit\n"
            "fi\n"
            f"exec {shutil.which('rm')} \"$@\"\n",
            encoding="utf-8",
        )
        (fake_bin / "rm").chmod(0o755)
        env_file = self._write_env_file(self._rendered_with_policy())

        result = subprocess.run(
            [str(SCRIPTS / "apply-docker-network-policy.sh"), "--checkpoint", str(Path(self.tmp) / "checkpoint-absent-fsync"), "--env", str(env_file)],
            capture_output=True,
            text=True,
            env=self._env(
                PATH=f"{fake_bin}:{os.environ['PATH']}",
                PYTHONPATH=str(audit_dir),
                FSYNC_AUDIT_LOG=str(audit_log),
            ),
            timeout=30,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("prior daemon.json restored", result.stderr)
        events = audit_log.read_text(encoding="utf-8").splitlines()
        unlink = events.index(f"U {self.daemon_dir / 'daemon.json'}")
        self.assertIn(f"F {self.daemon_dir}", events[unlink + 1 :])

    def test_first_apply_rollback_fsyncs_checkpoint_after_marker_unlink(self) -> None:
        self._write_daemon("{}\n")
        self._write_success_commands()
        (Path(self.tmp) / "probe.sh").write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
        (Path(self.tmp) / "probe.sh").chmod(0o755)
        checkpoint = Path(self.tmp) / "checkpoint-first-rollback-fsync"
        state_file = checkpoint / "docker-network-policy.json"
        audit_log = Path(self.tmp) / "first-rollback-fsync.log"
        audit_dir = Path(self.tmp) / "first-rollback-fsync-audit"
        audit_dir.mkdir()
        (audit_dir / "sitecustomize.py").write_text(
            "import os\n"
            "_unlink = os.unlink\n"
            "_fsync = os.fsync\n"
            "_log = os.environ['FSYNC_AUDIT_LOG']\n"
            "def record(value):\n"
            "    with open(_log, 'a', encoding='utf-8') as handle: handle.write(value + '\\n')\n"
            "def unlink(path, *args, **kwargs):\n"
            "    if os.path.realpath(path) == os.environ['STATE_FILE']: record('U ' + os.environ['STATE_FILE'])\n"
            "    return _unlink(path, *args, **kwargs)\n"
            "def fsync(fd):\n"
            "    record('F ' + os.path.realpath(f'/proc/self/fd/{fd}'))\n"
            "    return _fsync(fd)\n"
            "os.unlink = unlink\n"
            "os.fsync = fsync\n",
            encoding="utf-8",
        )
        fake_bin = Path(self.tmp) / "first-rollback-fsync-bin"
        fake_bin.mkdir()
        (fake_bin / "rm").write_text(
            "#!/usr/bin/env bash\n"
            f"if [[ ${{*: -1}} == /proc/self/fd/*/docker-network-policy.json ]]; then printf 'U %s\\n' {state_file} >> {audit_log}; fi\n"
            f"exec {shutil.which('rm')} \"$@\"\n",
            encoding="utf-8",
        )
        (fake_bin / "rm").chmod(0o755)
        env_file = self._write_env_file(self._rendered_with_policy())

        result = subprocess.run(
            [str(SCRIPTS / "apply-docker-network-policy.sh"), "--checkpoint", str(checkpoint), "--env", str(env_file)],
            capture_output=True,
            text=True,
            env=self._env(
                PATH=f"{fake_bin}:{os.environ['PATH']}",
                PYTHONPATH=str(audit_dir),
                FSYNC_AUDIT_LOG=str(audit_log),
                STATE_FILE=str(state_file),
            ),
            timeout=30,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("prior daemon.json restored", result.stderr)
        events = audit_log.read_text(encoding="utf-8").splitlines()
        unlink = events.index(f"U {state_file}")
        self.assertIn(f"F {checkpoint}", events[unlink + 1 :])

    def test_probe_failure_rolls_back_restarts_and_health_checks(self) -> None:
        prior = b'{"bip":"172.17.0.1/16","icc":false}\n'
        daemon = self.daemon_dir / "daemon.json"
        daemon.write_bytes(prior)
        restart_log = Path(self.tmp) / "restart.log"
        restart = Path(self.tmp) / "restart.sh"
        restart.write_text(
            f"#!/usr/bin/env bash\necho restart >> {restart_log}\n"
            f"[[ $(wc -l < {restart_log}) -eq 1 ]] || {{ echo '198.51.100.0/24 super-secret'; exit 1; }}\n",
            encoding="utf-8",
        )
        restart.chmod(0o755)
        probe = Path(self.tmp) / "probe.sh"
        probe.write_text(
            "#!/usr/bin/env bash\n"
            "echo '198.51.100.0/24 https://secret.example.invalid token=credential'\n"
            "echo 'probe-secret-error' >&2\n"
            "exit 1\n",
            encoding="utf-8",
        )
        probe.chmod(0o755)
        health_log = Path(self.tmp) / "health.log"
        health = Path(self.tmp) / "health.sh"
        health.write_text(
            "#!/usr/bin/env bash\n"
            f"[[ $1 == --env && $2 != {self.tmp}/ci-fleet.env ]] || exit 2\n"
            f"echo health >> {health_log}\n",
            encoding="utf-8",
        )
        health.chmod(0o755)
        env_file = self._write_env_file(self._rendered_with_policy())
        result = self._run(str(env_file), expected_rc=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(daemon.read_bytes(), prior)
        self.assertEqual(restart_log.read_text(encoding="utf-8").splitlines(), ["restart", "restart"])
        self.assertFalse(health_log.exists())
        self.assertNotIn("198.51.100.0/24", result.stdout + result.stderr)
        self.assertNotIn("super-secret", result.stdout + result.stderr)
        self.assertNotIn("secret.example.invalid", result.stdout + result.stderr)
        self.assertNotIn("credential", result.stdout + result.stderr)
        self.assertNotIn("probe-secret-error", result.stdout + result.stderr)

    def test_apply_rollback_preserves_concurrent_unrelated_settings(self) -> None:
        daemon = self._write_daemon('{"icc":false}\n')
        env_file = self._write_env_file(self._rendered_with_policy())
        command_log = Path(self.tmp) / "apply-merge-rollback.log"
        self.drain_command.write_text(f"#!/usr/bin/env bash\necho drain >> {command_log}\n", encoding="utf-8")
        for name in ("restart", "probe", "resume"):
            command = Path(self.tmp) / f"{name}.sh"
            command.write_text(f"#!/usr/bin/env bash\necho {name} >> {command_log}\n", encoding="utf-8")
            command.chmod(0o755)
        mutator = Path(self.tmp) / "mutate-apply-daemon.py"
        mutator.write_text(
            "import json, sys\n"
            "path = sys.argv[1]\n"
            "value = json.load(open(path))\n"
            "value['debug'] = True\n"
            "with open(path, 'w') as handle: json.dump(value, handle)\n",
            encoding="utf-8",
        )
        health = Path(self.tmp) / "health.sh"
        health.write_text(
            "#!/usr/bin/env bash\n"
            f"echo health >> {command_log}\n"
            'if grep -q "^CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_COUNT=" "$2"; then\n'
            f"  {shutil.which('python3')} {mutator} {daemon}\n"
            "  exit 2\n"
            "fi\n",
            encoding="utf-8",
        )
        health.chmod(0o755)

        result = self._run(str(env_file), expected_rc=1)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(daemon.read_text(encoding="utf-8")), {"debug": True, "icc": False})
        self.assertEqual(
            command_log.read_text(encoding="utf-8").splitlines(),
            ["drain", "restart", "probe", "resume", "health", "drain", "restart", "resume", "health"],
        )

    def test_interrupt_after_replace_rolls_back_and_retains_failed_recovery(self) -> None:
        prior = b'{"bip":"172.17.0.1/16"}\n'
        daemon = self.daemon_dir / "daemon.json"
        daemon.write_bytes(prior)
        ready = Path(self.tmp) / "restart.ready"
        restart_log = Path(self.tmp) / "restart.log"
        restart = Path(self.tmp) / "restart.sh"
        restart.write_text(
            "#!/usr/bin/env bash\n"
            f"echo restart >> {restart_log}\n"
            f"if [[ $(wc -l < {restart_log}) -eq 1 ]]; then\n"
            f"  touch {ready}\n"
            "  sleep 30\n"
            "else\n"
            "  echo rollback-restart-output\n"
            "  exit 1\n"
            "fi\n",
            encoding="utf-8",
        )
        restart.chmod(0o755)
        probe = Path(self.tmp) / "probe.sh"
        probe.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        probe.chmod(0o755)
        health_log = Path(self.tmp) / "health.log"
        health = Path(self.tmp) / "health.sh"
        health.write_text(
            f"#!/usr/bin/env bash\necho health >> {health_log}\necho rollback-health-output\n",
            encoding="utf-8",
        )
        health.chmod(0o755)
        env_file = self._write_env_file(self._rendered_with_policy())
        process = subprocess.Popen(
            [str(SCRIPTS / "apply-docker-network-policy.sh"), "--checkpoint", str(Path(self.tmp) / "checkpoint-direct"), "--env", str(env_file)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self._env(
                CI_FLEET_COMMAND_TIMEOUT_SECONDS="1",
                CI_FLEET_TEMP_DIR=self.tmp,
            ),
            start_new_session=True,
        )
        self.addCleanup(lambda: process.poll() is None and process.kill())
        for _ in range(300):
            if ready.exists():
                break
            time.sleep(0.02)
        self.assertTrue(ready.exists(), "apply did not reach restart after replacement")
        self.assertIn("default-address-pools", json.loads(daemon.read_text(encoding="utf-8")))

        os.killpg(process.pid, signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=10)

        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(daemon.read_bytes(), prior)
        self.assertEqual(restart_log.read_text(encoding="utf-8").splitlines(), ["restart", "restart"])
        self.assertFalse(health_log.exists())
        self.assertNotIn("rollback-restart-output", stdout + stderr)
        self.assertNotIn("rollback-health-output", stdout + stderr)
        recovery = Path((stdout + stderr).rstrip().rsplit("recovery data retained at ", 1)[1])
        self.assertEqual(recovery.parent, Path(self.tmp) / "checkpoint-direct")
        self.assertEqual((recovery / "daemon.json.before").read_bytes(), prior)

    def test_signal_during_failed_rollback_waits_for_durable_recovery(self) -> None:
        prior = b'{"bip":"172.17.0.1/16"}\n'
        self._write_daemon(prior.decode())
        checkpoint = Path(self.tmp) / "checkpoint-signal-during-rollback"
        rollback_ready = Path(self.tmp) / "rollback.ready"
        restart_count = Path(self.tmp) / "rollback-restart-count"
        restart = Path(self.tmp) / "restart.sh"
        restart.write_text(
            "#!/usr/bin/env bash\n"
            f"echo restart >> {restart_count}\n"
            f"if [[ $(wc -l < {restart_count}) -gt 1 ]]; then\n"
            f"  touch {rollback_ready}\n"
            "  sleep 1\n"
            "  exit 1\n"
            "fi\n",
            encoding="utf-8",
        )
        restart.chmod(0o755)
        (Path(self.tmp) / "probe.sh").write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
        (Path(self.tmp) / "probe.sh").chmod(0o755)
        (Path(self.tmp) / "health.sh").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        (Path(self.tmp) / "health.sh").chmod(0o755)
        env_file = self._write_env_file(self._rendered_with_policy())
        process = subprocess.Popen(
            [str(SCRIPTS / "apply-docker-network-policy.sh"), "--checkpoint", str(checkpoint), "--env", str(env_file)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self._env(CI_FLEET_COMMAND_TIMEOUT_SECONDS="5"),
        )
        self.addCleanup(lambda: process.poll() is None and process.kill())
        for _ in range(300):
            if rollback_ready.exists():
                break
            time.sleep(0.02)
        self.assertTrue(rollback_ready.exists(), "apply did not reach rollback restart")

        os.kill(process.pid, signal.SIGTERM)
        _, stderr = process.communicate(timeout=10)

        self.assertNotEqual(process.returncode, 0)
        self.assertIn("recovery data retained at", stderr)
        recoveries = list(checkpoint.glob("recovery.*"))
        self.assertEqual(len(recoveries), 1)
        self.assertEqual((recoveries[0] / "daemon.json.before").read_bytes(), prior)

    def test_rollback_failure_is_reported(self) -> None:
        prior = b'{"bip":"172.17.0.1/16","registry-mirrors":["https://mirror.example.invalid?token=credential"]}\n'
        self._write_daemon(prior.decode())
        restart_log = Path(self.tmp) / "restart.log"
        restart = Path(self.tmp) / "restart.sh"
        restart.write_text(
            f"#!/usr/bin/env bash\necho restart >> {restart_log}\n"
            f"[[ $(wc -l < {restart_log}) -eq 1 ]] || {{ echo '198.51.100.0/24 https://secret.example.invalid token=credential'; echo rollback-secret-error >&2; exit 1; }}\n",
            encoding="utf-8",
        )
        restart.chmod(0o755)
        probe = Path(self.tmp) / "probe.sh"
        probe.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
        probe.chmod(0o755)
        health = Path(self.tmp) / "health.sh"
        health.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        health.chmod(0o755)
        env_file = self._write_env_file(self._rendered_with_policy())

        result = self._run(str(env_file), expected_rc=1)

        self.assertNotEqual(result.returncode, 0)
        combined = result.stdout + result.stderr
        self.assertIn("rollback verification failed", combined)
        self.assertNotIn("prior daemon.json restored", combined)
        self.assertNotIn("198.51.100.0/24", combined)
        self.assertNotIn("172.17.0.1/16", combined)
        self.assertNotIn("secret.example.invalid", combined)
        self.assertNotIn("mirror.example.invalid", combined)
        self.assertNotIn("credential", combined)
        self.assertNotIn("rollback-secret-error", combined)
        recovery = Path(combined.rstrip().rsplit("recovery data retained at ", 1)[1])
        self.assertEqual(recovery.parent, Path(self.tmp) / "checkpoint-default")
        self.assertTrue(recovery.name.startswith("recovery."))
        self.assertEqual((recovery / "daemon.json.before").read_bytes(), prior)
        recovery_state = json.loads((Path(self.tmp) / "checkpoint-default" / "docker-network-policy.json").read_text(encoding="utf-8"))
        self.assertIsNone(recovery_state["verified_generation"])

    def test_failed_rollback_persists_exact_recovery_under_checkpoint(self) -> None:
        prior_daemon = b'{"bip":"172.17.0.1/16"}\n'
        self._write_daemon(prior_daemon.decode())
        prior_env = self.installed_env.read_bytes()
        checkpoint = Path(self.tmp) / "checkpoint-durable-recovery"
        restart_log = Path(self.tmp) / "durable-recovery-restart.log"
        restart = Path(self.tmp) / "restart.sh"
        restart.write_text(
            f"#!/usr/bin/env bash\necho restart >> {restart_log}\n"
            f"[[ $(wc -l < {restart_log}) -eq 1 ]]\n",
            encoding="utf-8",
        )
        restart.chmod(0o755)
        (Path(self.tmp) / "probe.sh").write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
        (Path(self.tmp) / "probe.sh").chmod(0o755)
        (Path(self.tmp) / "health.sh").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        (Path(self.tmp) / "health.sh").chmod(0o755)
        audit_dir = Path(self.tmp) / "recovery-fsync-audit"
        audit_dir.mkdir()
        audit_log = Path(self.tmp) / "recovery-fsync.log"
        (audit_dir / "sitecustomize.py").write_text(
            "import os\n"
            "_fsync = os.fsync\n"
            "_replace = os.replace\n"
            "_log = os.environ['FSYNC_AUDIT_LOG']\n"
            "def fsync(fd):\n"
            "    with open(_log, 'a', encoding='utf-8') as handle:\n"
            "        handle.write('F ' + os.readlink(f'/proc/self/fd/{fd}') + '\\n')\n"
            "    return _fsync(fd)\n"
            "def replace(source, target):\n"
            "    with open(_log, 'a', encoding='utf-8') as handle:\n"
            "        handle.write(f'R {os.path.realpath(source)} {os.path.realpath(target)}\\n')\n"
            "    return _replace(source, target)\n"
            "os.fsync = fsync\n"
            "os.replace = replace\n",
            encoding="utf-8",
        )
        env_file = self._write_env_file(self._rendered_with_policy())

        result = subprocess.run(
            [str(SCRIPTS / "apply-docker-network-policy.sh"), "--checkpoint", str(checkpoint), "--env", str(env_file)],
            capture_output=True,
            text=True,
            env=self._env(CI_FLEET_TEMP_DIR=self.tmp, PYTHONPATH=str(audit_dir), FSYNC_AUDIT_LOG=str(audit_log)),
            timeout=30,
        )

        self.assertNotEqual(result.returncode, 0)
        recovery = Path((result.stdout + result.stderr).rstrip().rsplit("recovery data retained at ", 1)[1])
        self.assertEqual(recovery.parent, checkpoint)
        self.assertEqual(recovery.stat().st_mode & 0o777, 0o700)
        self.assertEqual((recovery / "daemon.json.before").read_bytes(), prior_daemon)
        self.assertEqual((recovery / "prior-ci-fleet.env").read_bytes(), prior_env)
        self.assertEqual((recovery / "daemon.json.before").stat().st_mode & 0o777, 0o600)
        self.assertEqual((recovery / "prior-ci-fleet.env").stat().st_mode & 0o777, 0o600)
        self.assertFalse(list(Path(self.tmp).glob(".ci-fleet-apply.*")))
        audit = audit_log.read_text(encoding="utf-8").splitlines()
        self.assertTrue(any(line.startswith("F ") and line.endswith("/daemon.json.before") for line in audit))
        self.assertTrue(any(line.startswith("F ") and line.endswith("/prior-ci-fleet.env") for line in audit))
        self.assertIn(f"F {checkpoint}", audit)
        self.assertTrue(any(line.startswith("R ") and line.endswith(f" {recovery}") for line in audit))

    def test_managed_reapply_persists_recovery_before_daemon_restart(self) -> None:
        daemon = self._write_daemon('{"live-restore":true}\n')
        checkpoint = Path(self.tmp) / "checkpoint-reapply-recovery"
        self._write_success_commands()
        restart_count = Path(self.tmp) / "reapply-restart-count"
        restart = Path(self.tmp) / "restart.sh"
        restart.write_text(
            "#!/usr/bin/env bash\n"
            f"echo restart >> {restart_count}\n"
            f"if [[ $(wc -l < {restart_count}) -gt 1 ]]; then\n"
            f"  recovery=({checkpoint}/recovery.*)\n"
            '  [[ -f ${recovery[0]}/daemon.json.before && -f ${recovery[0]}/prior-ci-fleet.env ]] || exit 2\n'
            "fi\n",
            encoding="utf-8",
        )
        restart.chmod(0o755)
        policy_env = self._write_env_file(self._rendered_with_policy())
        applied = self._run(str(policy_env), checkpoint_dir=str(checkpoint))
        self.assertEqual(applied.returncode, 0, applied.stderr)
        prior_daemon = daemon.read_bytes()
        prior_env = policy_env.read_bytes()
        self.installed_env.write_bytes(prior_env)
        candidate = self._rendered_with_policy()
        candidate["CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_0_BASE"] = "192.0.2.0/24"
        candidate_env = self._write_env_file(candidate)

        reapplied = self._run(str(candidate_env), checkpoint_dir=str(checkpoint))

        self.assertEqual(reapplied.returncode, 0, reapplied.stderr)
        self.assertFalse(list(checkpoint.glob("recovery.*")))
        self.assertNotEqual(daemon.read_bytes(), prior_daemon)

    def test_signal_after_recovery_cleanup_cannot_run_rollback(self) -> None:
        daemon = self._write_daemon('{"live-restore":true}\n')
        checkpoint = Path(self.tmp) / "checkpoint-commit-cleanup-signal"
        self._write_success_commands()
        policy = self._rendered_with_policy()
        policy_env = self._write_env_file(policy)
        applied = self._run(str(policy_env), checkpoint_dir=str(checkpoint))
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.installed_env.write_bytes(policy_env.read_bytes())
        candidate = dict(policy)
        candidate["CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_0_BASE"] = "192.0.2.0/24"
        candidate_env = self._write_env_file(candidate)
        command_log = Path(self.tmp) / "commit-cleanup-signal.log"
        self.drain_command.write_text(f"#!/usr/bin/env bash\necho drain >> {command_log}\n", encoding="utf-8")
        for name in ("probe", "resume", "health"):
            command = Path(self.tmp) / f"{name}.sh"
            command.write_text(f"#!/usr/bin/env bash\necho {name} >> {command_log}\n", encoding="utf-8")
            command.chmod(0o755)
        restart = Path(self.tmp) / "restart.sh"
        restart.write_text(
            f"#!/usr/bin/env bash\necho restart >> {command_log}\n"
            f"[[ $(wc -l < {command_log}) -lt 7 ]]\n",
            encoding="utf-8",
        )
        restart.chmod(0o755)
        signal_marker = Path(self.tmp) / "cleanup-signal.sent"
        audit_dir = Path(self.tmp) / "cleanup-signal-audit"
        audit_dir.mkdir()
        (audit_dir / "sitecustomize.py").write_text(
            "import os, shutil, signal\n"
            "_rmtree = shutil.rmtree\n"
            "def rmtree(path, *args, **kwargs):\n"
            "    result = _rmtree(path, *args, **kwargs)\n"
            "    if os.path.basename(path).startswith('recovery.') and not os.path.exists(os.environ['SIGNAL_MARKER']):\n"
            "        open(os.environ['SIGNAL_MARKER'], 'w').close()\n"
            "        os.kill(os.getppid(), signal.SIGTERM)\n"
            "    return result\n"
            "shutil.rmtree = rmtree\n",
            encoding="utf-8",
        )

        reapplied = subprocess.run(
            [str(SCRIPTS / "apply-docker-network-policy.sh"), "--checkpoint", str(checkpoint), "--env", str(candidate_env)],
            capture_output=True,
            text=True,
            env=self._env(PYTHONPATH=str(audit_dir), SIGNAL_MARKER=str(signal_marker)),
            timeout=30,
        )

        self.assertEqual(reapplied.returncode, 0, reapplied.stderr)
        self.assertTrue(signal_marker.exists())
        self.assertEqual(
            command_log.read_text(encoding="utf-8").splitlines(),
            ["drain", "restart", "probe", "resume", "health"],
        )
        self.assertFalse(list(checkpoint.glob("recovery.*")))
        self.assertEqual(json.loads(daemon.read_text(encoding="utf-8"))["default-address-pools"][0]["base"], "192.0.2.0/24")

    def test_recovery_cleanup_failure_does_not_report_deleted_evidence(self) -> None:
        daemon = self._write_daemon('{"live-restore":true}\n')
        checkpoint = Path(self.tmp) / "checkpoint-commit-cleanup-failure"
        self._write_success_commands()
        policy = self._rendered_with_policy()
        policy_env = self._write_env_file(policy)
        applied = self._run(str(policy_env), checkpoint_dir=str(checkpoint))
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.installed_env.write_bytes(policy_env.read_bytes())
        candidate = dict(policy)
        candidate["CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_0_BASE"] = "192.0.2.0/24"
        candidate_env = self._write_env_file(candidate)
        restart_count = Path(self.tmp) / "commit-cleanup-failure-restarts"
        restart = Path(self.tmp) / "restart.sh"
        restart.write_text(
            f"#!/usr/bin/env bash\necho restart >> {restart_count}\n"
            f"[[ $(wc -l < {restart_count}) -eq 1 ]]\n",
            encoding="utf-8",
        )
        restart.chmod(0o755)
        audit_dir = Path(self.tmp) / "cleanup-failure-audit"
        audit_dir.mkdir()
        (audit_dir / "sitecustomize.py").write_text(
            "import os, shutil\n"
            "_rmtree = shutil.rmtree\n"
            "def rmtree(path, *args, **kwargs):\n"
            "    result = _rmtree(path, *args, **kwargs)\n"
            "    if os.path.basename(path).startswith('recovery.'):\n"
            "        raise OSError('injected recovery cleanup failure')\n"
            "    return result\n"
            "shutil.rmtree = rmtree\n",
            encoding="utf-8",
        )

        reapplied = subprocess.run(
            [str(SCRIPTS / "apply-docker-network-policy.sh"), "--checkpoint", str(checkpoint), "--env", str(candidate_env)],
            capture_output=True,
            text=True,
            env=self._env(PYTHONPATH=str(audit_dir), CI_FLEET_TEMP_DIR=self.tmp),
            timeout=30,
        )

        self.assertNotEqual(reapplied.returncode, 0)
        self.assertIn("failed to clear obsolete network-policy recovery data", reapplied.stderr)
        self.assertNotIn("recovery data retained at", reapplied.stderr)
        self.assertFalse(list(checkpoint.glob("recovery.*")))
        self.assertFalse(list(Path(self.tmp).glob(".ci-fleet-apply.*")))
        state = json.loads((checkpoint / "docker-network-policy.json").read_text(encoding="utf-8"))
        self.assertIsNotNone(state["verified_generation"])
        self.assertEqual(json.loads(daemon.read_text(encoding="utf-8"))["default-address-pools"][0]["base"], "192.0.2.0/24")

    def test_post_apply_health_uses_candidate_rendered_env(self) -> None:
        self._write_daemon("{}\n")
        for name in ("restart.sh", "probe.sh"):
            command = Path(self.tmp) / name
            command.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            command.chmod(0o755)
        health_log = Path(self.tmp) / "health.log"
        health = Path(self.tmp) / "health.sh"
        health.write_text(
            "#!/usr/bin/env bash\n"
            f"[[ $1 == --env && $2 != {self.tmp}/ci-fleet.env ]] || exit 2\n"
            f"printf '%s\\n' \"$2\" >> {health_log}\n",
            encoding="utf-8",
        )
        health.chmod(0o755)
        env_file = self._write_env_file(self._rendered_with_policy())

        result = self._run(str(env_file))

        self.assertEqual(result.returncode, 0, result.stderr)
        health_paths = health_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(health_paths), 1)
        self.assertNotEqual(health_paths[0], str(env_file))

    def test_apply_rejects_managed_pool_change_before_recording_generation(self) -> None:
        daemon = self._write_daemon("{}\n")
        env_file = self._write_env_file(self._rendered_with_policy())
        command_log = Path(self.tmp) / "final-pool-conflict.log"
        self.drain_command.write_text(f"#!/usr/bin/env bash\necho drain >> {command_log}\n", encoding="utf-8")
        for name in ("restart", "probe", "resume"):
            command = Path(self.tmp) / f"{name}.sh"
            command.write_text(f"#!/usr/bin/env bash\necho {name} >> {command_log}\n", encoding="utf-8")
            command.chmod(0o755)
        mutator = Path(self.tmp) / "mutate-final-pools.py"
        mutator.write_text(
            "import json, sys\n"
            "path = sys.argv[1]\n"
            "value = json.load(open(path))\n"
            "value['default-address-pools'] = [{'base': '192.0.2.0/24', 'size': 29}]\n"
            "with open(path, 'w') as handle: json.dump(value, handle)\n",
            encoding="utf-8",
        )
        health = Path(self.tmp) / "health.sh"
        health.write_text(
            "#!/usr/bin/env bash\n"
            f"echo health >> {command_log}\n"
            'if grep -q "^CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_COUNT=" "$2"; then\n'
            f"  {shutil.which('python3')} {mutator} {daemon}\n"
            "fi\n",
            encoding="utf-8",
        )
        health.chmod(0o755)

        result = self._run(str(env_file), expected_rc=1)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("daemon.json changed after network-policy verification", result.stderr)
        self.assertEqual(json.loads(daemon.read_text(encoding="utf-8")), {})
        self.assertEqual(
            command_log.read_text(encoding="utf-8").splitlines(),
            ["drain", "restart", "probe", "resume", "health", "drain", "restart", "resume", "health"],
        )

    def test_rollback_rejects_restored_pool_change_before_recording_generation(self) -> None:
        daemon = self._write_daemon("{}\n")
        checkpoint = Path(self.tmp) / "checkpoint-rollback-pool-conflict"
        self._write_success_commands()
        policy_a = self._rendered_with_policy()
        env_a = self._write_env_file(policy_a)
        applied = self._run(str(env_a), checkpoint_dir=str(checkpoint))
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.installed_env.write_bytes(env_a.read_bytes())
        state_file = checkpoint / "docker-network-policy.json"
        policy_b = dict(policy_a)
        policy_b["CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_0_BASE"] = "192.0.2.0/24"
        env_b = self._write_env_file(policy_b)
        health_count = Path(self.tmp) / "rollback-pool-health-count"
        mutator = Path(self.tmp) / "mutate-rollback-pools.py"
        mutator.write_text(
            "import json, sys\n"
            "path = sys.argv[1]\n"
            "value = json.load(open(path))\n"
            "value['default-address-pools'] = [{'base': '192.0.3.0/24', 'size': 29}]\n"
            "with open(path, 'w') as handle: json.dump(value, handle)\n",
            encoding="utf-8",
        )
        health = Path(self.tmp) / "health.sh"
        health.write_text(
            "#!/usr/bin/env bash\n"
            f"echo health >> {health_count}\n"
            f"if [[ $(wc -l < {health_count}) -eq 1 ]]; then exit 2; fi\n"
            f"{shutil.which('python3')} {mutator} {daemon}\n",
            encoding="utf-8",
        )
        health.chmod(0o755)

        result = self._run(str(env_b), checkpoint_dir=str(checkpoint))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rollback verification failed", result.stderr)
        self.assertIsNone(json.loads(state_file.read_text(encoding="utf-8"))["verified_generation"])
        self.assertEqual(len(list(checkpoint.glob("recovery.*"))), 1)

    def test_health_failure_evidence_excludes_command_output(self) -> None:
        self._write_daemon("{}\n")
        for name in ("restart.sh", "probe.sh"):
            command = Path(self.tmp) / name
            command.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            command.chmod(0o755)
        health = Path(self.tmp) / "health.sh"
        health.write_text(
            "#!/usr/bin/env bash\n"
            "echo '198.51.100.0/24 https://secret.example.invalid token=credential'\n"
            "echo 'health-secret-error' >&2\n"
            "exit 2\n",
            encoding="utf-8",
        )
        health.chmod(0o755)
        env_file = self._write_env_file(self._rendered_with_policy())

        result = self._run(str(env_file), expected_rc=1)

        self.assertNotEqual(result.returncode, 0)
        combined = result.stdout + result.stderr
        self.assertIn("ERROR: health check failed after network-policy restart", combined)
        self.assertNotIn("198.51.100.0/24", combined)
        self.assertNotIn("secret.example.invalid", combined)
        self.assertNotIn("credential", combined)
        self.assertNotIn("health-secret-error", combined)

    def test_sleeping_probe_times_out(self) -> None:
        self._write_daemon("{}\n")
        for name in ("drain.sh", "restart.sh", "health.sh"):
            command = Path(self.tmp) / name
            command.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            command.chmod(0o755)
        probe = Path(self.tmp) / "probe.sh"
        probe.write_text("#!/usr/bin/env bash\nsleep 10\n", encoding="utf-8")
        probe.chmod(0o755)
        env_file = self._write_env_file(self._rendered_with_policy())
        started = time.monotonic()
        result = subprocess.run(
            [str(SCRIPTS / "apply-docker-network-policy.sh"), "--checkpoint", str(Path(self.tmp) / "checkpoint-direct"), "--env", str(env_file)],
            capture_output=True,
            text=True,
            env=self._env(
                CI_FLEET_DOCKER_DRAIN_COMMAND=str(Path(self.tmp) / "drain.sh"),
                CI_FLEET_COMMAND_TIMEOUT_SECONDS="1",
            ),
            timeout=30,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertLess(time.monotonic() - started, 7)

    @unittest.skipUnless(Path("/dev/shm").is_dir(), "/dev/shm is unavailable")
    def test_apply_works_across_temp_filesystems(self) -> None:
        self._write_daemon(json.dumps({"bip": "172.17.0.1/16"}))
        for name in ("restart.sh", "probe.sh", "health.sh"):
            command = Path(self.tmp) / name
            command.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            command.chmod(0o755)
        env_file = self._write_env_file(self._rendered_with_policy())
        result = subprocess.run(
            [str(SCRIPTS / "apply-docker-network-policy.sh"), "--checkpoint", str(Path(self.tmp) / "checkpoint-direct"), "--env", str(env_file)],
            capture_output=True,
            text=True,
            env=self._env(CI_FLEET_TEMP_DIR="/dev/shm"),
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        daemon = json.loads((self.daemon_dir / "daemon.json").read_text(encoding="utf-8"))
        self.assertEqual(daemon["bip"], "172.17.0.1/16")
        self.assertIn("default-address-pools", daemon)

    def test_fsyncs_staged_file_and_daemon_directory_around_replace(self) -> None:
        daemon = self._write_daemon("{}\n")
        self._write_success_commands()
        env_file = self._write_env_file(self._rendered_with_policy())
        audit_dir = Path(self.tmp) / "audit-python"
        audit_dir.mkdir()
        audit_log = Path(self.tmp) / "fsync.log"
        (audit_dir / "sitecustomize.py").write_text(
            "import os\n"
            "_fsync = os.fsync\n"
            "_replace = os.replace\n"
            "_log = os.environ['FSYNC_AUDIT_LOG']\n"
            "def fsync(fd):\n"
            "    with open(_log, 'a', encoding='utf-8') as handle:\n"
            "        handle.write('F ' + os.readlink(f'/proc/self/fd/{fd}') + '\\n')\n"
            "    return _fsync(fd)\n"
            "def replace(source, target):\n"
            "    with open(_log, 'a', encoding='utf-8') as handle:\n"
            "        handle.write(f'R {os.path.realpath(source)} {os.path.realpath(target)}\\n')\n"
            "    return _replace(source, target)\n"
            "os.fsync = fsync\n"
            "os.replace = replace\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            [
                str(SCRIPTS / "apply-docker-network-policy.sh"),
                "--checkpoint",
                str(Path(self.tmp) / "checkpoint-fsync"),
                "--env",
                str(env_file),
            ],
            capture_output=True,
            text=True,
            env=self._env(PYTHONPATH=str(audit_dir), FSYNC_AUDIT_LOG=str(audit_log)),
            timeout=30,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        events = audit_log.read_text(encoding="utf-8").splitlines()
        replace_indexes = [i for i, event in enumerate(events) if event.startswith("R ")]
        for replace_index in replace_indexes:
            source = events[replace_index].split(" ", 2)[1]
            self.assertIn(f"F {source}", events[:replace_index])
        daemon_replace = next(i for i in replace_indexes if events[i].endswith(f" {daemon}"))
        self.assertEqual(events[daemon_replace + 1], f"F {self.daemon_dir}")

    @unittest.skipUnless(os.geteuid() == 0, "changing file GID requires root")
    def test_preserves_existing_daemon_config_gid(self) -> None:
        daemon = self._write_daemon("{}\n")
        daemon.chmod(0o640)
        os.chown(daemon, -1, 1)
        self._write_success_commands()
        env_file = self._write_env_file(self._rendered_with_policy())

        result = self._run(str(env_file))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(daemon.stat().st_gid, 1)

    def test_preserves_restrictive_daemon_config_mode(self) -> None:
        daemon = self._write_daemon("{}\n")
        daemon.chmod(0o600)
        for name in ("restart.sh", "probe.sh", "health.sh"):
            command = Path(self.tmp) / name
            command.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            command.chmod(0o755)
        env_file = self._write_env_file(self._rendered_with_policy())

        result = self._run(str(env_file))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(daemon.stat().st_mode & 0o777, 0o600)

    def test_applies_daemon_config_preserving_unrelated_keys(self) -> None:
        """GREEN: applying a policy preserves unrelated daemon.json keys."""
        self._write_daemon(json.dumps({"bip": "172.17.0.1/16", "icc": False}))
        restart = Path(self.tmp) / "restart.sh"
        restart.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        restart.chmod(0o755)
        probe = Path(self.tmp) / "probe.sh"
        probe.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        probe.chmod(0o755)
        health = Path(self.tmp) / "health.sh"
        health.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        health.chmod(0o755)
        env_file = self._write_env_file(self._rendered_with_policy())
        result = self._run(str(env_file))
        self.assertEqual(result.returncode, 0, result.stderr)
        daemon = json.loads((self.daemon_dir / "daemon.json").read_text(encoding="utf-8"))
        self.assertIn("default-address-pools", daemon)
        self.assertEqual(len(daemon["default-address-pools"]), 2)
        # Unrelated keys preserved
        self.assertEqual(daemon.get("bip"), "172.17.0.1/16")
        self.assertFalse(daemon.get("icc"))

    def test_rolls_back_on_probe_failure(self) -> None:
        """RED: if the capacity probe fails, the prior daemon config is restored."""
        prior = {"bip": "172.17.0.1/16", "icc": False}
        self._write_daemon(json.dumps(prior))
        restart = Path(self.tmp) / "restart.sh"
        restart.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        restart.chmod(0o755)
        probe = Path(self.tmp) / "probe.sh"
        probe.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")  # fails
        probe.chmod(0o755)
        health = Path(self.tmp) / "health.sh"
        health.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        health.chmod(0o755)
        env_file = self._write_env_file(self._rendered_with_policy())
        result = self._run(str(env_file), expected_rc=1)
        self.assertNotEqual(result.returncode, 0)
        daemon = json.loads((self.daemon_dir / "daemon.json").read_text(encoding="utf-8"))
        self.assertNotIn("default-address-pools", daemon)
        self.assertEqual(daemon, prior)

    def test_rolls_back_on_restart_failure(self) -> None:
        """RED: if the restart command fails, the prior daemon config is restored."""
        prior = {"bip": "172.17.0.1/16"}
        self._write_daemon(json.dumps(prior))
        restart = Path(self.tmp) / "restart.sh"
        restart.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")  # fails
        restart.chmod(0o755)
        probe = Path(self.tmp) / "probe.sh"
        probe.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        probe.chmod(0o755)
        health = Path(self.tmp) / "health.sh"
        health.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        health.chmod(0o755)
        env_file = self._write_env_file(self._rendered_with_policy())
        result = self._run(str(env_file), expected_rc=1)
        self.assertNotEqual(result.returncode, 0)
        daemon = json.loads((self.daemon_dir / "daemon.json").read_text(encoding="utf-8"))
        self.assertNotIn("default-address-pools", daemon)
        self.assertEqual(daemon, prior)

    def test_restart_uses_injected_command_boundary(self) -> None:
        """GREEN: restart never calls systemctl/dockerd directly."""
        self._write_daemon(json.dumps({}))
        restart = Path(self.tmp) / "restart.sh"
        restart.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"restart-invoked\" > \"$1/restart.log\"\nexit 0\n", encoding="utf-8")
        restart.chmod(0o755)
        probe = Path(self.tmp) / "probe.sh"
        probe.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        probe.chmod(0o755)
        health = Path(self.tmp) / "health.sh"
        health.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        health.chmod(0o755)
        env_file = self._write_env_file(self._rendered_with_policy())
        result = self._run(str(env_file))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.daemon_dir / "restart.log").exists())

    def test_restart_failure_evidence_excludes_command_output(self) -> None:
        self._write_daemon(json.dumps({}))
        restart = Path(self.tmp) / "restart.sh"
        restart.write_text(
            "#!/usr/bin/env bash\n"
            "echo '198.51.100.0/24 https://secret.example.invalid token=credential'\n"
            "echo 'restart-secret-error' >&2\n"
            "exit 1\n",
            encoding="utf-8",
        )
        restart.chmod(0o755)
        probe = Path(self.tmp) / "probe.sh"
        probe.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        probe.chmod(0o755)
        health = Path(self.tmp) / "health.sh"
        health.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        health.chmod(0o755)
        env = self._env(CI_FLEET_HEALTH_STATUS_URL="https://status.example.invalid/v1/status")
        env_file = self._write_env_file(self._rendered_with_policy())
        script = str(SCRIPTS / "apply-docker-network-policy.sh")
        result = subprocess.run(
            [script, "--checkpoint", str(Path(self.tmp) / "checkpoint-restart-failure"), "--env", env_file],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        self.assertNotEqual(result.returncode, 0)
        combined = result.stdout + result.stderr
        self.assertIn("ERROR: Docker restart command failed", combined)
        self.assertNotIn("198.51.100", combined)
        self.assertNotIn("203.0.113", combined)
        self.assertNotIn("secret.example.invalid", combined)
        self.assertNotIn("credential", combined)
        self.assertNotIn("restart-secret-error", combined)

    def test_removal_resumes_interrupted_first_apply_before_clearing_marker(self) -> None:
        daemon = self.daemon_dir / "daemon.json"
        self.assertFalse(daemon.exists())
        checkpoint = Path(self.tmp) / "checkpoint-interrupted-before-rename"
        checkpoint.mkdir(mode=0o700)
        state_file = checkpoint / "docker-network-policy.json"
        state_file.write_text(
            json.dumps({
                "managed": True,
                "prior_default_address_pools": None,
                "prior_default_address_pools_present": False,
                "prior_mode": None,
                "prior_present": False,
                "verified_generation": None,
            }),
            encoding="utf-8",
        )
        state_file.chmod(0o600)
        recovery = checkpoint / "recovery.interrupted"
        recovery.mkdir(mode=0o700)
        (recovery / "prior-ci-fleet.env").write_bytes(self.installed_env.read_bytes())
        (recovery / "prior-ci-fleet.env").chmod(0o600)
        command_markers = []
        for name in ("drain.sh", "restart.sh", "probe.sh", "resume.sh", "health.sh"):
            marker = Path(self.tmp) / f"interrupted-before-rename-{name}.marker"
            command_markers.append(marker)
            command = Path(self.tmp) / name
            command.write_text(f"#!/usr/bin/env bash\ntouch {marker}\n", encoding="utf-8")
            command.chmod(0o755)
        no_policy_env = self._write_env_file({"CI_FLEET_INSTANCE": "example-ci-01"})

        removed = self._run(str(no_policy_env), checkpoint_dir=str(checkpoint))

        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertEqual(removed.stdout, "NETWORK_POLICY_REMOVED\n")
        self.assertFalse(state_file.exists())
        self.assertFalse(recovery.exists())
        self.assertFalse(daemon.exists())
        self.assertEqual(
            [marker.name for marker in command_markers if marker.exists()],
            [
                "interrupted-before-rename-drain.sh.marker",
                "interrupted-before-rename-restart.sh.marker",
                "interrupted-before-rename-probe.sh.marker",
                "interrupted-before-rename-resume.sh.marker",
                "interrupted-before-rename-health.sh.marker",
            ],
        )

    def test_interrupted_first_apply_drain_failure_stops_before_restart_or_mutation(self) -> None:
        daemon = self.daemon_dir / "daemon.json"
        checkpoint = Path(self.tmp) / "checkpoint-interrupted-drain-failure"
        checkpoint.mkdir(mode=0o700)
        state_file = checkpoint / "docker-network-policy.json"
        state_file.write_text(
            json.dumps({
                "managed": True,
                "prior_default_address_pools": None,
                "prior_default_address_pools_present": False,
                "prior_mode": None,
                "prior_present": False,
                "verified_generation": None,
            }),
            encoding="utf-8",
        )
        state_file.chmod(0o600)
        recovery = checkpoint / "recovery.interrupted"
        recovery.mkdir(mode=0o700)
        (recovery / "prior-ci-fleet.env").write_bytes(self.installed_env.read_bytes())
        (recovery / "prior-ci-fleet.env").chmod(0o600)
        command_log = Path(self.tmp) / "interrupted-drain-failure.log"
        for name in ("drain", "restart", "probe", "resume", "health"):
            command = Path(self.tmp) / f"{name}.sh"
            command.write_text(
                f"#!/usr/bin/env bash\necho {name} >> {command_log}\n" + ("exit 1\n" if name == "drain" else ""),
                encoding="utf-8",
            )
            command.chmod(0o755)
        no_policy_env = self._write_env_file({"CI_FLEET_INSTANCE": "example-ci-01"})

        removed = self._run(str(no_policy_env), checkpoint_dir=str(checkpoint), expected_rc=1)

        self.assertNotEqual(removed.returncode, 0)
        self.assertFalse(daemon.exists())
        self.assertTrue(state_file.exists())
        self.assertTrue(recovery.exists())
        self.assertEqual(command_log.read_text(encoding="utf-8").splitlines(), ["drain", "resume", "health"])

    def test_removal_rejects_non_object_daemon_before_copy_or_commands(self) -> None:
        daemon = self._write_daemon("{}\n")
        checkpoint = Path(self.tmp) / "checkpoint-non-object-removal"
        self._write_success_commands()
        policy_env = self._write_env_file(self._rendered_with_policy())
        applied = self._run(str(policy_env), checkpoint_dir=str(checkpoint))
        self.assertEqual(applied.returncode, 0, applied.stderr)
        state_file = checkpoint / "docker-network-policy.json"
        prior_state = state_file.read_bytes()
        daemon.write_text("[]\n", encoding="utf-8")
        copy_marker = Path(self.tmp) / "non-object-copy.marker"
        command_markers = []
        for name in ("drain.sh", "restart.sh", "resume.sh", "health.sh"):
            marker = Path(self.tmp) / f"non-object-{name}.marker"
            command_markers.append(marker)
            command = Path(self.tmp) / name
            command.write_text(f"#!/usr/bin/env bash\ntouch {marker}\n", encoding="utf-8")
            command.chmod(0o755)
        fake_bin = Path(self.tmp) / "non-object-bin"
        fake_bin.mkdir()
        fake_cp = fake_bin / "cp"
        fake_cp.write_text(
            "#!/usr/bin/env bash\n"
            f"[[ ${{2:-}} != {daemon} ]] || touch {copy_marker}\n"
            f"exec {shutil.which('cp')} \"$@\"\n",
            encoding="utf-8",
        )
        fake_cp.chmod(0o755)
        no_policy_env = self._write_env_file({"CI_FLEET_INSTANCE": "example-ci-01"})

        removed = subprocess.run(
            [
                str(SCRIPTS / "apply-docker-network-policy.sh"),
                "--checkpoint",
                str(checkpoint),
                "--env",
                str(no_policy_env),
            ],
            capture_output=True,
            text=True,
            env=self._env(PATH=f"{fake_bin}:{os.environ['PATH']}"),
            timeout=30,
        )

        self.assertNotEqual(removed.returncode, 0)
        self.assertIn("managed daemon.json is not a valid JSON object", removed.stderr)
        self.assertEqual(daemon.read_text(encoding="utf-8"), "[]\n")
        self.assertEqual(state_file.read_bytes(), prior_state)
        self.assertFalse(copy_marker.exists())
        self.assertFalse(any(marker.exists() for marker in command_markers))

    def test_removal_daemon_change_during_drain_aborts_and_preserves_update(self) -> None:
        daemon = self._write_daemon('{"live-restore":true}\n')
        checkpoint = Path(self.tmp) / "checkpoint-removal-daemon-conflict"
        self._write_success_commands()
        policy_env = self._write_env_file(self._rendered_with_policy())
        applied = self._run(str(policy_env), checkpoint_dir=str(checkpoint))
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.installed_env.write_bytes(policy_env.read_bytes())
        state_file = checkpoint / "docker-network-policy.json"
        prior_state = state_file.read_bytes()
        changed = json.loads(daemon.read_text(encoding="utf-8"))
        changed["log-level"] = "debug"
        changed_bytes = json.dumps(changed).encode()
        command_log = Path(self.tmp) / "removal-daemon-conflict.log"
        mutator = Path(self.tmp) / "change-daemon-during-removal.py"
        mutator.write_text(
            "import json, sys\n"
            "path = sys.argv[1]\n"
            "value = json.load(open(path))\n"
            "value['log-level'] = 'debug'\n"
            "with open(path, 'w') as handle: json.dump(value, handle)\n",
            encoding="utf-8",
        )
        self.drain_command.write_text(
            "#!/usr/bin/env bash\n"
            f"echo drain >> {command_log}\n"
            f"{shutil.which('python3')} {mutator} {daemon}\n",
            encoding="utf-8",
        )
        for name in ("restart", "probe", "resume", "health"):
            command = Path(self.tmp) / f"{name}.sh"
            command.write_text(f"#!/usr/bin/env bash\necho {name} >> {command_log}\n", encoding="utf-8")
            command.chmod(0o755)
        no_policy_env = self._write_env_file({"CI_FLEET_INSTANCE": "example-ci-01"})

        removed = self._run(str(no_policy_env), checkpoint_dir=str(checkpoint), expected_rc=1)

        self.assertNotEqual(removed.returncode, 0)
        self.assertEqual(removed.stderr, "ERROR: daemon.json changed during network-policy removal\n")
        self.assertEqual(command_log.read_text(encoding="utf-8").splitlines(), ["drain", "resume", "health"])
        self.assertEqual(daemon.read_bytes(), changed_bytes)
        self.assertEqual(state_file.read_bytes(), prior_state)

    def test_removal_stages_from_the_conflict_detection_snapshot(self) -> None:
        daemon = self._write_daemon('{"live-restore":true}\n')
        checkpoint = Path(self.tmp) / "checkpoint-removal-snapshot-stage"
        self._write_success_commands()
        policy_env = self._write_env_file(self._rendered_with_policy())
        applied = self._run(str(policy_env), checkpoint_dir=str(checkpoint))
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.installed_env.write_bytes(policy_env.read_bytes())
        mutator = Path(self.tmp) / "mutate-before-removal-snapshot.py"
        mutator.write_text(
            "import json, sys\n"
            "value = json.load(open(sys.argv[1]))\n"
            "value['log-level'] = 'debug'\n"
            "with open(sys.argv[1], 'w') as handle: json.dump(value, handle)\n",
            encoding="utf-8",
        )
        fake_bin = Path(self.tmp) / "removal-snapshot-cp-bin"
        fake_bin.mkdir()
        cp = fake_bin / "cp"
        cp.write_text(
            "#!/usr/bin/env bash\n"
            f"if [[ $2 == {daemon} ]]; then {shutil.which('python3')} {mutator} {daemon}; fi\n"
            f"exec {shutil.which('cp')} \"$@\"\n",
            encoding="utf-8",
        )
        cp.chmod(0o755)
        no_policy_env = self._write_env_file({"CI_FLEET_INSTANCE": "example-ci-01"})

        removed = subprocess.run(
            [
                str(SCRIPTS / "apply-docker-network-policy.sh"),
                "--checkpoint",
                str(checkpoint),
                "--env",
                str(no_policy_env),
            ],
            capture_output=True,
            text=True,
            env=self._env(PATH=f"{fake_bin}:{os.environ['PATH']}"),
            timeout=30,
        )

        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertEqual(json.loads(daemon.read_text(encoding="utf-8"))["log-level"], "debug")

    def test_removal_restores_prior_pools_and_preserves_current_unrelated_keys(self) -> None:
        prior_pools = [{"base": "192.0.2.0/24", "size": 28}]
        daemon = self._write_daemon(json.dumps({"default-address-pools": prior_pools, "icc": False}))
        checkpoint = Path(self.tmp) / "checkpoint-prior-pools"
        self._write_success_commands()
        policy_env = self._write_env_file(self._rendered_with_policy())

        applied = self._run(str(policy_env), checkpoint_dir=str(checkpoint))

        self.assertEqual(applied.returncode, 0, applied.stderr)
        state = json.loads((checkpoint / "docker-network-policy.json").read_text(encoding="utf-8"))
        self.assertTrue(state["prior_default_address_pools_present"])
        self.assertEqual(state["prior_default_address_pools"], prior_pools)
        current = json.loads(daemon.read_text(encoding="utf-8"))
        current.pop("icc")
        current["live-restore"] = True
        daemon.write_text(json.dumps(current), encoding="utf-8")
        no_policy_env = self._write_env_file({"CI_FLEET_INSTANCE": "example-ci-01"})

        removed = self._run(str(no_policy_env), checkpoint_dir=str(checkpoint))

        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertEqual(
            json.loads(daemon.read_text(encoding="utf-8")),
            {"default-address-pools": prior_pools, "live-restore": True},
        )

    def test_removal_drops_only_managed_key_when_prior_key_was_absent(self) -> None:
        for original_file_present, current_unrelated in ((True, {"live-restore": True}), (False, {})):
            with self.subTest(original_file_present=original_file_present):
                daemon = self.daemon_dir / "daemon.json"
                daemon.unlink(missing_ok=True)
                if original_file_present:
                    daemon.write_text(json.dumps({"icc": False}), encoding="utf-8")
                checkpoint = Path(self.tmp) / f"checkpoint-prior-key-absent-{original_file_present}"
                self._write_success_commands()
                policy_env = self._write_env_file(self._rendered_with_policy())

                applied = self._run(str(policy_env), checkpoint_dir=str(checkpoint))

                self.assertEqual(applied.returncode, 0, applied.stderr)
                state = json.loads((checkpoint / "docker-network-policy.json").read_text(encoding="utf-8"))
                self.assertFalse(state["prior_default_address_pools_present"])
                self.assertIsNone(state["prior_default_address_pools"])
                managed = json.loads(daemon.read_text(encoding="utf-8"))
                managed.pop("icc", None)
                managed.update(current_unrelated)
                daemon.write_text(json.dumps(managed), encoding="utf-8")
                no_policy_env = self._write_env_file({"CI_FLEET_INSTANCE": "example-ci-01"})

                removed = self._run(str(no_policy_env), checkpoint_dir=str(checkpoint))

                self.assertEqual(removed.returncode, 0, removed.stderr)
                self.assertEqual(daemon.exists(), bool(current_unrelated) or original_file_present)
                if daemon.exists():
                    self.assertEqual(json.loads(daemon.read_text(encoding="utf-8")), current_unrelated)

    def test_persistent_provenance_has_no_recovery_backup_and_rejects_inconsistent_state(self) -> None:
        prior_pools = [{"base": "192.0.2.0/24", "size": 28}]
        daemon = self._write_daemon(json.dumps({"default-address-pools": prior_pools, "registry-mirrors": ["private-daemon-value"]}))
        checkpoint = Path(self.tmp) / "checkpoint-provenance-only"
        self._write_success_commands()
        policy_env = self._write_env_file(self._rendered_with_policy())

        applied = self._run(str(policy_env), checkpoint_dir=str(checkpoint))

        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertFalse((checkpoint / "daemon.json").exists())
        state_file = checkpoint / "docker-network-policy.json"
        state = json.loads(state_file.read_text(encoding="utf-8"))
        state["prior_default_address_pools_present"] = False
        state_file.write_text(json.dumps(state), encoding="utf-8")
        state_file.chmod(0o600)
        managed = daemon.read_bytes()
        drain_marker = Path(self.tmp) / "inconsistent-provenance-drain.marker"
        self.drain_command.write_text(f"#!/usr/bin/env bash\ntouch {drain_marker}\n", encoding="utf-8")
        no_policy_env = self._write_env_file({"CI_FLEET_INSTANCE": "example-ci-01"})

        removed = self._run(str(no_policy_env), checkpoint_dir=str(checkpoint), expected_rc=1)

        self.assertNotEqual(removed.returncode, 0)
        self.assertEqual(daemon.read_bytes(), managed)
        self.assertFalse(drain_marker.exists())
        self.assertIn("network-policy checkpoint state is invalid", removed.stderr)
        self.assertNotIn("private-daemon-value", removed.stdout + removed.stderr)
        self.assertNotIn("192.0.2.0/24", removed.stdout + removed.stderr)

    def test_removal_rejects_malformed_saved_pools_before_commands_or_mutation(self) -> None:
        prior_pools = [{"base": "192.0.2.0/24", "size": 28}]
        daemon = self._write_daemon(json.dumps({"default-address-pools": prior_pools}))
        checkpoint = Path(self.tmp) / "checkpoint-malformed-saved-pools"
        self._write_success_commands()
        policy_env = self._write_env_file(self._rendered_with_policy())
        applied = self._run(str(policy_env), checkpoint_dir=str(checkpoint))
        self.assertEqual(applied.returncode, 0, applied.stderr)
        state_file = checkpoint / "docker-network-policy.json"
        state = json.loads(state_file.read_text(encoding="utf-8"))
        state["prior_default_address_pools"] = [{"base": "not-a-cidr", "size": 29}]
        state_file.write_text(json.dumps(state), encoding="utf-8")
        state_file.chmod(0o600)
        managed = daemon.read_bytes()
        command_markers = []
        for name in ("drain.sh", "restart.sh", "probe.sh", "resume.sh", "health.sh"):
            marker = Path(self.tmp) / f"malformed-saved-pools-{name}.marker"
            command_markers.append(marker)
            command = Path(self.tmp) / name
            command.write_text(f"#!/usr/bin/env bash\ntouch {marker}\n", encoding="utf-8")
            command.chmod(0o755)
        no_policy_env = self._write_env_file({"CI_FLEET_INSTANCE": "example-ci-01"})

        removed = self._run(str(no_policy_env), checkpoint_dir=str(checkpoint), expected_rc=1)

        self.assertNotEqual(removed.returncode, 0)
        self.assertIn("network-policy checkpoint state is invalid", removed.stderr)
        self.assertEqual(daemon.read_bytes(), managed)
        self.assertFalse(any(marker.exists() for marker in command_markers))

    def test_removal_rejects_malformed_current_managed_pools_before_checkpoint_or_commands(self) -> None:
        for label, pools in (("malformed-entry", [{"base": "not-a-cidr", "size": 29}]), ("null", None)):
            with self.subTest(pools=label):
                daemon = self._write_daemon("{}\n")
                checkpoint = Path(self.tmp) / f"checkpoint-malformed-current-managed-pools-{label}"
                self._write_success_commands()
                policy_env = self._write_env_file(self._rendered_with_policy())
                applied = self._run(str(policy_env), checkpoint_dir=str(checkpoint))
                self.assertEqual(applied.returncode, 0, applied.stderr)
                state_file = checkpoint / "docker-network-policy.json"
                managed_state = state_file.read_bytes()
                malformed = json.dumps({"default-address-pools": pools, "live-restore": True}).encode()
                daemon.write_bytes(malformed)
                command_markers = []
                for name in ("drain.sh", "restart.sh", "probe.sh", "resume.sh", "health.sh"):
                    marker = Path(self.tmp) / f"malformed-current-managed-pools-{label}-{name}.marker"
                    command_markers.append(marker)
                    command = Path(self.tmp) / name
                    command.write_text(f"#!/usr/bin/env bash\ntouch {marker}\n", encoding="utf-8")
                    command.chmod(0o755)
                no_policy_env = self._write_env_file({"CI_FLEET_INSTANCE": "example-ci-01"})

                removed = self._run(str(no_policy_env), checkpoint_dir=str(checkpoint), expected_rc=1)

                self.assertNotEqual(removed.returncode, 0)
                self.assertIn("managed daemon.json is not a valid JSON object", removed.stderr)
                self.assertEqual(state_file.read_bytes(), managed_state)
                self.assertEqual(daemon.read_bytes(), malformed)
                self.assertFalse(any(marker.exists() for marker in command_markers))

    def test_removal_rejects_malformed_pending_pools_before_commands_or_mutation(self) -> None:
        daemon = self._write_daemon("{}\n")
        checkpoint = Path(self.tmp) / "checkpoint-malformed-pending-pools"
        self._write_success_commands()
        policy_env = self._write_env_file(self._rendered_with_policy())
        applied = self._run(str(policy_env), checkpoint_dir=str(checkpoint))
        self.assertEqual(applied.returncode, 0, applied.stderr)
        state_file = checkpoint / "docker-network-policy.json"
        state = json.loads(state_file.read_text(encoding="utf-8"))
        state["phase"] = "removal-pending"
        state["removal_managed_default_address_pools"] = []
        state_file.write_text(json.dumps(state), encoding="utf-8")
        state_file.chmod(0o600)
        managed = daemon.read_bytes()
        command_markers = []
        for name in ("drain.sh", "restart.sh", "probe.sh", "resume.sh", "health.sh"):
            marker = Path(self.tmp) / f"malformed-pending-pools-{name}.marker"
            command_markers.append(marker)
            command = Path(self.tmp) / name
            command.write_text(f"#!/usr/bin/env bash\ntouch {marker}\n", encoding="utf-8")
            command.chmod(0o755)
        no_policy_env = self._write_env_file({"CI_FLEET_INSTANCE": "example-ci-01"})

        removed = self._run(str(no_policy_env), checkpoint_dir=str(checkpoint), expected_rc=1)

        self.assertNotEqual(removed.returncode, 0)
        self.assertIn("network-policy checkpoint state is invalid", removed.stderr)
        self.assertEqual(daemon.read_bytes(), managed)
        self.assertFalse(any(marker.exists() for marker in command_markers))

    def test_managed_policy_removal_replaces_daemon_atomically(self) -> None:
        prior = b'{"icc":false}\n'
        daemon = self.daemon_dir / "daemon.json"
        daemon.write_bytes(prior)
        daemon.chmod(0o640)
        checkpoint = Path(self.tmp) / "checkpoint-atomic-removal"
        self._write_success_commands()
        policy_env = self._write_env_file(self._rendered_with_policy())
        applied = self._run(str(policy_env), checkpoint_dir=str(checkpoint))
        self.assertEqual(applied.returncode, 0, applied.stderr)

        no_policy_env = self._write_env_file({"CI_FLEET_INSTANCE": "example-ci-01"})
        fake_bin = Path(self.tmp) / "fake-cp-bin"
        fake_bin.mkdir()
        fake_cp = fake_bin / "cp"
        fake_cp.write_text(
            "#!/usr/bin/env bash\n"
            f"[[ ${{*: -1}} != {daemon} ]] || exit 99\n"
            f"exec {shutil.which('cp')} \"$@\"\n",
            encoding="utf-8",
        )
        fake_cp.chmod(0o755)
        removed = subprocess.run(
            [
                str(SCRIPTS / "apply-docker-network-policy.sh"),
                "--checkpoint",
                str(checkpoint),
                "--env",
                str(no_policy_env),
            ],
            capture_output=True,
            text=True,
            env=self._env(PATH=f"{fake_bin}:{os.environ['PATH']}"),
            timeout=30,
        )

        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertEqual(json.loads(daemon.read_text(encoding="utf-8")), json.loads(prior))
        self.assertEqual(daemon.stat().st_mode & 0o777, 0o640)

    def test_interrupted_removal_retry_uses_persisted_managed_pools_for_rollback(self) -> None:
        daemon = self._write_daemon('{"live-restore":true}\n')
        checkpoint = Path(self.tmp) / "checkpoint-interrupted-removal"
        self._write_success_commands()
        policy_env = self._write_env_file(self._rendered_with_policy())
        applied = self._run(str(policy_env), checkpoint_dir=str(checkpoint))
        self.assertEqual(applied.returncode, 0, applied.stderr)
        managed_pools = json.loads(daemon.read_text(encoding="utf-8"))["default-address-pools"]
        self.installed_env.write_bytes(policy_env.read_bytes())
        state_file = checkpoint / "docker-network-policy.json"
        state = json.loads(state_file.read_text(encoding="utf-8"))
        state["phase"] = "removal-pending"
        state["removal_managed_default_address_pools"] = managed_pools
        state_file.write_text(json.dumps(state), encoding="utf-8")
        state_file.chmod(0o600)
        daemon.write_text('{"live-restore":true}\n', encoding="utf-8")
        (Path(self.tmp) / "probe.sh").write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
        (Path(self.tmp) / "probe.sh").chmod(0o755)
        no_policy_env = self._write_env_file({"CI_FLEET_INSTANCE": "example-ci-01"})

        removed = self._run(str(no_policy_env), checkpoint_dir=str(checkpoint), expected_rc=1)

        self.assertNotEqual(removed.returncode, 0)
        self.assertEqual(
            json.loads(daemon.read_text(encoding="utf-8")),
            {"default-address-pools": managed_pools, "live-restore": True},
        )
        rolled_back_state = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertNotIn("phase", rolled_back_state)
        self.assertNotIn("removal_managed_default_address_pools", rolled_back_state)

    def test_pending_removal_with_absent_daemon_completes_runtime_verification(self) -> None:
        daemon = self.daemon_dir / "daemon.json"
        checkpoint = Path(self.tmp) / "checkpoint-pending-removal-absent-daemon"
        self._write_success_commands()
        policy_env = self._write_env_file(self._rendered_with_policy())
        applied = self._run(str(policy_env), checkpoint_dir=str(checkpoint))
        self.assertEqual(applied.returncode, 0, applied.stderr)
        state_file = checkpoint / "docker-network-policy.json"
        state = json.loads(state_file.read_text(encoding="utf-8"))
        state["phase"] = "removal-pending"
        state["removal_managed_default_address_pools"] = json.loads(
            daemon.read_text(encoding="utf-8")
        )["default-address-pools"]
        state["verified_generation"] = None
        state_file.write_text(json.dumps(state), encoding="utf-8")
        state_file.chmod(0o600)
        daemon.unlink()
        command_log = Path(self.tmp) / "pending-removal-absent-daemon.log"
        for name in ("restart", "probe", "resume", "health"):
            command = Path(self.tmp) / f"{name}.sh"
            command.write_text(f"#!/usr/bin/env bash\necho {name} >> {command_log}\n", encoding="utf-8")
            command.chmod(0o755)
        no_policy_env = self._write_env_file({"CI_FLEET_INSTANCE": "example-ci-01"})

        removed = self._run(str(no_policy_env), checkpoint_dir=str(checkpoint))

        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertEqual(removed.stdout, "NETWORK_POLICY_REMOVED\n")
        self.assertFalse(state_file.exists())
        self.assertFalse(daemon.exists())
        self.assertEqual(
            command_log.read_text(encoding="utf-8").splitlines(),
            ["restart", "probe", "resume", "health"],
        )

    def test_pending_removal_with_absent_daemon_failure_restores_managed_state(self) -> None:
        daemon = self.daemon_dir / "daemon.json"
        checkpoint = Path(self.tmp) / "checkpoint-pending-removal-absent-failure"
        self._write_success_commands()
        policy_env = self._write_env_file(self._rendered_with_policy())
        applied = self._run(str(policy_env), checkpoint_dir=str(checkpoint))
        self.assertEqual(applied.returncode, 0, applied.stderr)
        managed = daemon.read_bytes()
        self.installed_env.write_bytes(policy_env.read_bytes())
        state_file = checkpoint / "docker-network-policy.json"
        state = json.loads(state_file.read_text(encoding="utf-8"))
        state["phase"] = "removal-pending"
        state["removal_managed_default_address_pools"] = json.loads(managed)["default-address-pools"]
        state["verified_generation"] = None
        state_file.write_text(json.dumps(state), encoding="utf-8")
        state_file.chmod(0o600)
        daemon.unlink()
        command_log = Path(self.tmp) / "pending-removal-absent-failure.log"
        (Path(self.tmp) / "restart.sh").write_text(
            f"#!/usr/bin/env bash\necho restart >> {command_log}\n",
            encoding="utf-8",
        )
        (Path(self.tmp) / "probe.sh").write_text(
            f"#!/usr/bin/env bash\necho probe >> {command_log}\nexit 1\n",
            encoding="utf-8",
        )
        for name in ("resume", "health"):
            command = Path(self.tmp) / f"{name}.sh"
            command.write_text(
                "#!/usr/bin/env bash\n"
                f"echo {name} >> {command_log}\n"
                f"grep -Fqx 'CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_COUNT=2' \"$2\"\n",
                encoding="utf-8",
            )
        for name in ("restart", "probe", "resume", "health"):
            (Path(self.tmp) / f"{name}.sh").chmod(0o755)
        no_policy_env = self._write_env_file({"CI_FLEET_INSTANCE": "example-ci-01"})

        removed = self._run(str(no_policy_env), checkpoint_dir=str(checkpoint), expected_rc=1)

        self.assertNotEqual(removed.returncode, 0)
        self.assertEqual(daemon.read_bytes(), managed)
        restored_state = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertTrue(restored_state["managed"])
        self.assertNotIn("phase", restored_state)
        self.assertIsNotNone(restored_state["verified_generation"])
        self.assertEqual(
            command_log.read_text(encoding="utf-8").splitlines(),
            ["restart", "probe", "restart", "resume", "health"],
        )

    def test_pending_removal_with_absent_daemon_failed_rollback_retains_recovery(self) -> None:
        daemon = self.daemon_dir / "daemon.json"
        checkpoint = Path(self.tmp) / "checkpoint-pending-removal-absent-recovery"
        self._write_success_commands()
        policy_env = self._write_env_file(self._rendered_with_policy())
        applied = self._run(str(policy_env), checkpoint_dir=str(checkpoint))
        self.assertEqual(applied.returncode, 0, applied.stderr)
        managed = daemon.read_bytes()
        self.installed_env.write_bytes(policy_env.read_bytes())
        prior_env = self.installed_env.read_bytes()
        state_file = checkpoint / "docker-network-policy.json"
        state = json.loads(state_file.read_text(encoding="utf-8"))
        state["phase"] = "removal-pending"
        state["removal_managed_default_address_pools"] = json.loads(managed)["default-address-pools"]
        state["verified_generation"] = None
        state_file.write_text(json.dumps(state), encoding="utf-8")
        state_file.chmod(0o600)
        daemon.unlink()
        restart_log = Path(self.tmp) / "pending-removal-absent-recovery.log"
        restart = Path(self.tmp) / "restart.sh"
        restart.write_text(
            f"#!/usr/bin/env bash\necho restart >> {restart_log}\n"
            f"[[ $(wc -l < {restart_log}) -eq 1 ]]\n",
            encoding="utf-8",
        )
        restart.chmod(0o755)
        (Path(self.tmp) / "probe.sh").write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
        (Path(self.tmp) / "probe.sh").chmod(0o755)
        no_policy_env = self._write_env_file({"CI_FLEET_INSTANCE": "example-ci-01"})

        removed = self._run(str(no_policy_env), checkpoint_dir=str(checkpoint), expected_rc=1)

        self.assertNotEqual(removed.returncode, 0)
        self.assertIn("rollback verification failed; recovery data retained at", removed.stderr)
        recovery = next(checkpoint.glob("recovery.*"))
        self.assertEqual((recovery / "daemon.json.before").read_bytes(), managed)
        self.assertEqual((recovery / "prior-ci-fleet.env").read_bytes(), prior_env)
        retained_state = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertEqual(retained_state["phase"], "removal-pending")
        self.assertIsNone(retained_state["verified_generation"])

    def test_apply_from_pending_removal_persists_synthesized_rollback_daemon(self) -> None:
        daemon = self.daemon_dir / "daemon.json"
        checkpoint = Path(self.tmp) / "checkpoint-pending-removal-apply-recovery"
        self._write_success_commands()
        policy = self._rendered_with_policy()
        policy_env = self._write_env_file(policy)
        applied = self._run(str(policy_env), checkpoint_dir=str(checkpoint))
        self.assertEqual(applied.returncode, 0, applied.stderr)
        managed_pools = json.loads(daemon.read_text(encoding="utf-8"))["default-address-pools"]
        state_file = checkpoint / "docker-network-policy.json"
        state = json.loads(state_file.read_text(encoding="utf-8"))
        state["phase"] = "removal-pending"
        state["removal_managed_default_address_pools"] = managed_pools
        state["verified_generation"] = None
        state_file.write_text(json.dumps(state), encoding="utf-8")
        state_file.chmod(0o600)
        daemon.unlink()
        changed_policy = dict(policy)
        changed_policy["CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_0_BASE"] = "192.0.2.0/24"
        changed_env = self._write_env_file(changed_policy)
        (Path(self.tmp) / "restart.sh").write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
        (Path(self.tmp) / "restart.sh").chmod(0o755)

        retried = self._run(str(changed_env), checkpoint_dir=str(checkpoint), expected_rc=1)

        self.assertNotEqual(retried.returncode, 0)
        recovery = next(checkpoint.glob("recovery.*"))
        recovered_daemon = json.loads((recovery / "daemon.json.before").read_text(encoding="utf-8"))
        self.assertEqual(recovered_daemon["default-address-pools"], managed_pools)

    def test_configured_apply_resumes_removal_pending_checkpoint(self) -> None:
        daemon = self._write_daemon('{"live-restore":true}\n')
        checkpoint = Path(self.tmp) / "checkpoint-removal-pending-apply"
        self._write_success_commands()
        policy_env = self._write_env_file(self._rendered_with_policy())
        applied = self._run(str(policy_env), checkpoint_dir=str(checkpoint))
        self.assertEqual(applied.returncode, 0, applied.stderr)
        state_file = checkpoint / "docker-network-policy.json"
        state = json.loads(state_file.read_text(encoding="utf-8"))
        state["phase"] = "removal-pending"
        state["removal_managed_default_address_pools"] = json.loads(daemon.read_text(encoding="utf-8"))["default-address-pools"]
        state["verified_generation"] = None
        state_file.write_text(json.dumps(state), encoding="utf-8")
        state_file.chmod(0o600)
        daemon.write_text('{"live-restore":true}\n', encoding="utf-8")

        resumed = self._run(str(policy_env), checkpoint_dir=str(checkpoint))

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertIn("default-address-pools", json.loads(daemon.read_text(encoding="utf-8")))
        resumed_state = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertNotIn("phase", resumed_state)
        self.assertNotIn("removal_managed_default_address_pools", resumed_state)

    def test_failed_configured_apply_from_removal_pending_restores_managed_pools(self) -> None:
        daemon = self._write_daemon('{"live-restore":true}\n')
        checkpoint = Path(self.tmp) / "checkpoint-removal-pending-apply-rollback"
        self._write_success_commands()
        policy_a = self._rendered_with_policy()
        env_a = self._write_env_file(policy_a)
        applied = self._run(str(env_a), checkpoint_dir=str(checkpoint))
        self.assertEqual(applied.returncode, 0, applied.stderr)
        managed = daemon.read_bytes()
        managed_pools = json.loads(managed)["default-address-pools"]
        state_file = checkpoint / "docker-network-policy.json"
        state = json.loads(state_file.read_text(encoding="utf-8"))
        state["phase"] = "removal-pending"
        state["removal_managed_default_address_pools"] = managed_pools
        state["verified_generation"] = None
        state_file.write_text(json.dumps(state), encoding="utf-8")
        state_file.chmod(0o600)
        policy_b = dict(policy_a)
        policy_b["CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_0_BASE"] = "192.0.2.0/24"
        env_b = self._write_env_file(policy_b)
        (Path(self.tmp) / "probe.sh").write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
        (Path(self.tmp) / "probe.sh").chmod(0o755)

        retried = self._run(str(env_b), checkpoint_dir=str(checkpoint), expected_rc=1)

        self.assertNotEqual(retried.returncode, 0)
        self.assertEqual(daemon.read_bytes(), managed)

    def test_configured_apply_rejects_invalid_removal_pending_pools_before_drain(self) -> None:
        daemon = self._write_daemon("{}\n")
        checkpoint = Path(self.tmp) / "checkpoint-invalid-removal-pending-apply"
        self._write_success_commands()
        policy_env = self._write_env_file(self._rendered_with_policy())
        applied = self._run(str(policy_env), checkpoint_dir=str(checkpoint))
        self.assertEqual(applied.returncode, 0, applied.stderr)
        state_file = checkpoint / "docker-network-policy.json"
        state = json.loads(state_file.read_text(encoding="utf-8"))
        state["phase"] = "removal-pending"
        state["removal_managed_default_address_pools"] = []
        state_file.write_text(json.dumps(state), encoding="utf-8")
        state_file.chmod(0o600)
        managed = daemon.read_bytes()
        drain_marker = Path(self.tmp) / "invalid-removal-pending-apply-drain.marker"
        self.drain_command.write_text(f"#!/usr/bin/env bash\ntouch {drain_marker}\n", encoding="utf-8")

        rejected = self._run(str(policy_env), checkpoint_dir=str(checkpoint))

        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(daemon.read_bytes(), managed)
        self.assertFalse(drain_marker.exists())

    def test_removal_failure_restores_snapshot_with_absent_managed_key(self) -> None:
        daemon = self._write_daemon('{"live-restore":true}\n')
        checkpoint = Path(self.tmp) / "checkpoint-absent-managed-key"
        self._write_success_commands()
        policy_env = self._write_env_file(self._rendered_with_policy())
        applied = self._run(str(policy_env), checkpoint_dir=str(checkpoint))
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.installed_env.write_bytes(policy_env.read_bytes())
        daemon.write_text('{"debug":true,"live-restore":true}\n', encoding="utf-8")
        no_policy_env = self._write_env_file({"CI_FLEET_INSTANCE": "example-ci-01"})
        command_log = Path(self.tmp) / "absent-managed-key.log"
        self.drain_command.write_text(f"#!/usr/bin/env bash\necho drain >> {command_log}\n", encoding="utf-8")
        restart = Path(self.tmp) / "restart.sh"
        restart.write_text(f"#!/usr/bin/env bash\necho restart >> {command_log}\n", encoding="utf-8")
        restart.chmod(0o755)
        resume = Path(self.tmp) / "resume.sh"
        resume.write_text(
            "#!/usr/bin/env bash\n"
            f"echo resume >> {command_log}\n"
            'grep -q "^CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_COUNT=" "$2"\n',
            encoding="utf-8",
        )
        resume.chmod(0o755)

        removed = self._run(str(no_policy_env), checkpoint_dir=str(checkpoint), expected_rc=1)

        self.assertNotEqual(removed.returncode, 0)
        self.assertIn("managed daemon.json restored", removed.stderr)
        self.assertEqual(json.loads(daemon.read_text(encoding="utf-8")), {"debug": True, "live-restore": True})
        self.assertEqual(command_log.read_text(encoding="utf-8").splitlines(), ["drain", "restart", "resume", "drain", "restart", "resume"])

    def test_removal_pending_state_is_durable_before_daemon_mutation(self) -> None:
        daemon = self._write_daemon("{}\n")
        checkpoint = Path(self.tmp) / "checkpoint-durable-removal-pending"
        self._write_success_commands()
        policy_env = self._write_env_file(self._rendered_with_policy())
        applied = self._run(str(policy_env), checkpoint_dir=str(checkpoint))
        self.assertEqual(applied.returncode, 0, applied.stderr)
        managed_pools = json.loads(daemon.read_text(encoding="utf-8"))["default-address-pools"]
        state_file = checkpoint / "docker-network-policy.json"
        self.drain_command.write_text(
            "#!/usr/bin/env bash\n"
            f"grep -Fq '\"phase\": \"removal-pending\"' {state_file}\n",
            encoding="utf-8",
        )
        audit_dir = Path(self.tmp) / "removal-pending-audit"
        audit_dir.mkdir()
        audit_log = Path(self.tmp) / "removal-pending.log"
        (audit_dir / "sitecustomize.py").write_text(
            "import json, os\n"
            "_fsync = os.fsync\n"
            "_replace = os.replace\n"
            "_log = os.environ['AUDIT_LOG']\n"
            "def record(value):\n"
            "    with open(_log, 'a', encoding='utf-8') as handle: handle.write(value + '\\n')\n"
            "def fsync(fd):\n"
            "    record('F ' + os.path.realpath(f'/proc/self/fd/{fd}'))\n"
            "    return _fsync(fd)\n"
            "def replace(source, target):\n"
            "    target = os.path.realpath(target)\n"
            "    if target == os.environ['DAEMON']:\n"
            "        state = json.load(open(os.environ['STATE'], encoding='utf-8'))\n"
            "        assert state['phase'] == 'removal-pending'\n"
            "        assert state['removal_managed_default_address_pools'] == json.loads(os.environ['POOLS'])\n"
            "    record('R ' + target)\n"
            "    return _replace(source, target)\n"
            "os.fsync = fsync\n"
            "os.replace = replace\n",
            encoding="utf-8",
        )
        no_policy_env = self._write_env_file({"CI_FLEET_INSTANCE": "example-ci-01"})

        removed = subprocess.run(
            [str(SCRIPTS / "apply-docker-network-policy.sh"), "--checkpoint", str(checkpoint), "--env", str(no_policy_env)],
            capture_output=True,
            text=True,
            env=self._env(
                PYTHONPATH=str(audit_dir),
                AUDIT_LOG=str(audit_log),
                DAEMON=str(daemon),
                STATE=str(state_file),
                POOLS=json.dumps(managed_pools),
            ),
            timeout=30,
        )

        self.assertEqual(removed.returncode, 0, removed.stderr)
        events = audit_log.read_text(encoding="utf-8").splitlines()
        state_replace = events.index(f"R {state_file}")
        daemon_replace = events.index(f"R {daemon}")
        self.assertTrue(any(event.startswith("F ") and ".docker-network-policy." in event for event in events[:state_replace]))
        self.assertIn(f"F {checkpoint}", events[state_replace + 1 : daemon_replace])

    def test_uncertain_removal_rollback_retains_pending_state(self) -> None:
        daemon = self._write_daemon("{}\n")
        checkpoint = Path(self.tmp) / "checkpoint-uncertain-removal"
        self._write_success_commands()
        policy_env = self._write_env_file(self._rendered_with_policy())
        applied = self._run(str(policy_env), checkpoint_dir=str(checkpoint))
        self.assertEqual(applied.returncode, 0, applied.stderr)
        managed_pools = json.loads(daemon.read_text(encoding="utf-8"))["default-address-pools"]
        self.installed_env.write_bytes(policy_env.read_bytes())
        restart_log = Path(self.tmp) / "uncertain-removal-restart.log"
        restart = Path(self.tmp) / "restart.sh"
        restart.write_text(
            f"#!/usr/bin/env bash\necho restart >> {restart_log}\n"
            f"[[ $(wc -l < {restart_log}) -eq 1 ]]\n",
            encoding="utf-8",
        )
        restart.chmod(0o755)
        (Path(self.tmp) / "probe.sh").write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
        (Path(self.tmp) / "probe.sh").chmod(0o755)
        resume_marker = Path(self.tmp) / "uncertain-removal-resume.marker"
        (Path(self.tmp) / "resume.sh").write_text(
            f"#!/usr/bin/env bash\ntouch {resume_marker}\n",
            encoding="utf-8",
        )
        (Path(self.tmp) / "resume.sh").chmod(0o755)
        no_policy_env = self._write_env_file({"CI_FLEET_INSTANCE": "example-ci-01"})

        removed = self._run(str(no_policy_env), checkpoint_dir=str(checkpoint), expected_rc=1)

        self.assertNotEqual(removed.returncode, 0)
        state = json.loads((checkpoint / "docker-network-policy.json").read_text(encoding="utf-8"))
        self.assertEqual(state["phase"], "removal-pending")
        self.assertEqual(state["removal_managed_default_address_pools"], managed_pools)
        self.assertFalse(resume_marker.exists())

    def test_failed_removal_from_absent_baseline_persists_durable_recovery(self) -> None:
        daemon = self.daemon_dir / "daemon.json"
        self.assertFalse(daemon.exists())
        checkpoint = Path(self.tmp) / "checkpoint-durable-removal-recovery"
        self._write_success_commands()
        policy_env = self._write_env_file(self._rendered_with_policy())
        applied = self._run(str(policy_env), checkpoint_dir=str(checkpoint))
        self.assertEqual(applied.returncode, 0, applied.stderr)
        managed = daemon.read_bytes()
        managed_pools = json.loads(managed)["default-address-pools"]
        self.installed_env.write_bytes(policy_env.read_bytes())
        prior_env = self.installed_env.read_bytes()
        restart_log = Path(self.tmp) / "durable-removal-restart.log"
        restart = Path(self.tmp) / "restart.sh"
        restart.write_text(
            f"#!/usr/bin/env bash\necho restart >> {restart_log}\n"
            f"[[ $(wc -l < {restart_log}) -eq 1 ]]\n",
            encoding="utf-8",
        )
        restart.chmod(0o755)
        (Path(self.tmp) / "probe.sh").write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
        (Path(self.tmp) / "probe.sh").chmod(0o755)
        no_policy_env = self._write_env_file({"CI_FLEET_INSTANCE": "example-ci-01"})

        removed = subprocess.run(
            [str(SCRIPTS / "apply-docker-network-policy.sh"), "--checkpoint", str(checkpoint), "--env", str(no_policy_env)],
            capture_output=True,
            text=True,
            env=self._env(CI_FLEET_TEMP_DIR=self.tmp),
            timeout=30,
        )

        self.assertNotEqual(removed.returncode, 0)
        combined = removed.stdout + removed.stderr
        recovery = Path(combined.rstrip().rsplit("recovery data retained at ", 1)[1])
        for volatile in Path(self.tmp).glob(".ci-fleet-apply.*"):
            shutil.rmtree(volatile)
        self.assertEqual(recovery.parent, checkpoint)
        self.assertEqual((recovery / "daemon.json.before").read_bytes(), managed)
        self.assertEqual((recovery / "prior-ci-fleet.env").read_bytes(), prior_env)
        state = json.loads((checkpoint / "docker-network-policy.json").read_text(encoding="utf-8"))
        self.assertEqual(state["phase"], "removal-pending")
        self.assertEqual(state["removal_managed_default_address_pools"], managed_pools)
        self.assertIsNone(state["verified_generation"])

    def test_removal_probe_failure_rolls_back_before_resume_health_or_marker_clear(self) -> None:
        daemon = self._write_daemon("{}\n")
        checkpoint = Path(self.tmp) / "checkpoint-removal-probe-failure"
        self._write_success_commands()
        policy_env = self._write_env_file(self._rendered_with_policy())
        applied = self._run(str(policy_env), checkpoint_dir=str(checkpoint))
        self.assertEqual(applied.returncode, 0, applied.stderr)
        managed = daemon.read_bytes()
        self.installed_env.write_bytes(policy_env.read_bytes())
        state_file = checkpoint / "docker-network-policy.json"
        state = state_file.read_bytes()
        no_policy_env = self._write_env_file({"CI_FLEET_INSTANCE": "example-ci-01"})
        command_log = Path(self.tmp) / "removal-probe-failure.log"
        self.drain_command.write_text(f"#!/usr/bin/env bash\necho drain >> {command_log}\n", encoding="utf-8")
        for name in ("restart", "resume", "health"):
            command = Path(self.tmp) / f"{name}.sh"
            command.write_text(f"#!/usr/bin/env bash\necho {name} >> {command_log}\n", encoding="utf-8")
            command.chmod(0o755)
        probe = Path(self.tmp) / "probe.sh"
        probe.write_text(f"#!/usr/bin/env bash\necho probe >> {command_log}\nexit 1\n", encoding="utf-8")
        probe.chmod(0o755)

        removed = self._run(str(no_policy_env), checkpoint_dir=str(checkpoint), expected_rc=1)

        self.assertNotEqual(removed.returncode, 0)
        self.assertEqual(daemon.read_bytes(), managed)
        self.assertEqual(state_file.read_bytes(), state)
        self.assertEqual(
            command_log.read_text(encoding="utf-8").splitlines(),
            ["drain", "restart", "probe", "restart", "resume", "health"],
        )

    def test_successful_removal_runs_runtime_checks_before_deleting_marker(self) -> None:
        self._write_daemon("{}\n")
        checkpoint = Path(self.tmp) / "checkpoint-removal-resume"
        self._write_success_commands()
        policy_env = self._write_env_file(self._rendered_with_policy())
        applied = self._run(str(policy_env), checkpoint_dir=str(checkpoint))
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.installed_env.write_bytes(policy_env.read_bytes())
        state_file = checkpoint / "docker-network-policy.json"
        no_policy_env = self._write_env_file({"CI_FLEET_INSTANCE": "example-ci-01"})
        command_log = Path(self.tmp) / "removal-resume.log"
        self.drain_command.write_text(f"#!/usr/bin/env bash\necho drain >> {command_log}\n", encoding="utf-8")
        for name in ("restart", "probe", "resume", "health"):
            command = Path(self.tmp) / f"{name}.sh"
            command.write_text(
                "#!/usr/bin/env bash\n"
                f"grep -Fq '\"phase\": \"removal-pending\"' {state_file} || exit 2\n"
                f"echo {name} >> {command_log}\n",
                encoding="utf-8",
            )
            command.chmod(0o755)

        removed = self._run(str(no_policy_env), checkpoint_dir=str(checkpoint))

        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertEqual(command_log.read_text(encoding="utf-8").splitlines(), ["drain", "restart", "probe", "resume", "health"])
        self.assertFalse(state_file.exists())

    def test_successful_removal_clears_recovery_before_marker_and_allows_next_reconciliation(self) -> None:
        self._write_daemon("{}\n")
        checkpoint = Path(self.tmp) / "checkpoint-removal-recovery-cleanup"
        self._write_success_commands()
        policy_env = self._write_env_file(self._rendered_with_policy())
        applied = self._run(str(policy_env), checkpoint_dir=str(checkpoint))
        self.assertEqual(applied.returncode, 0, applied.stderr)
        state_file = checkpoint / "docker-network-policy.json"
        recovery = checkpoint / "recovery.stale"
        recovery.mkdir(mode=0o700)
        (recovery / "prior-ci-fleet.env").write_bytes(self.installed_env.read_bytes())
        (recovery / "prior-ci-fleet.env").chmod(0o600)
        audit_log = Path(self.tmp) / "removal-recovery-cleanup.log"
        audit_dir = Path(self.tmp) / "removal-recovery-cleanup-audit"
        audit_dir.mkdir()
        (audit_dir / "sitecustomize.py").write_text(
            "import os, shutil\n"
            "_unlink = os.unlink\n"
            "_rmtree = shutil.rmtree\n"
            "_log = os.environ['CLEANUP_AUDIT_LOG']\n"
            "def record(value):\n"
            "    with open(_log, 'a', encoding='utf-8') as handle: handle.write(value + '\\n')\n"
            "def unlink(path, *args, **kwargs):\n"
            "    if os.path.realpath(path) == os.environ['STATE_FILE']: record('marker')\n"
            "    return _unlink(path, *args, **kwargs)\n"
            "def rmtree(path, *args, **kwargs):\n"
            "    if os.path.realpath(path) == os.environ['RECOVERY_DIR']: record('recovery')\n"
            "    return _rmtree(path, *args, **kwargs)\n"
            "os.unlink = unlink\n"
            "shutil.rmtree = rmtree\n",
            encoding="utf-8",
        )
        no_policy_env = self._write_env_file({"CI_FLEET_INSTANCE": "example-ci-01"})

        removed = subprocess.run(
            [str(SCRIPTS / "apply-docker-network-policy.sh"), "--checkpoint", str(checkpoint), "--env", str(no_policy_env)],
            capture_output=True,
            text=True,
            env=self._env(
                PYTHONPATH=str(audit_dir),
                CLEANUP_AUDIT_LOG=str(audit_log),
                STATE_FILE=str(state_file),
                RECOVERY_DIR=str(recovery),
            ),
            timeout=30,
        )

        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertEqual(audit_log.read_text(encoding="utf-8").splitlines(), ["recovery", "marker"])
        self.assertFalse(recovery.exists())
        reconciled = self._run(str(no_policy_env), checkpoint_dir=str(checkpoint))
        self.assertEqual(reconciled.returncode, 0, reconciled.stderr)
        self.assertEqual(reconciled.stdout, "NETWORK_POLICY_NOOP\n")

    def test_removal_recovery_cleanup_failure_rolls_back_and_keeps_marker_usable(self) -> None:
        daemon = self._write_daemon("{}\n")
        checkpoint = Path(self.tmp) / "checkpoint-removal-recovery-cleanup-failure"
        self._write_success_commands()
        policy_env = self._write_env_file(self._rendered_with_policy())
        applied = self._run(str(policy_env), checkpoint_dir=str(checkpoint))
        self.assertEqual(applied.returncode, 0, applied.stderr)
        managed = daemon.read_bytes()
        self.installed_env.write_bytes(policy_env.read_bytes())
        state_file = checkpoint / "docker-network-policy.json"
        recovery = checkpoint / "recovery.invalid"
        recovery.mkdir(mode=0o700)
        (recovery / "prior-ci-fleet.env").write_bytes(self.installed_env.read_bytes())
        (recovery / "prior-ci-fleet.env").chmod(0o600)
        (recovery / "unexpected").write_text("invalid\n", encoding="utf-8")
        (recovery / "unexpected").chmod(0o600)
        no_policy_env = self._write_env_file({"CI_FLEET_INSTANCE": "example-ci-01"})

        removed = self._run(str(no_policy_env), checkpoint_dir=str(checkpoint), expected_rc=1)

        self.assertNotEqual(removed.returncode, 0)
        self.assertIn("failed to clear obsolete network-policy recovery data", removed.stderr)
        self.assertEqual(daemon.read_bytes(), managed)
        state = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertTrue(state["managed"])
        self.assertNotIn("phase", state)
        self.assertTrue(recovery.exists())
        shutil.rmtree(recovery)
        retried = self._run(str(no_policy_env), checkpoint_dir=str(checkpoint))
        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assertEqual(retried.stdout, "NETWORK_POLICY_REMOVED\n")

    def test_successful_removal_fsyncs_checkpoint_after_marker_unlink(self) -> None:
        self._write_daemon("{}\n")
        checkpoint = Path(self.tmp) / "checkpoint-marker-fsync"
        self._write_success_commands()
        policy_env = self._write_env_file(self._rendered_with_policy())
        applied = self._run(str(policy_env), checkpoint_dir=str(checkpoint))
        self.assertEqual(applied.returncode, 0, applied.stderr)
        state_file = checkpoint / "docker-network-policy.json"
        audit_log = Path(self.tmp) / "marker-fsync.log"
        audit_dir = Path(self.tmp) / "marker-fsync-audit"
        audit_dir.mkdir()
        (audit_dir / "sitecustomize.py").write_text(
            "import os\n"
            "_unlink = os.unlink\n"
            "_fsync = os.fsync\n"
            "_log = os.environ['FSYNC_AUDIT_LOG']\n"
            "def record(value):\n"
            "    with open(_log, 'a', encoding='utf-8') as handle: handle.write(value + '\\n')\n"
            "def unlink(path, *args, **kwargs):\n"
            "    if os.path.realpath(path) == os.environ['STATE_FILE']: record('U ' + os.environ['STATE_FILE'])\n"
            "    return _unlink(path, *args, **kwargs)\n"
            "def fsync(fd):\n"
            "    record('F ' + os.path.realpath(f'/proc/self/fd/{fd}'))\n"
            "    return _fsync(fd)\n"
            "os.unlink = unlink\n"
            "os.fsync = fsync\n",
            encoding="utf-8",
        )
        fake_bin = Path(self.tmp) / "marker-fsync-bin"
        fake_bin.mkdir()
        (fake_bin / "rm").write_text(
            "#!/usr/bin/env bash\n"
            f"if [[ ${{*: -1}} == /proc/self/fd/*/docker-network-policy.json ]]; then printf 'U %s\\n' {state_file} >> {audit_log}; fi\n"
            f"exec {shutil.which('rm')} \"$@\"\n",
            encoding="utf-8",
        )
        (fake_bin / "rm").chmod(0o755)
        no_policy_env = self._write_env_file({"CI_FLEET_INSTANCE": "example-ci-01"})

        removed = subprocess.run(
            [str(SCRIPTS / "apply-docker-network-policy.sh"), "--checkpoint", str(checkpoint), "--env", str(no_policy_env)],
            capture_output=True,
            text=True,
            env=self._env(
                PATH=f"{fake_bin}:{os.environ['PATH']}",
                PYTHONPATH=str(audit_dir),
                FSYNC_AUDIT_LOG=str(audit_log),
                STATE_FILE=str(state_file),
            ),
            timeout=30,
        )

        self.assertEqual(removed.returncode, 0, removed.stderr)
        events = audit_log.read_text(encoding="utf-8").splitlines()
        unlink = events.index(f"U {state_file}")
        self.assertIn(f"F {checkpoint}", events[unlink + 1 :])

    def test_removal_retains_marker_if_managed_key_reappears_during_verification(self) -> None:
        daemon = self._write_daemon("{}\n")
        checkpoint = Path(self.tmp) / "checkpoint-removal-key-conflict"
        self._write_success_commands()
        policy_env = self._write_env_file(self._rendered_with_policy())
        applied = self._run(str(policy_env), checkpoint_dir=str(checkpoint))
        self.assertEqual(applied.returncode, 0, applied.stderr)
        managed_pools = json.loads(daemon.read_text(encoding="utf-8"))["default-address-pools"]
        self.installed_env.write_bytes(policy_env.read_bytes())
        state_file = checkpoint / "docker-network-policy.json"
        mutator = Path(self.tmp) / "reintroduce-pools.py"
        mutator.write_text(
            "import json, sys\n"
            "path = sys.argv[1]\n"
            "value = json.load(open(path))\n"
            "value['default-address-pools'] = [{'base': '192.0.2.0/24', 'size': 29}]\n"
            "with open(path, 'w') as handle: json.dump(value, handle)\n",
            encoding="utf-8",
        )
        health = Path(self.tmp) / "health.sh"
        health.write_text(
            "#!/usr/bin/env bash\n"
            'grep -q "^CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_COUNT=" "$2" || '
            f"{shutil.which('python3')} {mutator} {daemon}\n",
            encoding="utf-8",
        )
        health.chmod(0o755)
        no_policy_env = self._write_env_file({"CI_FLEET_INSTANCE": "example-ci-01"})

        removed = self._run(str(no_policy_env), checkpoint_dir=str(checkpoint), expected_rc=1)

        self.assertNotEqual(removed.returncode, 0)
        self.assertTrue(state_file.exists())
        self.assertEqual(json.loads(daemon.read_text(encoding="utf-8"))["default-address-pools"], managed_pools)

    def test_removal_restores_marker_if_managed_key_reappears_during_marker_clear(self) -> None:
        daemon = self._write_daemon("{}\n")
        checkpoint = Path(self.tmp) / "checkpoint-removal-marker-clear-conflict"
        self._write_success_commands()
        policy_env = self._write_env_file(self._rendered_with_policy())
        applied = self._run(str(policy_env), checkpoint_dir=str(checkpoint))
        self.assertEqual(applied.returncode, 0, applied.stderr)
        managed_pools = json.loads(daemon.read_text(encoding="utf-8"))["default-address-pools"]
        self.installed_env.write_bytes(policy_env.read_bytes())
        state_file = checkpoint / "docker-network-policy.json"
        audit_dir = Path(self.tmp) / "marker-clear-conflict-audit"
        audit_dir.mkdir()
        (audit_dir / "sitecustomize.py").write_text(
            "import json, os\n"
            "_unlink = os.unlink\n"
            "def unlink(path, *args, **kwargs):\n"
            "    result = _unlink(path, *args, **kwargs)\n"
            "    if os.path.realpath(path) == os.environ['STATE_FILE']:\n"
            "        daemon = os.environ['DAEMON']\n"
            "        value = json.load(open(daemon, encoding='utf-8'))\n"
            "        value['default-address-pools'] = [{'base': '192.0.2.0/24', 'size': 29}]\n"
            "        with open(daemon, 'w', encoding='utf-8') as handle: json.dump(value, handle)\n"
            "    return result\n"
            "os.unlink = unlink\n",
            encoding="utf-8",
        )
        no_policy_env = self._write_env_file({"CI_FLEET_INSTANCE": "example-ci-01"})

        removed = subprocess.run(
            [str(SCRIPTS / "apply-docker-network-policy.sh"), "--checkpoint", str(checkpoint), "--env", str(no_policy_env)],
            capture_output=True,
            text=True,
            env=self._env(PYTHONPATH=str(audit_dir), STATE_FILE=str(state_file), DAEMON=str(daemon)),
            timeout=30,
        )

        self.assertNotEqual(removed.returncode, 0)
        self.assertTrue(state_file.exists())
        self.assertEqual(json.loads(daemon.read_text(encoding="utf-8"))["default-address-pools"], managed_pools)

    def test_managed_policy_removal_restores_original_state_and_clears_only_marker(self) -> None:
        for prior_present in (False, True):
            with self.subTest(prior_present=prior_present):
                daemon = self.daemon_dir / "daemon.json"
                daemon.unlink(missing_ok=True)
                prior = b'{"icc":false}\n'
                if prior_present:
                    daemon.write_bytes(prior)
                    daemon.chmod(0o600)
                checkpoint = Path(self.tmp) / f"checkpoint-remove-{prior_present}"
                self._write_success_commands()
                policy_env = self._write_env_file(self._rendered_with_policy())
                applied = self._run(str(policy_env), checkpoint_dir=str(checkpoint))
                self.assertEqual(applied.returncode, 0, applied.stderr)
                self.assertTrue((checkpoint / "docker-network-policy.json").exists())
                retained = checkpoint / "retained"
                retained.write_text("keep\n", encoding="utf-8")

                command_log = Path(self.tmp) / f"remove-{prior_present}.log"
                for name in ("drain.sh", "restart.sh", "probe.sh"):
                    command = Path(self.tmp) / name
                    command.write_text(f"#!/usr/bin/env bash\necho {name} >> {command_log}\n", encoding="utf-8")
                    command.chmod(0o755)
                no_policy_env = self._write_env_file({"CI_FLEET_INSTANCE": "example-ci-01"})
                health = Path(self.tmp) / "health.sh"
                health.write_text(
                    "#!/usr/bin/env bash\n"
                    f"[[ $1 == --env && $2 != {no_policy_env} ]] || exit 2\n"
                    'grep -Fqx "CI_FLEET_INSTANCE=example-ci-01" "$2" || exit 2\n'
                    f"echo health.sh >> {command_log}\n",
                    encoding="utf-8",
                )
                health.chmod(0o755)

                removed = self._run(str(no_policy_env), checkpoint_dir=str(checkpoint))

                self.assertEqual(removed.returncode, 0, removed.stderr)
                self.assertEqual(removed.stdout, "NETWORK_POLICY_REMOVED\n")
                self.assertEqual(command_log.read_text(encoding="utf-8").splitlines(), ["drain.sh", "restart.sh", "probe.sh", "health.sh"])
                self.assertEqual(daemon.exists(), prior_present)
                if prior_present:
                    self.assertEqual(json.loads(daemon.read_text(encoding="utf-8")), json.loads(prior))
                    self.assertEqual(daemon.stat().st_mode & 0o777, 0o600)
                    self.assertFalse((checkpoint / "daemon.json").exists())
                self.assertFalse((checkpoint / "docker-network-policy.json").exists())
                self.assertTrue(retained.exists())

    def test_removal_rollback_preserves_concurrent_content_and_metadata(self) -> None:
        daemon = self._write_daemon(json.dumps({"icc": False}))
        checkpoint = Path(self.tmp) / "checkpoint-remove-merge-rollback"
        self._write_success_commands()
        policy_env = self._write_env_file(self._rendered_with_policy())
        applied = self._run(str(policy_env), checkpoint_dir=str(checkpoint))
        self.assertEqual(applied.returncode, 0, applied.stderr)
        managed_pools = json.loads(daemon.read_text(encoding="utf-8"))["default-address-pools"]
        self.installed_env.write_bytes(policy_env.read_bytes())
        current = json.loads(daemon.read_text(encoding="utf-8"))
        current.pop("icc")
        current["live-restore"] = True
        daemon.write_text(json.dumps(current), encoding="utf-8")
        no_policy_env = self._write_env_file({"CI_FLEET_INSTANCE": "example-ci-01"})
        command_log = Path(self.tmp) / "removal-merge-rollback.log"
        self.drain_command.write_text(f"#!/usr/bin/env bash\necho drain >> {command_log}\n", encoding="utf-8")
        restart = Path(self.tmp) / "restart.sh"
        restart.write_text(f"#!/usr/bin/env bash\necho restart >> {command_log}\n", encoding="utf-8")
        restart.chmod(0o755)
        mutator = Path(self.tmp) / "mutate-daemon.py"
        mutator.write_text(
            "import json, sys\n"
            "p = sys.argv[1]\n"
            "d = json.load(open(p))\n"
            "d['debug'] = True\n"
            "with open(p, 'w') as handle: json.dump(d, handle)\n",
            encoding="utf-8",
        )
        resume = Path(self.tmp) / "resume.sh"
        resume.write_text(
            "#!/usr/bin/env bash\n"
            f"echo resume >> {command_log}\n"
            'if ! grep -q "^CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_COUNT=" "$2"; then\n'
            f"  {shutil.which('python3')} {mutator} {daemon}\n"
            f"  chmod 600 {daemon}\n"
            "  exit 2\n"
            "fi\n",
            encoding="utf-8",
        )
        resume.chmod(0o755)
        health = Path(self.tmp) / "health.sh"
        health.write_text(f"#!/usr/bin/env bash\necho health >> {command_log}\n", encoding="utf-8")
        health.chmod(0o755)

        removed = self._run(str(no_policy_env), checkpoint_dir=str(checkpoint), expected_rc=1)

        self.assertNotEqual(removed.returncode, 0)
        self.assertEqual(
            json.loads(daemon.read_text(encoding="utf-8")),
            {"default-address-pools": managed_pools, "debug": True, "live-restore": True},
        )
        self.assertEqual(daemon.stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            command_log.read_text(encoding="utf-8").splitlines(),
            ["drain", "restart", "resume", "drain", "restart", "resume", "health"],
        )

    def test_removal_failure_restores_managed_config_and_retains_recovery_state(self) -> None:
        prior = b'{"registry-mirrors":["https://mirror.example.invalid?token=credential"]}\n'
        daemon = self.daemon_dir / "daemon.json"
        daemon.write_bytes(prior)
        daemon.chmod(0o600)
        checkpoint = Path(self.tmp) / "checkpoint-remove-failure"
        self._write_success_commands()
        policy_env = self._write_env_file(self._rendered_with_policy())
        applied = self._run(str(policy_env), checkpoint_dir=str(checkpoint))
        self.assertEqual(applied.returncode, 0, applied.stderr)
        managed = daemon.read_bytes()
        managed_mode = daemon.stat().st_mode & 0o777
        state = (checkpoint / "docker-network-policy.json").read_bytes()
        self.assertFalse((checkpoint / "daemon.json").exists())
        self.installed_env.write_bytes(policy_env.read_bytes())

        restart_log = Path(self.tmp) / "remove-failure-restart.log"
        restart = Path(self.tmp) / "restart.sh"
        restart.write_text(f"#!/usr/bin/env bash\necho restart >> {restart_log}\n", encoding="utf-8")
        restart.chmod(0o755)
        health = Path(self.tmp) / "health.sh"
        health.write_text(
            "#!/usr/bin/env bash\n"
            'grep -q "^CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_COUNT=" "$2" && exit 0\n'
            "echo '198.51.100.0/24 https://secret.example.invalid token=credential'\n"
            "echo removal-health-secret >&2\n"
            "exit 2\n",
            encoding="utf-8",
        )
        health.chmod(0o755)
        no_policy_env = self._write_env_file({"CI_FLEET_INSTANCE": "example-ci-01"})

        removed = self._run(str(no_policy_env), checkpoint_dir=str(checkpoint), expected_rc=1)

        self.assertNotEqual(removed.returncode, 0)
        self.assertEqual(daemon.read_bytes(), managed)
        self.assertEqual(daemon.stat().st_mode & 0o777, managed_mode)
        self.assertEqual(restart_log.read_text(encoding="utf-8").splitlines(), ["restart", "restart"])
        self.assertEqual((checkpoint / "docker-network-policy.json").read_bytes(), state)
        self.assertFalse((checkpoint / "daemon.json").exists())
        combined = removed.stdout + removed.stderr
        self.assertIn("managed daemon.json restored", combined)
        for secret in ("198.51.100.0/24", "secret.example.invalid", "mirror.example.invalid", "credential", "removal-health-secret"):
            self.assertNotIn(secret, combined)

    def test_removal_rollback_resumes_and_checks_health_with_managed_env(self) -> None:
        daemon = self._write_daemon("{}\n")
        checkpoint = Path(self.tmp) / "checkpoint-removal-rollback-env"
        self._write_success_commands()
        policy_env = self._write_env_file(self._rendered_with_policy())
        applied = self._run(str(policy_env), checkpoint_dir=str(checkpoint))
        self.assertEqual(applied.returncode, 0, applied.stderr)
        managed = daemon.read_bytes()
        self.installed_env.write_bytes(policy_env.read_bytes())
        no_policy_env = self._write_env_file({"CI_FLEET_INSTANCE": "example-ci-01"})
        command_log = Path(self.tmp) / "removal-rollback-env.log"
        self.drain_command.write_text(f"#!/usr/bin/env bash\necho drain >> {command_log}\n", encoding="utf-8")
        restart = Path(self.tmp) / "restart.sh"
        restart.write_text(f"#!/usr/bin/env bash\necho restart >> {command_log}\n", encoding="utf-8")
        restart.chmod(0o755)
        for name in ("resume", "health"):
            command = Path(self.tmp) / f"{name}.sh"
            command.write_text(
                "#!/usr/bin/env bash\n"
                'grep -q "^CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_COUNT=" "$2" && generation=managed || generation=candidate\n'
                f"echo {name}-$generation >> {command_log}\n"
                f"[[ {name} != health || $generation == managed ]] || exit 2\n",
                encoding="utf-8",
            )
            command.chmod(0o755)

        removed = self._run(str(no_policy_env), checkpoint_dir=str(checkpoint), expected_rc=1)

        self.assertNotEqual(removed.returncode, 0)
        self.assertIn("managed daemon.json restored", removed.stderr)
        self.assertEqual(daemon.read_bytes(), managed)
        self.assertEqual(
            command_log.read_text(encoding="utf-8").splitlines(),
            ["drain", "restart", "resume-candidate", "health-candidate", "drain", "restart", "resume-managed", "health-managed"],
        )

    def test_marker_clear_failure_rolls_back_managed_config_and_retains_state(self) -> None:
        daemon = self._write_daemon('{"icc":false}\n')
        checkpoint = Path(self.tmp) / "checkpoint-marker-failure"
        self._write_success_commands()
        policy_env = self._write_env_file(self._rendered_with_policy())
        applied = self._run(str(policy_env), checkpoint_dir=str(checkpoint))
        self.assertEqual(applied.returncode, 0, applied.stderr)
        managed = daemon.read_bytes()
        state_file = checkpoint / "docker-network-policy.json"
        state = state_file.read_bytes()

        restart_log = Path(self.tmp) / "marker-failure-restart.log"
        restart = Path(self.tmp) / "restart.sh"
        restart.write_text(f"#!/usr/bin/env bash\necho restart >> {restart_log}\n", encoding="utf-8")
        restart.chmod(0o755)
        health = Path(self.tmp) / "health.sh"
        health.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        health.chmod(0o755)
        no_policy_env = self._write_env_file({"CI_FLEET_INSTANCE": "example-ci-01"})
        audit_dir = Path(self.tmp) / "marker-failure-audit"
        audit_dir.mkdir()
        (audit_dir / "sitecustomize.py").write_text(
            "import os\n"
            "_unlink = os.unlink\n"
            "def unlink(path, *args, **kwargs):\n"
            "    if os.path.realpath(path) == os.environ['STATE_FILE']: raise OSError\n"
            "    return _unlink(path, *args, **kwargs)\n"
            "os.unlink = unlink\n",
            encoding="utf-8",
        )
        env = self._env(PYTHONPATH=str(audit_dir), STATE_FILE=str(state_file))

        removed = subprocess.run(
            [
                str(SCRIPTS / "apply-docker-network-policy.sh"),
                "--checkpoint",
                str(checkpoint),
                "--env",
                str(no_policy_env),
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

        self.assertNotEqual(removed.returncode, 0)
        self.assertEqual(daemon.read_bytes(), managed)
        self.assertEqual(state_file.read_bytes(), state)
        self.assertEqual(restart_log.read_text(encoding="utf-8").splitlines(), ["restart", "restart"])

    def test_marker_directory_fsync_failure_restores_state_before_rollback(self) -> None:
        daemon = self._write_daemon('{"icc":false}\n')
        checkpoint = Path(self.tmp) / "checkpoint-marker-fsync-failure"
        self._write_success_commands()
        policy_env = self._write_env_file(self._rendered_with_policy())
        applied = self._run(str(policy_env), checkpoint_dir=str(checkpoint))
        self.assertEqual(applied.returncode, 0, applied.stderr)
        managed = daemon.read_bytes()
        state_file = checkpoint / "docker-network-policy.json"
        state = state_file.read_bytes()
        no_policy_env = self._write_env_file({"CI_FLEET_INSTANCE": "example-ci-01"})
        audit_dir = Path(self.tmp) / "marker-fsync-failure-audit"
        audit_dir.mkdir()
        (audit_dir / "sitecustomize.py").write_text(
            "import os\n"
            "_fsync = os.fsync\n"
            "_failed = False\n"
            "def fsync(fd):\n"
            "    global _failed\n"
            "    path = os.path.realpath(f'/proc/self/fd/{fd}')\n"
            "    if not _failed and path == os.environ['CHECKPOINT'] and not os.path.exists(os.environ['STATE_FILE']):\n"
            "        _failed = True\n"
            "        raise OSError('injected checkpoint fsync failure')\n"
            "    return _fsync(fd)\n"
            "os.fsync = fsync\n",
            encoding="utf-8",
        )

        removed = subprocess.run(
            [str(SCRIPTS / "apply-docker-network-policy.sh"), "--checkpoint", str(checkpoint), "--env", str(no_policy_env)],
            capture_output=True,
            text=True,
            env=self._env(PYTHONPATH=str(audit_dir), CHECKPOINT=str(checkpoint), STATE_FILE=str(state_file)),
            timeout=30,
        )

        self.assertNotEqual(removed.returncode, 0)
        self.assertEqual(daemon.read_bytes(), managed)
        self.assertEqual(state_file.read_bytes(), state)

    def test_unmanaged_no_policy_is_noop_without_mutation_or_commands(self) -> None:
        prior = b'{"bip":"172.17.0.1/16"}\n'
        daemon = self.daemon_dir / "daemon.json"
        daemon.write_bytes(prior)
        markers = []
        for name in ("drain.sh", "restart.sh", "probe.sh", "resume.sh", "health.sh"):
            marker = Path(self.tmp) / f"{name}.marker"
            markers.append(marker)
            command = Path(self.tmp) / name
            command.write_text(f"#!/usr/bin/env bash\ntouch {marker}\nexit 1\n", encoding="utf-8")
            command.chmod(0o755)
        checkpoint = Path(self.tmp) / "checkpoint"
        env_file = self._write_env_file({"CI_FLEET_INSTANCE": "example-ci-01"})

        result = self._run(str(env_file), checkpoint_dir=str(checkpoint))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "NETWORK_POLICY_NOOP\n")
        self.assertEqual(daemon.read_bytes(), prior)
        self.assertFalse(checkpoint.exists())
        self.assertTrue(all(not marker.exists() for marker in markers))

    def test_removal_rejects_broken_checkpoint_state_symlink(self) -> None:
        daemon = self._write_daemon('{"default-address-pools":[]}\n')
        prior = daemon.read_bytes()
        self._write_success_commands()
        checkpoint = Path(self.tmp) / "checkpoint-broken-state-link"
        checkpoint.mkdir(mode=0o700)
        (checkpoint / "docker-network-policy.json").symlink_to(checkpoint / "missing-state")
        env_file = self._write_env_file({"CI_FLEET_INSTANCE": "example-ci-01"})

        result = self._run(str(env_file), checkpoint_dir=str(checkpoint))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("checkpoint state is invalid", result.stderr)
        self.assertEqual(daemon.read_bytes(), prior)

    def test_first_apply_persists_prior_environment_before_drain(self) -> None:
        self._write_daemon("{}\n")
        self._write_success_commands()
        checkpoint = Path(self.tmp) / "checkpoint-first-apply-environment"
        self.drain_command.write_text(
            "#!/usr/bin/env bash\n"
            f"recovery=({checkpoint}/recovery.*)\n"
            '[[ ${#recovery[@]} == 1 ]]\n'
            f"cmp -s {self.installed_env} \"${{recovery[0]}}/prior-ci-fleet.env\"\n",
            encoding="utf-8",
        )
        env_file = self._write_env_file(self._rendered_with_policy())

        result = self._run(str(env_file), checkpoint_dir=str(checkpoint))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(list(checkpoint.glob("recovery.*")))

    def test_daemon_config_cannot_alias_checkpoint_state(self) -> None:
        self._write_success_commands()
        checkpoint = Path(self.tmp) / "checkpoint-daemon-state-alias"
        checkpoint.mkdir(mode=0o700)
        env_file = self._write_env_file(self._rendered_with_policy())
        daemon = checkpoint / "docker-network-policy.json"

        result = subprocess.run(
            [str(SCRIPTS / "apply-docker-network-policy.sh"), "--checkpoint", str(checkpoint), "--env", str(env_file)],
            capture_output=True,
            text=True,
            env=self._env(CI_FLEET_DOCKER_DAEMON_CONFIG=str(daemon)),
            timeout=30,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("daemon config must be outside checkpoint directory", result.stderr)
        self.assertFalse(daemon.exists())

    def test_no_change_clears_stale_committed_recovery(self) -> None:
        self._write_daemon("{}\n")
        self._write_success_commands()
        checkpoint = Path(self.tmp) / "checkpoint-stale-recovery"
        env_file = self._write_env_file(self._rendered_with_policy())
        applied = self._run(str(env_file), checkpoint_dir=str(checkpoint))
        self.assertEqual(applied.returncode, 0, applied.stderr)
        recovery = checkpoint / "recovery.stale"
        recovery.mkdir(mode=0o700)
        (recovery / "prior-ci-fleet.env").write_bytes(self.installed_env.read_bytes())
        (recovery / "prior-ci-fleet.env").chmod(0o600)

        result = self._run(str(env_file), checkpoint_dir=str(checkpoint))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "NETWORK_POLICY_NO_CHANGE\n")
        self.assertFalse(recovery.exists())

    def test_non_standard_daemon_json_is_rejected_before_drain(self) -> None:
        daemon = self._write_daemon('{"log-level":NaN}\n')
        prior = daemon.read_bytes()
        self._write_success_commands()
        drain_marker = Path(self.tmp) / "non-standard-json-drain.marker"
        self.drain_command.write_text(f"#!/usr/bin/env bash\ntouch {drain_marker}\n", encoding="utf-8")
        env_file = self._write_env_file(self._rendered_with_policy())

        result = self._run(str(env_file), checkpoint_dir=str(Path(self.tmp) / "checkpoint-non-standard-json"))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("failed to stage merged daemon.json", result.stderr)
        self.assertFalse(drain_marker.exists())
        self.assertEqual(daemon.read_bytes(), prior)

    def test_failed_resume_redrains_before_rollback_restart(self) -> None:
        self._write_daemon("{}\n")
        env_file = self._write_env_file(self._rendered_with_policy())
        command_log = Path(self.tmp) / "failed-resume-rollback.log"
        self.drain_command.write_text(f"#!/usr/bin/env bash\necho drain >> {command_log}\n", encoding="utf-8")
        for name in ("restart", "probe", "health"):
            command = Path(self.tmp) / f"{name}.sh"
            command.write_text(f"#!/usr/bin/env bash\necho {name} >> {command_log}\n", encoding="utf-8")
            command.chmod(0o755)
        resume = Path(self.tmp) / "resume.sh"
        resume.write_text(f"#!/usr/bin/env bash\necho resume >> {command_log}\nexit 1\n", encoding="utf-8")
        resume.chmod(0o755)

        result = self._run(str(env_file), expected_rc=1)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            command_log.read_text(encoding="utf-8").splitlines(),
            ["drain", "restart", "probe", "resume", "drain", "restart", "resume"],
        )

    def test_rollback_merge_rejects_concurrent_content_change(self) -> None:
        daemon = self._write_daemon('{"icc":false}\n')
        self._write_success_commands()
        trigger = Path(self.tmp) / "mutate-during-rollback"
        health = Path(self.tmp) / "health.sh"
        health.write_text(f"#!/usr/bin/env bash\ntouch {trigger}\nexit 2\n", encoding="utf-8")
        health.chmod(0o755)
        fake_bin = Path(self.tmp) / "fake-bin"
        fake_bin.mkdir()
        stat_command = fake_bin / "stat"
        stat_command.write_text(
            "#!/usr/bin/env bash\n"
            f"{shutil.which('stat')} \"$@\"\n"
            "status=$?\n"
            f"if [[ -e {trigger} && $1 == -c && $2 == %g && $3 == {daemon} ]]; then\n"
            f"  {shutil.which('python3')} -c \"import json; p='{daemon}'; v=json.load(open(p)); v['concurrent']=True; json.dump(v,open(p,'w'))\"\n"
            f"  rm -f {trigger}\n"
            "fi\n"
            "exit $status\n",
            encoding="utf-8",
        )
        stat_command.chmod(0o755)
        checkpoint = Path(self.tmp) / "checkpoint-rollback-content-conflict"
        env_file = self._write_env_file(self._rendered_with_policy())
        env = self._env(PATH=f"{fake_bin}:{os.environ['PATH']}")

        result = subprocess.run(
            [str(SCRIPTS / "apply-docker-network-policy.sh"), "--checkpoint", str(checkpoint), "--env", str(env_file)],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rollback verification failed", result.stderr)
        self.assertTrue(json.loads(daemon.read_text(encoding="utf-8"))["concurrent"])


if __name__ == "__main__":
    unittest.main()
