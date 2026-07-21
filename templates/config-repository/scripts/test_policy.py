#!/usr/bin/env python3
"""Regression tests for ci-fleet's non-negotiable configuration policies."""

from __future__ import annotations

import copy
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from validate import Validation, load_json, scan_secret_material, scan_tree_path_list, validate_config


ROOT = Path(__file__).resolve().parents[1]


def reference_config() -> dict:
    return json.loads((ROOT / "fleet.json").read_text(encoding="utf-8"))


def contract_schema() -> dict:
    return json.loads((ROOT / "fleet.schema.json").read_text(encoding="utf-8"))


def schema_accepts_engine_ref(value: str) -> bool:
    pattern = contract_schema()["$defs"]["controller"]["properties"]["engine_ref"]["pattern"]
    return re.fullmatch(pattern, value) is not None


def schema_accepts_delivery_engine(value: str) -> bool:
    contract = contract_schema()["properties"]["organization"]["properties"]["delivery_engine"]
    return contract.get("const") == value


def errors_for(config: dict, *, strict: bool = False) -> list[str]:
    validation = Validation()
    scan_secret_material(config, validation)
    validate_config(config, validation, strict)
    return validation.errors


def first_project(config: dict) -> dict:
    return next(iter(config["projects"].values()))


def first_controller(config: dict) -> dict:
    return next(iter(config["controllers"].values()))


class PolicyTests(unittest.TestCase):
    def assert_rejected(self, config: dict, expected: str, *, strict: bool = False) -> None:
        errors = errors_for(config, strict=strict)
        self.assertTrue(any(expected in error for error in errors), errors)

    def assert_engine_ref_contract(self, value: str, accepted: bool) -> None:
        config = copy.deepcopy(reference_config())
        first_controller(config)["engine_ref"] = value
        self.assertEqual(schema_accepts_engine_ref(value), accepted)
        self.assertEqual(errors_for(config) == [], accepted)

    def assert_delivery_engine_contract(self, value: str, accepted: bool) -> None:
        config = copy.deepcopy(reference_config())
        config["organization"]["delivery_engine"] = value
        self.assertEqual(schema_accepts_delivery_engine(value), accepted)
        self.assertEqual(errors_for(config) == [], accepted)

    def test_reference_configuration_is_valid(self) -> None:
        self.assertEqual(errors_for(reference_config()), [])

    def test_multi_host_multi_location_configuration_is_valid(self) -> None:
        config = json.loads((ROOT / "examples" / "multi-host" / "fleet.json").read_text(encoding="utf-8"))
        self.assertEqual(errors_for(config), [])

    def test_schema_version_two_is_rejected(self) -> None:
        config = copy.deepcopy(reference_config())
        config["schema_version"] = 2
        self.assert_rejected(config, "must equal 3")

    def test_public_repository_access_is_rejected(self) -> None:
        config = copy.deepcopy(reference_config())
        config["runner_pools"]["trusted-ci"]["public_repositories"] = True
        self.assert_rejected(config, "trusted private repositories")

    def test_capacity_overcommit_is_rejected(self) -> None:
        config = copy.deepcopy(reference_config())
        overcommit = config["runner_pools"]["trusted-ci"]["capacity_budget"] + 1
        first_controller(config)["max_runners"] = overcommit
        self.assert_rejected(config, f"must cover {overcommit} runners")

    def test_drained_capacity_remains_reserved(self) -> None:
        config = copy.deepcopy(reference_config())
        overcommit = config["runner_pools"]["trusted-ci"]["capacity_budget"] + 1
        first_controller(config)["state"] = "drained"
        first_controller(config)["max_runners"] = overcommit
        self.assert_rejected(config, f"must cover {overcommit} runners")

    def test_disabled_capacity_is_not_reserved(self) -> None:
        config = copy.deepcopy(reference_config())
        first_controller(config)["state"] = "disabled"
        first_controller(config)["max_runners"] = 100
        self.assertEqual(errors_for(config), [])

    def test_duplicate_scale_set_is_rejected(self) -> None:
        config = copy.deepcopy(reference_config())
        duplicate = copy.deepcopy(first_controller(config))
        duplicate["location"] = "example-site-b"
        config["controllers"]["example-ci-02"] = duplicate
        config["runner_pools"]["trusted-ci"]["capacity_budget"] = 2
        self.assert_rejected(config, "scale_set_name: must be unique")

    def test_scale_set_must_include_controller_id(self) -> None:
        config = copy.deepcopy(reference_config())
        first_controller(config)["scale_set_name"] = "other-scale"
        self.assert_rejected(config, "must include the controller ID")

    def test_routing_label_must_not_equal_scale_set(self) -> None:
        config = copy.deepcopy(reference_config())
        config["runner_pools"]["trusted-ci"]["routing_labels"] = [first_controller(config)["scale_set_name"]]
        self.assert_rejected(config, "must not equal a controller scale-set name")

    def test_controller_pool_must_exist(self) -> None:
        config = copy.deepcopy(reference_config())
        first_controller(config)["pool"] = "missing"
        self.assert_rejected(config, "must reference a declared runner pool")

    def test_controller_pool_must_be_a_string(self) -> None:
        config = copy.deepcopy(reference_config())
        first_controller(config)["pool"] = ["trusted-ci"]
        self.assert_rejected(config, "must reference a declared runner pool")

    def test_delivery_engine_repository_is_fixed_in_schema_and_semantics(self) -> None:
        self.assert_delivery_engine_contract("RandomDevelopment/ci-fleet", True)

    def test_delivery_engine_rejects_another_repository(self) -> None:
        self.assert_delivery_engine_contract("attacker/engine", False)

    def test_delivery_engine_rejects_url_form(self) -> None:
        self.assert_delivery_engine_contract("https://github.com/RandomDevelopment/ci-fleet", False)

    def test_delivery_engine_rejects_credential_form(self) -> None:
        self.assert_delivery_engine_contract("user:password@RandomDevelopment/ci-fleet", False)

    def test_controller_address_is_rejected(self) -> None:
        config = copy.deepcopy(reference_config())
        first_controller(config)["ip_address"] = "192.0.2.10"
        self.assert_rejected(config, "host-local infrastructure details are forbidden")

    def test_full_lowercase_engine_ref_passes_schema_and_semantics(self) -> None:
        self.assert_engine_ref_contract("1" * 40, True)

    def test_uppercase_engine_ref_fails_schema_and_semantics(self) -> None:
        self.assert_engine_ref_contract("A" * 40, False)

    def test_short_engine_ref_fails_schema_and_semantics(self) -> None:
        self.assert_engine_ref_contract("1" * 39, False)

    def test_malformed_engine_ref_fails_schema_and_semantics(self) -> None:
        self.assert_engine_ref_contract("g" * 40, False)

    def test_zero_engine_ref_fails_schema_and_semantics(self) -> None:
        self.assert_engine_ref_contract("0" * 40, False)

    def test_active_controller_minimum_must_be_zero(self) -> None:
        config = copy.deepcopy(reference_config())
        first_controller(config)["min_runners"] = 1
        self.assert_rejected(config, "managed prewarmed runners are not supported")

    def test_drained_controller_minimum_must_be_zero(self) -> None:
        config = copy.deepcopy(reference_config())
        first_controller(config)["state"] = "drained"
        first_controller(config)["min_runners"] = 1
        self.assert_rejected(config, "managed prewarmed runners are not supported")

    def test_application_capacity_control_is_rejected(self) -> None:
        config = copy.deepcopy(reference_config())
        first_project(config)["ci_contract"]["max_parallel"] = 1
        self.assert_rejected(config, "unknown keys: max_parallel")

    def test_pool_must_submit_all_independent_jobs(self) -> None:
        config = copy.deepcopy(reference_config())
        config["runner_pools"]["trusted-ci"]["job_submission_policy"] = "max-parallel"
        self.assert_rejected(config, "leave capacity control to infrastructure")

    def test_automatic_production_is_rejected(self) -> None:
        config = copy.deepcopy(reference_config())
        config["environments"]["production"]["automatic"] = True
        self.assert_rejected(config, "production deployment must not be automatic")

    def test_unapproved_production_is_rejected(self) -> None:
        config = copy.deepcopy(reference_config())
        config["environments"]["production"]["requires_approval"] = False
        self.assert_rejected(config, "production deployment must require approval")

    def test_repository_must_be_in_pool_allowlist(self) -> None:
        config = copy.deepcopy(reference_config())
        config["runner_pools"]["trusted-ci"]["allowed_repositories"] = ["example-org/other-app"]
        self.assert_rejected(config, "explicitly allowed by its CI pool")

    def test_embedded_credential_url_is_rejected(self) -> None:
        config = copy.deepcopy(reference_config())
        config["organization"]["database_url"] = "post" + "gres://user:***@db.example.invalid/app"
        self.assert_rejected(config, "probable secret material")

    def test_secret_value_key_is_rejected(self) -> None:
        config = copy.deepcopy(reference_config())
        config["environments"]["development"]["token"] = "not-a-real-token"
        self.assert_rejected(config, "secret values are forbidden")

    def test_host_local_environment_paths_are_rejected(self) -> None:
        validation = Validation()
        with tempfile.TemporaryDirectory() as directory:
            path_list = Path(directory) / "paths"
            path_list.write_bytes(b"host.env\0ci-fleet.env\0nested/host.env\0nested/ci-fleet.env\0")
            scan_tree_path_list(path_list, validation)
        self.assertEqual(len(validation.errors), 4, validation.errors)
        self.assertTrue(all("secret-bearing files are forbidden" in error for error in validation.errors), validation.errors)

    def test_template_ci_scans_committed_file_contents(self) -> None:
        scanner = ROOT / "scripts" / "scan_committed_secrets.py"
        self.assertTrue(scanner.is_file())
        workflow = (ROOT / ".github" / "workflows" / "validate.yml").read_text(encoding="utf-8")
        self.assertIn("python3 scripts/scan_committed_secrets.py", workflow)
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            subprocess.run(["git", "init", "-q", str(repository)], check=True)
            for relative in ("scripts/scan_committed_secrets.py", "scripts/validate.sh"):
                path = repository / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("ghp_" + "x" * 20 + "\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
            result = subprocess.run(
                [sys.executable, str(scanner), "--repository", str(repository)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("scripts/scan_committed_secrets.py:1", result.stderr)
        self.assertIn("scripts/validate.sh:1", result.stderr)

    def test_duplicate_json_controller_id_is_rejected(self) -> None:
        validation = Validation()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duplicate.json"
            path.write_text('{"controllers":{"ci-01":{},"ci-01":{}}}', encoding="utf-8")
            self.assertIsNone(load_json(path, validation))
        self.assertTrue(any("duplicate object key: ci-01" in error for error in validation.errors), validation.errors)

    def test_strict_mode_rejects_unchanged_example(self) -> None:
        config = copy.deepcopy(reference_config())
        project = first_project(config)
        config["organization"]["slug"] = "example-org"
        config["runner_pools"][project["ci_pool"]]["allowed_repositories"] = ["example-org/example-app"]
        project["repository"] = "example-org/example-app"
        self.assert_rejected(config, "replace the example organization", strict=True)

    def test_nonstandard_ci_entrypoint_is_rejected(self) -> None:
        config = copy.deepcopy(reference_config())
        first_project(config)["ci_contract"]["aggregate_entrypoints"]["fast"] = "npm test"
        self.assert_rejected(config, "standard aggregate fast entrypoint")

    def test_job_ceiling_above_five_minutes_is_rejected(self) -> None:
        config = copy.deepcopy(reference_config())
        first_project(config)["ci_contract"]["max_job_minutes"] = 10
        self.assert_rejected(config, "five-minute hard job ceiling")

    def test_shard_target_must_reserve_startup_time(self) -> None:
        config = copy.deepcopy(reference_config())
        first_project(config)["ci_contract"]["shard_target_minutes"] = 5
        self.assert_rejected(config, "reserve startup time")

    def test_standard_task_plan_path_is_required(self) -> None:
        config = copy.deepcopy(reference_config())
        first_project(config)["ci_contract"]["task_plan"] = "ci/custom.json"
        self.assert_rejected(config, "standard task-plan path")


if __name__ == "__main__":
    unittest.main()
