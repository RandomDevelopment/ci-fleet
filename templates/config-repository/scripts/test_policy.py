#!/usr/bin/env python3
"""Regression tests for ci-fleet's non-negotiable configuration policies."""

from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from validate import Validation, load_json, scan_secret_material, scan_tree_path_list, validate_config, validate_rollout_evidence, validate_transition


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
        images = next(iter(config["managed_images"].values()))
        config["managed_images"] = {value: images}
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

    def test_status_reporting_null_is_rejected(self) -> None:
        config = copy.deepcopy(reference_config())
        first_controller(config)["status_reporting"] = None
        self.assert_rejected(config, "must be an object")

    def test_initializer_omits_status_reporting_for_staged_adoption(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "fleet.json"
            subprocess.run([
                sys.executable, str(ROOT / "scripts" / "init.py"),
                "--organization", "sample-org", "--project", "sample-app",
                "--engine-ref", "1" * 40,
                "--controller-image-digest", "sha256:" + "1" * 64,
                "--runner-image-digest", "sha256:" + "2" * 64,
                "--output", str(output),
            ], check=True, stdout=subprocess.DEVNULL)
            controller = first_controller(json.loads(output.read_text()))
        self.assertNotIn("status_reporting", controller)

    def test_status_reporting_requires_a_separate_engine_rollout(self) -> None:
        previous = reference_config()
        current = copy.deepcopy(previous)
        first_controller(current)["engine_ref"] = "2" * 40
        first_controller(current)["status_reporting"] = {
            "enabled": False,
            "config_file": "/etc/ci-fleet/monitoring.env",
        }
        validation = Validation()
        validate_transition(previous, current, {}, validation)
        self.assertTrue(any("later commit" in error for error in validation.errors), validation.errors)

        staged = copy.deepcopy(current)
        first_controller(staged).pop("status_reporting")
        validation = Validation()
        validate_transition(staged, current, {}, validation)
        self.assertTrue(any("rollout evidence" in error for error in validation.errors), validation.errors)
        validation = Validation()
        controller_name = next(iter(staged["controllers"]))
        validate_transition(staged, current, {
            controller_name: {
                "engine_ref": first_controller(staged)["engine_ref"],
                "status_reporting_config": True,
                "required_status_reporting": False,
            },
        }, validation)
        self.assertEqual(validation.errors, [])
        validation = Validation()
        validate_transition(staged, current, {
            controller_name: {
                "engine_ref": first_controller(staged)["engine_ref"],
                "status_reporting_config": True,
                "required_status_reporting": False,
            },
        }, validation, {})
        self.assertTrue(any("capability evidence" in error for error in validation.errors), validation.errors)

    def test_retained_reporting_requires_target_engine_capabilities(self) -> None:
        previous = reference_config()
        first_controller(previous)["status_reporting"] = {
            "enabled": False,
            "config_file": "/etc/ci-fleet/monitoring.env",
        }
        current = copy.deepcopy(previous)
        first_controller(current)["engine_ref"] = "2" * 40
        controller = next(iter(current["controllers"]))
        compatible = {
            controller: {
                "engine_ref": "2" * 40,
                "status_reporting_config": True,
                "required_status_reporting": False,
            }
        }
        validation = Validation()
        validate_transition(previous, current, compatible, validation)
        self.assertEqual(validation.errors, [])

        for evidence in (
            {},
            {controller: {**compatible[controller], "engine_ref": "3" * 40}},
            {controller: {**compatible[controller], "status_reporting_config": False}},
        ):
            validation = Validation()
            validate_transition(previous, current, evidence, validation)
            self.assertTrue(any("configuration capability evidence" in error for error in validation.errors), validation.errors)

        first_controller(previous)["status_reporting"]["enabled"] = True
        first_controller(current)["status_reporting"]["enabled"] = True
        validation = Validation()
        validate_transition(previous, current, compatible, validation)
        self.assertTrue(any("required status-reporting" in error for error in validation.errors), validation.errors)
        compatible[controller]["required_status_reporting"] = True
        validation = Validation()
        validate_transition(previous, current, compatible, validation)
        self.assertEqual(validation.errors, [])

    def test_reporting_removal_before_engine_change_needs_no_evidence(self) -> None:
        previous = reference_config()
        first_controller(previous)["status_reporting"] = {
            "enabled": True,
            "config_file": "/etc/ci-fleet/monitoring.env",
        }
        removed = copy.deepcopy(previous)
        first_controller(removed).pop("status_reporting")
        validation = Validation()
        validate_transition(previous, removed, {}, validation)
        self.assertEqual(validation.errors, [])

        changed = copy.deepcopy(removed)
        first_controller(changed)["engine_ref"] = "2" * 40
        validation = Validation()
        validate_transition(removed, changed, {}, validation)
        self.assertEqual(validation.errors, [])

    def test_rollout_evidence_requires_explicit_capability_booleans(self) -> None:
        base = {"engine_ref": "1" * 40, "status_reporting_config": True, "required_status_reporting": False}
        for evidence in (
            {key: value for key, value in base.items() if key != "status_reporting_config"},
            {**base, "status_reporting_config": None},
            {**base, "required_status_reporting": None},
        ):
            validation = Validation()
            self.assertEqual(validate_rollout_evidence({
                "schema_version": 1,
                "status_reporting_engine_capabilities": {"example-ci-01": evidence},
            }, validation), {})
            self.assertTrue(validation.errors)

    def test_new_controller_cannot_introduce_status_reporting(self) -> None:
        previous = reference_config()
        current = copy.deepcopy(previous)
        controller = copy.deepcopy(first_controller(current))
        controller["scale_set_name"] = "example-ci-02-scale"
        controller["status_reporting"] = {
            "enabled": False,
            "config_file": "/etc/ci-fleet/monitoring.env",
        }
        current["controllers"]["example-ci-02"] = controller
        validation = Validation()
        validate_transition(previous, current, {}, validation)
        self.assertTrue(any("new controller" in error for error in validation.errors), validation.errors)

    def test_rollout_evidence_is_scoped_to_its_controller(self) -> None:
        previous = reference_config()
        second = copy.deepcopy(first_controller(previous))
        second["scale_set_name"] = "example-ci-02-scale"
        previous["controllers"]["example-ci-02"] = second
        current = copy.deepcopy(previous)
        current["controllers"]["example-ci-02"]["status_reporting"] = {
            "enabled": False,
            "config_file": "/etc/ci-fleet/monitoring.env",
        }
        validation = Validation()
        validate_transition(previous, current, {
            "example-ci-01": {
                "engine_ref": second["engine_ref"],
                "status_reporting_config": True,
                "required_status_reporting": False,
            },
        }, validation)
        self.assertTrue(any("for this controller" in error for error in validation.errors), validation.errors)

    def test_rollout_evidence_requires_controller_mapping(self) -> None:
        validation = Validation()
        evidence = {
            "engine_ref": "1" * 40,
            "status_reporting_config": True,
            "required_status_reporting": False,
        }
        refs = validate_rollout_evidence({
            "schema_version": 1,
            "status_reporting_engine_capabilities": {"example-ci-01": evidence},
        }, validation)
        self.assertEqual(refs, {"example-ci-01": evidence})
        self.assertEqual(validation.errors, [])

    def test_rollout_evidence_requires_previous_engine_selection(self) -> None:
        previous = reference_config()
        current = copy.deepcopy(previous)
        first_controller(current)["engine_ref"] = "2" * 40
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "previous.json").write_text(json.dumps(previous), encoding="utf-8")
            (root / "current.json").write_text(json.dumps(current), encoding="utf-8")
            (root / "previous-evidence.json").write_text(json.dumps({
                "schema_version": 1,
                "status_reporting_engine_capabilities": {},
            }), encoding="utf-8")
            (root / "evidence.json").write_text(json.dumps({
                "schema_version": 1,
                "status_reporting_engine_capabilities": {
                    next(iter(current["controllers"])): {
                        "engine_ref": "2" * 40,
                        "status_reporting_config": True,
                        "required_status_reporting": False,
                    },
                },
            }), encoding="utf-8")
            result = subprocess.run([
                sys.executable, str(ROOT / "scripts" / "validate.py"),
                "--config", str(root / "current.json"),
                "--previous-config", str(root / "previous.json"),
                "--rollout-evidence", str(root / "evidence.json"),
                "--previous-rollout-evidence", str(root / "previous-evidence.json"),
                "--skip-path-scan",
            ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must already be selected", result.stderr)

    def test_rollout_evidence_must_match_current_controller_ref(self) -> None:
        previous = reference_config()
        current = copy.deepcopy(previous)
        controller_name = next(iter(current["controllers"]))
        proven_ref = first_controller(previous)["engine_ref"]
        first_controller(current)["engine_ref"] = "2" * 40
        evidence = {
            "schema_version": 1,
            "status_reporting_engine_capabilities": {
                controller_name: {
                    "engine_ref": proven_ref,
                    "status_reporting_config": True,
                    "required_status_reporting": False,
                },
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, value in (
                ("previous.json", previous),
                ("current.json", current),
                ("previous-evidence.json", evidence),
                ("evidence.json", evidence),
            ):
                (root / name).write_text(json.dumps(value), encoding="utf-8")
            result = subprocess.run([
                sys.executable, str(ROOT / "scripts" / "validate.py"),
                "--config", str(root / "current.json"),
                "--previous-config", str(root / "previous.json"),
                "--rollout-evidence", str(root / "evidence.json"),
                "--previous-rollout-evidence", str(root / "previous-evidence.json"),
                "--skip-path-scan",
            ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must match the current controller engine_ref", result.stderr)

    def test_enabling_required_reporting_needs_required_capability_evidence(self) -> None:
        previous = reference_config()
        controller_name = next(iter(previous["controllers"]))
        first_controller(previous)["status_reporting"] = {
            "enabled": False,
            "config_file": "/etc/ci-fleet/monitoring.env",
        }
        current = copy.deepcopy(previous)
        first_controller(current)["status_reporting"]["enabled"] = True
        evidence = {
            "schema_version": 1,
            "status_reporting_engine_capabilities": {
                controller_name: {
                    "engine_ref": first_controller(previous)["engine_ref"],
                    "status_reporting_config": True,
                    "required_status_reporting": False,
                },
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, value in (
                ("previous.json", previous),
                ("current.json", current),
                ("previous-evidence.json", evidence),
                ("evidence.json", evidence),
            ):
                (root / name).write_text(json.dumps(value), encoding="utf-8")
            result = subprocess.run([
                sys.executable, str(ROOT / "scripts" / "validate.py"),
                "--config", str(root / "current.json"),
                "--previous-config", str(root / "previous.json"),
                "--rollout-evidence", str(root / "evidence.json"),
                "--previous-rollout-evidence", str(root / "previous-evidence.json"),
                "--skip-path-scan",
            ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires required status-reporting rollout evidence", result.stderr)
        validation = Validation()
        validate_transition(previous, current, {
            controller_name: {
                "engine_ref": first_controller(previous)["engine_ref"],
                "status_reporting_config": True,
                "required_status_reporting": True,
            },
        }, validation)
        self.assertEqual(validation.errors, [])

    def test_alternate_config_does_not_use_fleet_rollout_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            template = Path(directory) / "template"
            shutil.copytree(ROOT, template)
            (template / "engine-rollout-evidence.json").write_text(json.dumps({
                "schema_version": 1,
                "status_reporting_engine_capabilities": {
                    "private-ci-01": {
                        "engine_ref": "1" * 40,
                        "status_reporting_config": True,
                        "required_status_reporting": False,
                    },
                },
            }), encoding="utf-8")
            result = subprocess.run([
                sys.executable, str(template / "scripts" / "validate.py"),
                "--config", str(template / "examples" / "multi-host" / "fleet.json"),
                "--skip-path-scan",
            ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_default_fleet_config_cannot_be_a_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            template = Path(directory) / "template"
            shutil.copytree(ROOT, template)
            fleet = template / "fleet.json"
            fleet.rename(template / "fleet-target.json")
            fleet.symlink_to("fleet-target.json")
            result = subprocess.run([
                sys.executable, str(template / "scripts" / "validate.py"),
                "--skip-path-scan",
            ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symlinked JSON files are forbidden", result.stderr)

    def test_rollout_evidence_cannot_be_a_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            template = Path(directory) / "template"
            shutil.copytree(ROOT, template)
            evidence = template / "engine-rollout-evidence.json"
            evidence.rename(template / "evidence-target.json")
            evidence.symlink_to("evidence-target.json")
            result = subprocess.run([
                sys.executable, str(template / "scripts" / "validate.py"),
                "--skip-path-scan",
            ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symlinked JSON files are forbidden", result.stderr)

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

    def test_duplicate_runner_group_is_rejected(self) -> None:
        config = copy.deepcopy(reference_config())
        duplicate = copy.deepcopy(config["runner_pools"]["trusted-ci"])
        duplicate["routing_labels"] = ["other-ci"]
        config["runner_pools"]["other-ci"] = duplicate
        self.assert_rejected(config, "runner_group: must be unique")

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
        self.assertIn('python3 "$scanner" --repository "$GITHUB_WORKSPACE" --commit "$commit"', workflow)
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

    def test_committed_secret_scanner_reads_symlink_blobs(self) -> None:
        scanner = ROOT / "scripts" / "scan_committed_secrets.py"
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            subprocess.run(["git", "init", "-q", str(repository)], check=True)
            target = "ghp_" + "x" * 20
            (repository / target).write_text("clean\n", encoding="utf-8")
            (repository / "secret-link").symlink_to(target)
            subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
            result = subprocess.run(
                [sys.executable, str(scanner), "--repository", str(repository)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("secret-link:1", result.stderr)

    def test_committed_secret_scanner_reads_every_commit_in_range(self) -> None:
        scanner = ROOT / "scripts" / "scan_committed_secrets.py"
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            subprocess.run(["git", "init", "-q", str(repository)], check=True)
            subprocess.run(["git", "-C", str(repository), "config", "user.name", "Policy Test"], check=True)
            subprocess.run(["git", "-C", str(repository), "config", "user.email", "policy@example.invalid"], check=True)
            (repository / "README.md").write_text("clean\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repository), "commit", "-qm", "base"], check=True)
            base = subprocess.check_output(["git", "-C", str(repository), "rev-parse", "HEAD"], text=True).strip()
            leak = repository / "temporary-leak.txt"
            leak.write_text("ghp_" + "x" * 20 + "\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repository), "commit", "-qm", "add leak"], check=True)
            leaked = subprocess.check_output(["git", "-C", str(repository), "rev-parse", "HEAD"], text=True).strip()
            leak.unlink()
            subprocess.run(["git", "-C", str(repository), "add", "-u"], check=True)
            subprocess.run(["git", "-C", str(repository), "commit", "-qm", "remove leak"], check=True)
            head = subprocess.check_output(["git", "-C", str(repository), "rev-parse", "HEAD"], text=True).strip()
            for revision in (f"{base}..{head}", head, f"{head}..{leaked}"):
                with self.subTest(revision=revision):
                    result = subprocess.run(
                        [sys.executable, str(scanner), "--repository", str(repository), "--commit-range", revision],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("temporary-leak.txt:1", result.stderr)

    def test_committed_secret_scanner_rejects_historical_forbidden_paths(self) -> None:
        scanner = ROOT / "scripts" / "scan_committed_secrets.py"
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            subprocess.run(["git", "init", "-q", str(repository)], check=True)
            subprocess.run(["git", "-C", str(repository), "config", "user.name", "Policy Test"], check=True)
            subprocess.run(["git", "-C", str(repository), "config", "user.email", "policy@example.invalid"], check=True)
            (repository / "README.md").write_text("clean\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repository), "commit", "-qm", "base"], check=True)
            base = subprocess.check_output(["git", "-C", str(repository), "rev-parse", "HEAD"], text=True).strip()
            forbidden = repository / ".env"
            forbidden.write_text("MODE=test\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repository), "commit", "-qm", "add forbidden path"], check=True)
            forbidden.unlink()
            subprocess.run(["git", "-C", str(repository), "add", "-u"], check=True)
            subprocess.run(["git", "-C", str(repository), "commit", "-qm", "remove forbidden path"], check=True)
            head = subprocess.check_output(["git", "-C", str(repository), "rev-parse", "HEAD"], text=True).strip()
            result = subprocess.run(
                [sys.executable, str(scanner), "--repository", str(repository), "--commit-range", f"{base}..{head}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(".env: forbidden secret-bearing path", result.stderr)

    def test_committed_secret_scanner_reads_nested_repository_prefix(self) -> None:
        scanner = ROOT / "scripts" / "scan_committed_secrets.py"
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            nested = repository / "config"
            nested.mkdir()
            subprocess.run(["git", "init", "-q", str(repository)], check=True)
            (nested / "fleet.json").write_text("ghp_" + "x" * 20 + "\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
            result = subprocess.run(
                [sys.executable, str(scanner), "--repository", str(nested)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("fleet.json:1", result.stderr)

    def test_committed_secret_scanner_reads_unstaged_tracked_edits(self) -> None:
        scanner = ROOT / "scripts" / "scan_committed_secrets.py"
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            subprocess.run(["git", "init", "-q", str(repository)], check=True)
            tracked = repository / "fleet.json"
            tracked.write_text("clean\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
            tracked.write_text("ghp_" + "x" * 20 + "\n", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(scanner), "--repository", str(repository)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("fleet.json:1", result.stderr)

    def test_workflow_uses_trusted_scanner_for_complete_history(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "validate.yml").read_text(encoding="utf-8")
        for required in (
            "fetch-depth: 0",
            'git show "$BASE_SHA:$scanner"',
            'git rev-list --reverse "$HEAD_SHA"',
            'git rev-list --reverse "$BASE_SHA..$HEAD_SHA"',
            'commits+=("$HEAD_SHA")',
            '--commit "$commit"',
        ):
            self.assertIn(required, workflow)

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
