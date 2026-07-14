#!/usr/bin/env python3
"""Validate and expand a ci-fleet project task plan."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path
from typing import Any


TASK_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
PLAN_KEYS = {
    "schema_version",
    "target_wall_clock_minutes",
    "max_job_minutes",
    "shard_target_minutes",
    "tasks",
}
TASK_KEYS = {"id", "groups", "shards", "estimated_minutes_per_shard"}


def fail(message: str) -> None:
    raise ValueError(message)


def exact_keys(value: Any, required: set[str], path: str, optional: set[str] | None = None) -> None:
    if not isinstance(value, dict):
        fail(f"{path} must be an object")
    optional = optional or set()
    missing = required - set(value)
    unknown = set(value) - required - optional
    if missing:
        fail(f"{path} is missing: {', '.join(sorted(missing))}")
    if unknown:
        fail(f"{path} contains unknown keys: {', '.join(sorted(unknown))}")


def load_plan(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        fail(f"{path} was not found")
        raise AssertionError from exc
    except json.JSONDecodeError as exc:
        fail(f"{path}:{exc.lineno}:{exc.colno} is not valid JSON: {exc.msg}")
        raise AssertionError from exc
    exact_keys(value, PLAN_KEYS, "$", {"$schema"})
    return value


def validate_and_expand(plan: dict[str, Any], group: str) -> tuple[dict[str, list[dict[str, Any]]], int]:
    if group not in {"fast", "full"}:
        fail("group must be fast or full")
    if plan["schema_version"] != 1:
        fail("$.schema_version must equal 1")
    if plan["target_wall_clock_minutes"] != 5:
        fail("$.target_wall_clock_minutes must equal 5")
    if plan["max_job_minutes"] != 5:
        fail("$.max_job_minutes must equal 5")
    target = plan["shard_target_minutes"]
    if type(target) is not int or not 1 <= target <= 4:
        fail("$.shard_target_minutes must be an integer between 1 and 4")
    tasks = plan["tasks"]
    if not isinstance(tasks, list) or not tasks:
        fail("$.tasks must be a non-empty array")

    seen: set[str] = set()
    coverage = {"fast": 0, "full": 0}
    include: list[dict[str, Any]] = []
    estimated_total = 0
    for index, task in enumerate(tasks):
        path = f"$.tasks[{index}]"
        exact_keys(task, TASK_KEYS, path)
        task_id = task["id"]
        if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id):
            fail(f"{path}.id must be a lowercase slug")
        if task_id in {"fast", "full"}:
            fail(f"{path}.id uses a reserved aggregate name")
        if task_id in seen:
            fail(f"{path}.id duplicates {task_id}")
        seen.add(task_id)

        groups = task["groups"]
        if not isinstance(groups, list) or not groups or any(value not in {"fast", "full"} for value in groups):
            fail(f"{path}.groups must contain fast and/or full")
        if len(groups) != len(set(groups)):
            fail(f"{path}.groups must be unique")
        if "fast" in groups and "full" not in groups:
            fail(f"{path}.groups must include full whenever it includes fast")
        for value in groups:
            coverage[value] += 1

        shards = task["shards"]
        if type(shards) is not int or shards < 1:
            fail(f"{path}.shards must be a positive integer")
        estimate = task["estimated_minutes_per_shard"]
        if type(estimate) not in {int, float} or isinstance(estimate, bool) or estimate <= 0 or estimate > target:
            fail(f"{path}.estimated_minutes_per_shard must be greater than zero and at most {target}")

        if group in groups:
            for shard in range(1, shards + 1):
                include.append(
                    {
                        "task": task_id,
                        "shard": shard,
                        "shards": shards,
                        "estimated_minutes": estimate,
                    }
                )
                estimated_total += estimate

    if coverage["fast"] == 0 or coverage["full"] == 0:
        fail("$.tasks must provide both fast and full coverage")
    if len(include) > 256:
        fail(f"expanded {group} matrix has {len(include)} jobs; GitHub permits at most 256")
    if not include:
        fail(f"group {group} contains no tasks")
    return {"include": include}, math.ceil(estimated_total)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--group", choices=("fast", "full"), default="fast")
    parser.add_argument("--github-output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        plan = load_plan(args.plan)
        matrix, estimated_total = validate_and_expand(plan, args.group)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    encoded = json.dumps(matrix, separators=(",", ":"))
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as output:
            output.write(f"matrix={encoded}\n")
            output.write(f"job-count={len(matrix['include'])}\n")
            output.write(f"estimated-test-minutes={estimated_total}\n")
    print(encoded)
    print(
        f"OK: {args.group} expands to {len(matrix['include'])} jobs "
        f"covering approximately {estimated_total} test-minutes",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
