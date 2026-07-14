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
PROJECT_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--organization", required=True, help="GitHub organization slug")
    parser.add_argument("--project", required=True, help="initial project slug")
    parser.add_argument("--repository", help="owner/repository; defaults to ORGANIZATION/PROJECT")
    parser.add_argument("--registry", help="registry namespace; defaults to ghcr.io/ORGANIZATION")
    parser.add_argument("--runner-label", default="docker-ci", help="capability label for the shared CI pool")
    parser.add_argument("--output", type=Path, default=ROOT / "fleet.json", help="output configuration path")
    parser.add_argument("--force", action="store_true", help="replace an existing non-example output file")
    return parser.parse_args()


def fail(message: str) -> None:
    raise SystemExit(f"ERROR: {message}")


def main() -> int:
    args = parse_args()
    if not ORG_SLUG.fullmatch(args.organization):
        fail("--organization must be a lowercase GitHub organization slug")
    if not PROJECT_SLUG.fullmatch(args.project):
        fail("--project must be a lowercase slug")
    if not PROJECT_SLUG.fullmatch(args.runner_label):
        fail("--runner-label must be a lowercase slug")

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
        "schema_version": 1,
        "organization": {
            "slug": args.organization,
            "registry": registry,
            "delivery_engine": "RandomDevelopment/ci-fleet",
            "workflow_ref_policy": "immutable-commit",
        },
        "runner_pools": {
            "trusted-ci": {
                "routing_labels": [args.runner_label],
                "allowed_repositories": [repository],
                "public_repositories": False,
                "max_concurrent_jobs": 1,
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
                "ci_entrypoints": {
                    "fast": "./scripts/ci/run.sh fast",
                    "full": "./scripts/ci/run.sh full",
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
    print("Next: edit logical mappings, configure GitHub Environments, and keep every secret value outside Git.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
