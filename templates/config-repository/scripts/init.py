#!/usr/bin/env python3
"""Initialize a ci-fleet configuration repository for one private project."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ORG_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,38}$")
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
DEFAULT_ENGINE_REF = "f657117b1e1b5b94823d00fee4d35904c1ff8b72"


def positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--organization", required=True, help="GitHub organization slug")
    parser.add_argument("--project", required=True, help="initial project slug")
    parser.add_argument("--repository", help="owner/repository; defaults to ORGANIZATION/PROJECT")
    parser.add_argument("--registry", help="registry namespace; defaults to ghcr.io/ORGANIZATION")
    parser.add_argument("--runner-label", default="docker-ci", help="capability label for the shared CI pool")
    parser.add_argument("--runner-group", default="trusted-ci", help="logical GitHub runner-group slug")
    parser.add_argument("--controller", default="ci-01", help="logical controller ID and scale-set name")
    parser.add_argument("--location", default="primary-site", help="logical location slug; never an address")
    parser.add_argument("--capacity-budget", type=positive_integer, default=1, help="maximum capacity reserved by the pool")
    parser.add_argument("--max-runners", type=positive_integer, default=1, help="initial controller maximum")
    parser.add_argument("--runner-cpu-cores", type=positive_integer, default=2, help="CPU cores available to each runner")
    parser.add_argument("--runner-memory-mib", type=positive_integer, default=4096, help="memory available to each runner")
    parser.add_argument("--engine-ref", default=DEFAULT_ENGINE_REF, help="reviewed full ci-fleet commit SHA")
    parser.add_argument("--output", type=Path, default=ROOT / "fleet.json", help="output configuration path")
    parser.add_argument("--force", action="store_true", help="replace an existing non-example output file")
    return parser.parse_args()


def fail(message: str) -> None:
    raise SystemExit(f"ERROR: {message}")


def main() -> int:
    args = parse_args()
    if not ORG_SLUG.fullmatch(args.organization):
        fail("--organization must be a lowercase GitHub organization slug")
    for option, value in {
        "--project": args.project,
        "--runner-label": args.runner_label,
        "--runner-group": args.runner_group,
        "--controller": args.controller,
        "--location": args.location,
    }.items():
        if not SLUG.fullmatch(value):
            fail(f"{option} must be a lowercase slug")
    if not COMMIT_SHA.fullmatch(args.engine_ref) or args.engine_ref == "0" * 40:
        fail("--engine-ref must be a nonzero full lowercase commit SHA")
    if args.max_runners > args.capacity_budget:
        fail("--max-runners must not exceed --capacity-budget")
    if args.runner_memory_mib < 512:
        fail("--runner-memory-mib must be at least 512")

    repository = args.repository or f"{args.organization}/{args.project}"
    registry = (args.registry or f"ghcr.io/{args.organization}").rstrip("/")
    output = args.output.resolve()
    if output.exists() and not args.force:
        try:
            current = json.loads(output.read_text(encoding="utf-8"))
            example = current.get("organization", {}).get("slug") == "example-org"
        except (json.JSONDecodeError, AttributeError):
            example = False
        if not example:
            fail(f"{output} already exists; pass --force only if replacement is intentional")

    config = {
        "$schema": str((ROOT / "fleet.schema.json").resolve()) if output.parent != ROOT else "./fleet.schema.json",
        "schema_version": 3,
        "organization": {
            "slug": args.organization,
            "registry": registry,
            "delivery_engine": "RandomDevelopment/ci-fleet",
            "workflow_ref_policy": "immutable-commit",
        },
        "runner_pools": {
            "trusted-ci": {
                "runner_group": args.runner_group,
                "routing_labels": [args.runner_label],
                "allowed_repositories": [repository],
                "public_repositories": False,
                "capacity_budget": args.capacity_budget,
                "job_submission_policy": "all-independent-jobs",
            }
        },
        "controllers": {
            args.controller: {
                "pool": "trusted-ci",
                "location": args.location,
                "state": "active",
                "scale_set_name": args.controller,
                "lifecycle": "experimental",
                "engine_ref": args.engine_ref,
                "min_runners": 0,
                "max_runners": args.max_runners,
                "runner_resources": {
                    "cpu_cores": args.runner_cpu_cores,
                    "memory_mib": args.runner_memory_mib,
                },
            }
        },
        "host_groups": {
            "development-apps": {"role": "deployment", "environment_class": "development"},
            "production-apps": {"role": "deployment", "environment_class": "production"},
        },
        "environments": {
            "development": {
                "host_group": "development-apps",
                "automatic": True,
                "requires_approval": False,
                "required_secret_names": ["DEPLOY_AUTH"],
            },
            "production": {
                "host_group": "production-apps",
                "automatic": False,
                "requires_approval": True,
                "required_secret_names": ["DEPLOY_AUTH"],
            },
        },
        "projects": {
            args.project: {
                "repository": repository,
                "image": f"{registry}/{args.project}",
                "ci_pool": "trusted-ci",
                "ci_contract": {
                    "runner_entrypoint": "./scripts/ci/run.sh",
                    "task_plan": "./scripts/ci/plan.json",
                    "aggregate_entrypoints": {
                        "fast": "./scripts/ci/run.sh fast",
                        "full": "./scripts/ci/run.sh full",
                    },
                    "target_wall_clock_minutes": 5,
                    "max_job_minutes": 5,
                    "shard_target_minutes": 4,
                },
                "deployments": ["development", "production"],
            }
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    subprocess.run(
        [str(ROOT / "scripts" / "validate.sh"), "--strict", "--skip-path-scan", "--config", str(output)],
        check=True,
    )
    print(f"Initialized {output}")
    print("Next: review controller capacity, configure GitHub policy, and keep every secret value outside Git.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
