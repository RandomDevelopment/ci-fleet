#!/usr/bin/env python3
"""Regression tests for ci-fleet task-plan expansion."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import unittest
from pathlib import Path

from plan import validate_and_expand


ROOT = Path(__file__).resolve().parents[3]


def sample_plan() -> dict:
    return json.loads((ROOT / "examples/project/scripts/ci/plan.json").read_text(encoding="utf-8"))


class PlanTests(unittest.TestCase):
    def assert_rejected(self, plan: dict, expected: str) -> None:
        with self.assertRaisesRegex(ValueError, expected):
            validate_and_expand(plan, "full")

    def test_example_expands_to_independent_shards(self) -> None:
        matrix, total = validate_and_expand(sample_plan(), "full")
        self.assertEqual(len(matrix["include"]), 13)
        self.assertEqual(total, 45)

    def test_fast_is_a_smaller_group(self) -> None:
        matrix, total = validate_and_expand(sample_plan(), "fast")
        self.assertEqual(len(matrix["include"]), 6)
        self.assertEqual(total, 20)

    def test_five_minute_job_ceiling_is_mandatory(self) -> None:
        plan = sample_plan()
        plan["max_job_minutes"] = 6
        self.assert_rejected(plan, "must equal 5")

    def test_shard_estimate_cannot_consume_startup_reserve(self) -> None:
        plan = sample_plan()
        plan["tasks"][0]["estimated_minutes_per_shard"] = 5
        self.assert_rejected(plan, "at most 4")

    def test_duplicate_task_ids_are_rejected(self) -> None:
        plan = sample_plan()
        plan["tasks"][1]["id"] = plan["tasks"][0]["id"]
        self.assert_rejected(plan, "duplicates")

    def test_fast_tasks_must_also_run_in_full(self) -> None:
        plan = sample_plan()
        plan["tasks"][0]["groups"] = ["fast"]
        self.assert_rejected(plan, "whenever it includes fast")

    def test_action_runs_planner_in_pinned_runtime_container(self) -> None:
        action = (ROOT / ".github/actions/plan/action.yml").read_text(encoding="utf-8")
        self.assertIn(
            "python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7",
            action,
        )
        self.assertIn("--network none", action)
        self.assertIn("--read-only", action)
        self.assertIn("--cap-drop all", action)
        self.assertNotIn('python3 "${GITHUB_ACTION_PATH}/plan.py"', action)

    def test_cli_can_emit_github_outputs_to_stdout(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / ".github/actions/plan/plan.py"),
                "--plan",
                str(ROOT / "examples/project/scripts/ci/plan.json"),
                "--group",
                "fast",
                "--github-output",
                "-",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.stdout.count("matrix="), 1)
        self.assertIn("job-count=6\n", result.stdout)
        self.assertIn("estimated-test-minutes=20\n", result.stdout)
        self.assertNotIn("\n{", result.stdout)

    def test_matrix_limit_is_enforced(self) -> None:
        plan = sample_plan()
        plan["tasks"][0]["shards"] = 257
        self.assert_rejected(plan, "at most 256")


if __name__ == "__main__":
    unittest.main()
