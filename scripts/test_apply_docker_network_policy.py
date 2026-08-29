#!/usr/bin/env python3
"""Tests for the Docker daemon network policy apply stage.

Validates the daemon.json rendering helper and the apply script's
transactional behavior: validation before mutation, preservation of
unrelated daemon JSON keys, rollback on failure, and failure evidence
without secrets.
"""
from __future__ import annotations

import json
import os
import shutil
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

    def test_not_configured_returns_empty(self) -> None:
        # No network policy rendered -> no pools key.
        daemon = render_docker_daemon_config({})
        self.assertNotIn("default-address-pools", daemon)


class ApplyScriptTests(unittest.TestCase):
    """Integration tests for scripts/apply-docker-network-policy.sh.

    Uses real temp files and injected commands (no Docker daemon required).
    """

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.daemon_dir = Path(self.tmp) / "etc" / "docker"
        self.daemon_dir.mkdir(parents=True)

    def _write_daemon(self, content: str) -> Path:
        path = self.daemon_dir / "daemon.json"
        path.write_text(content, encoding="utf-8")
        return path

    def _env(self, **extra: str) -> dict[str, str]:
        env = dict(os.environ)
        env["CI_FLEET_TESTING"] = "1"
        env["CI_FLEET_DOCKER_DAEMON_CONFIG"] = str(self.daemon_dir / "daemon.json")
        env["CI_FLEET_DOCKER_RESTART_COMMAND"] = str(Path(self.tmp) / "restart.sh")
        env["CI_FLEET_DOCKER_NETWORK_PROBE"] = str(Path(self.tmp) / "probe.sh")
        env["CI_FLEET_HEALTH_CHECK_COMMAND"] = str(Path(self.tmp) / "health.sh")
        env["CI_FLEET_ROOT_PREFIX"] = self.tmp
        for key, value in extra.items():
            env[key] = value
        return env

    def _write_env_file(self, rendered: dict[str, str]) -> Path:
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

    def _run(self, env_file: str, checkpoint_dir: str = "", expected_rc: int = 0) -> subprocess.CompletedProcess:
        script = str(SCRIPTS / "apply-docker-network-policy.sh")
        args = [script]
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
            [str(SCRIPTS / "apply-docker-network-policy.sh"), "--env", str(bad_env)],
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
        drain.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
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
            [str(SCRIPTS / "apply-docker-network-policy.sh"), "--env", str(env_file)],
            capture_output=True,
            text=True,
            env=self._env(CI_FLEET_DOCKER_DRAIN_COMMAND=str(drain)),
            timeout=30,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(daemon.read_bytes(), prior)
        self.assertFalse(restart_marker.exists())

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
        probe.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
        probe.chmod(0o755)
        health_log = Path(self.tmp) / "health.log"
        health = Path(self.tmp) / "health.sh"
        health.write_text(f"#!/usr/bin/env bash\necho health >> {health_log}\n", encoding="utf-8")
        health.chmod(0o755)
        env_file = self._write_env_file(self._rendered_with_policy())
        result = self._run(str(env_file), expected_rc=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(daemon.read_bytes(), prior)
        self.assertEqual(restart_log.read_text(encoding="utf-8").splitlines(), ["restart", "restart"])
        self.assertEqual(health_log.read_text(encoding="utf-8").splitlines(), ["health"])
        self.assertNotIn("198.51.100.0/24", result.stdout + result.stderr)
        self.assertNotIn("super-secret", result.stdout + result.stderr)

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
            [str(SCRIPTS / "apply-docker-network-policy.sh"), "--env", str(env_file)],
            capture_output=True,
            text=True,
            env=self._env(
                CI_FLEET_DOCKER_DRAIN_COMMAND=str(Path(self.tmp) / "drain.sh"),
                CI_FLEET_COMMAND_TIMEOUT_SECONDS="1",
            ),
            timeout=30,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertLess(time.monotonic() - started, 5)

    @unittest.skipUnless(Path("/dev/shm").is_dir(), "/dev/shm is unavailable")
    def test_apply_works_across_temp_filesystems(self) -> None:
        self._write_daemon(json.dumps({"bip": "172.17.0.1/16"}))
        for name in ("restart.sh", "probe.sh", "health.sh"):
            command = Path(self.tmp) / name
            command.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            command.chmod(0o755)
        env_file = self._write_env_file(self._rendered_with_policy())
        result = subprocess.run(
            [str(SCRIPTS / "apply-docker-network-policy.sh"), "--env", str(env_file)],
            capture_output=True,
            text=True,
            env=self._env(CI_FLEET_TEMP_DIR="/dev/shm"),
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        daemon = json.loads((self.daemon_dir / "daemon.json").read_text(encoding="utf-8"))
        self.assertEqual(daemon["bip"], "172.17.0.1/16")
        self.assertIn("default-address-pools", daemon)

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

    def test_failure_evidence_excludes_secrets(self) -> None:
        """GREEN: failure evidence must not contain secrets or CIDRs."""
        self._write_daemon(json.dumps({}))
        restart = Path(self.tmp) / "restart.sh"
        restart.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
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
            [script, "--env", env_file],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        self.assertNotEqual(result.returncode, 0)
        combined = result.stdout + result.stderr
        self.assertNotIn("198.51.100", combined)
        self.assertNotIn("203.0.113", combined)

    def test_checkpoint_preserves_prior_config(self) -> None:
        """GREEN: the checkpoint retains the prior daemon.json for restoration."""
        prior = {"bip": "172.17.0.1/16", "icc": False}
        self._write_daemon(json.dumps(prior))
        restart = Path(self.tmp) / "restart.sh"
        restart.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        restart.chmod(0o755)
        probe = Path(self.tmp) / "probe.sh"
        probe.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        probe.chmod(0o755)
        health = Path(self.tmp) / "health.sh"
        health.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        health.chmod(0o755)
        checkpoint_dir = Path(self.tmp) / "checkpoint"
        env_file = self._write_env_file(self._rendered_with_policy())
        result = self._run(str(env_file), checkpoint_dir=str(checkpoint_dir))
        self.assertEqual(result.returncode, 0, result.stderr)
        backup = checkpoint_dir / "daemon.json"
        self.assertTrue(backup.exists())
        self.assertEqual(json.loads(backup.read_text(encoding="utf-8")), prior)

    def test_no_policy_is_noop(self) -> None:
        """GREEN: when no network policy is rendered, the script does nothing."""
        self._write_daemon(json.dumps({"bip": "172.17.0.1/16"}))
        restart = Path(self.tmp) / "restart.sh"
        restart.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
        restart.chmod(0o755)
        env_file = self._write_env_file({"CI_FLEET_INSTANCE": "example-ci-01"})
        result = self._run(str(env_file))
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
