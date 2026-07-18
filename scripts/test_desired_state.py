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


class DesiredStateTests(unittest.TestCase):
    def render(self, value: dict | None = None):
        return build_rendered_env(
            value or config(),
            "example-ci-01",
            host_values(),
            config_repository="example-org/example-fleet-config",
            config_ref=CONFIG_COMMIT,
            docker_gid=998,
        )

    def test_active_controller_renders_configured_capacity(self) -> None:
        environment, metadata = self.render()
        self.assertEqual(environment["CI_FLEET_MAX_RUNNERS"], "1")
        self.assertEqual(environment["CI_FLEET_CONFIGURED_MAX_RUNNERS"], "1")
        self.assertEqual(environment["CI_FLEET_LABELS"], "docker-ci")
        self.assertEqual(metadata["controller_state"], "active")

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
