#!/usr/bin/env python3
"""Deterministic tests for schema-v3 controller selection and rendering."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from desired_state import (
    DesiredStateError,
    build_rendered_env,
    load_engine_capabilities,
    load_and_validate_config,
    parse_env,
    validate_host_values,
)


ROOT = Path(__file__).resolve().parents[1]
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
        "reserve_subnets": 1,
        "default_address_pools": [
            {"base": "198.51.100.0/24", "size": 28},
        ],
    }


class DesiredStateTests(unittest.TestCase):
    def render(self, value: dict | None = None, capabilities: set[str] | None = None):
        return build_rendered_env(
            value or config(),
            "example-ci-01",
            host_values(),
            config_repository="example-org/example-fleet-config",
            config_ref=CONFIG_COMMIT,
            docker_gid=998,
            engine_capabilities={"status_reporting_config"} if capabilities is None else capabilities,
        )

    def test_active_controller_renders_configured_capacity(self) -> None:
        environment, metadata = self.render()
        self.assertEqual(environment["CI_FLEET_MAX_RUNNERS"], "1")
        self.assertEqual(environment["CI_FLEET_CONFIGURED_MAX_RUNNERS"], "1")
        self.assertEqual(environment["CI_FLEET_LABELS"], "docker-ci")
        self.assertEqual(environment["CI_FLEET_COMMIT"], environment["CI_FLEET_ENGINE_REF"])
        self.assertEqual(metadata["controller_state"], "active")

    def test_status_reporting_requires_fixed_host_local_configuration(self) -> None:
        value = config()
        value["controllers"]["example-ci-01"]["status_reporting"] = {
            "enabled": True,
            "config_file": "/etc/ci-fleet/monitoring.env",
        }
        environment, _ = self.render(value, {"status_reporting_config", "required_status_reporting"})
        self.assertEqual(environment["CI_FLEET_STATUS_REPORTING_REQUIRED"], "1")
        value["controllers"]["example-ci-01"]["status_reporting"]["config_file"] = "https://example.invalid/v1/status"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fleet.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(DesiredStateError, "fixed host-local monitoring"):
                load_and_validate_config(path)
        value["controllers"]["example-ci-01"]["status_reporting"] = None
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fleet.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(DesiredStateError, "must be an object"):
                load_and_validate_config(path)

    def test_status_reporting_requires_engine_capability(self) -> None:
        value = config()
        value["controllers"]["example-ci-01"]["status_reporting"] = {
            "enabled": True,
            "config_file": "/etc/ci-fleet/monitoring.env",
        }
        with self.assertRaisesRegex(DesiredStateError, "does not advertise"):
            self.render(value, set())
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "engine-capabilities.json"
            manifest.write_text("not json", encoding="utf-8")
            with self.assertRaisesRegex(DesiredStateError, "malformed"):
                load_engine_capabilities(manifest)
            manifest.write_text('{"schema_version":1,"schema_version":1,"capabilities":{}}', encoding="utf-8")
            with self.assertRaisesRegex(DesiredStateError, "malformed"):
                load_engine_capabilities(manifest)
            manifest.unlink()
            with self.assertRaisesRegex(DesiredStateError, "missing"):
                load_engine_capabilities(manifest)

    def test_omitted_status_reporting_accepts_older_engine(self) -> None:
        value = config()
        value["controllers"]["example-ci-01"].pop("status_reporting", None)
        environment, metadata = self.render(value, set())
        self.assertNotIn("CI_FLEET_STATUS_REPORTING_REQUIRED", environment)
        self.assertFalse(metadata["status_reporting_configured"])
        self.assertFalse(metadata["status_reporting_required"])

    def test_disabled_status_reporting_requires_schema_capability(self) -> None:
        value = config()
        value["controllers"]["example-ci-01"]["status_reporting"] = {
            "enabled": False,
            "config_file": "/etc/ci-fleet/monitoring.env",
        }
        with self.assertRaisesRegex(DesiredStateError, "does not support status reporting configuration"):
            self.render(value, set())
        environment, metadata = self.render(value, {"status_reporting_config"})
        self.assertNotIn("CI_FLEET_STATUS_REPORTING_REQUIRED", environment)
        self.assertTrue(metadata["status_reporting_configured"])
        self.assertFalse(metadata["status_reporting_required"])

    def test_docker_network_policy_renders_read_only_inspection_values(self) -> None:
        value = config()
        value["controllers"]["example-ci-01"]["docker_network_policy"] = docker_network_policy()
        environment, metadata = self.render(value)
        self.assertEqual(environment["CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_COUNT"], "1")
        self.assertEqual(environment["CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_0_BASE"], "198.51.100.0/24")
        self.assertEqual(environment["CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_0_SIZE"], "28")
        self.assertEqual(environment["CI_FLEET_DOCKER_NETWORK_RESERVE_SUBNETS"], "1")
        self.assertTrue(metadata["docker_network_policy_configured"])
        self.assertEqual(metadata["docker_network_default_address_pools"], 1)
        self.assertEqual(metadata["docker_network_reserve_subnets"], 1)

    def test_docker_network_policy_requires_capacity_for_reserve(self) -> None:
        value = config()
        value["controllers"]["example-ci-01"]["max_runners"] = 2
        value["controllers"]["example-ci-01"]["docker_network_policy"] = {
            "reserve_subnets": 1,
            "default_address_pools": [{"base": "198.51.100.0/30", "size": 30}],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fleet.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(DesiredStateError, "capacity"):
                load_and_validate_config(path)

    def test_drained_controller_renders_zero_effective_capacity(self) -> None:
        value = config()
        value["controllers"]["example-ci-01"]["state"] = "drained"
        environment, metadata = self.render(value)
        self.assertEqual(environment["CI_FLEET_MAX_RUNNERS"], "0")
        self.assertEqual(environment["CI_FLEET_CONFIGURED_MAX_RUNNERS"], "1")
        self.assertEqual(metadata["effective_max_runners"], 0)

    def test_disabled_controller_renders_zero_effective_capacity(self) -> None:
        value = config()
        value["controllers"]["example-ci-01"]["state"] = "disabled"
        environment, _ = self.render(value)
        self.assertEqual(environment["CI_FLEET_MAX_RUNNERS"], "0")

    def test_missing_controller_fails_closed(self) -> None:
        with self.assertRaisesRegex(DesiredStateError, "is not declared"):
            build_rendered_env(
                config(),
                "missing-ci-01",
                host_values(),
                config_repository="example-org/example-fleet-config",
                config_ref=CONFIG_COMMIT,
                docker_gid=998,
            )

    def test_capacity_overcommit_is_rejected_by_public_contract(self) -> None:
        value = config()
        value["controllers"]["example-ci-01"]["max_runners"] = 2
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fleet.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(DesiredStateError, "capacity_budget"):
                load_and_validate_config(path)

    def test_project_max_parallel_is_rejected(self) -> None:
        value = config()
        value["projects"]["example-app"]["ci_contract"]["max_parallel"] = 1
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fleet.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(DesiredStateError, "max_parallel"):
                load_and_validate_config(path)

    def test_unknown_host_local_variable_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "host.env"
            path.write_text(
                "CI_FLEET_GITHUB_APP_CLIENT_ID=Iv1.EXAMPLE\n"
                "CI_FLEET_GITHUB_APP_INSTALLATION_ID=123\n"
                "CI_FLEET_GITHUB_APP_PRIVATE_KEY_FILE=/safe/key.pem\n"
                "PROJECT_SECRET=forbidden\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(DesiredStateError, "unsupported host-local variable"):
                parse_env(path, allow_unknown=False)

    def test_duplicate_host_local_variable_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "host.env"
            path.write_text(
                "CI_FLEET_GITHUB_APP_CLIENT_ID=first\n"
                "CI_FLEET_GITHUB_APP_CLIENT_ID=second\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(DesiredStateError, "duplicate variable"):
                parse_env(path, allow_unknown=False)

    def test_host_values_require_absolute_key_path(self) -> None:
        values = copy.deepcopy(host_values())
        values["CI_FLEET_GITHUB_APP_PRIVATE_KEY_FILE"] = "relative.pem"
        with self.assertRaisesRegex(DesiredStateError, "absolute shell-safe"):
            validate_host_values(values)

    def test_host_values_reject_sub_hour_ttl(self) -> None:
        values = copy.deepcopy(host_values())
        values["CI_FLEET_RUNNER_TTL"] = "30m"
        with self.assertRaisesRegex(DesiredStateError, "at least one hour"):
            validate_host_values(values)

    def test_shell_metacharacter_in_config_identity_is_rejected(self) -> None:
        with self.assertRaisesRegex(DesiredStateError, "must be shell-safe"):
            build_rendered_env(
                config(),
                "example-ci-01",
                host_values(),
                config_repository="/tmp/config$(id)",
                config_ref=CONFIG_COMMIT,
                docker_gid=998,
            )


if __name__ == "__main__":
    unittest.main()
