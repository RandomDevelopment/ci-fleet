#!/usr/bin/env python3
"""Regression tests for ci-fleet task-plan expansion."""

from __future__ import annotations

import copy
import json
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

    def test_matrix_limit_is_enforced(self) -> None:
        plan = sample_plan()
        plan["tasks"][0]["shards"] = 257
        self.assert_rejected(plan, "at most 256")


if __name__ == "__main__":
    unittest.main()
