#!/usr/bin/env python3
"""Validate a ci-fleet organization configuration without third-party packages."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
ORG_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,38}$")
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
IMAGE = re.compile(r"^[a-z0-9.-]+/[a-z0-9._/-]+$")
SECRET_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
HIGH_CONFIDENCE_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"gh[opusr]_[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?:postgres|mysql|mongodb(?:\+srv)?|redis)://[^\s/:]+:[^\s/@]+@"),
)
FORBIDDEN_SECRET_KEYS = {
    "access_token",
    "api_key",
    "credential",
    "credentials",
    "database_url",
    "password",
    "private_key",
    "secret",
    "secret_value",
    "token",
}
FORBIDDEN_FILENAMES = re.compile(r"(?:^|/)\.env(?:\..+)?$|\.(?:key|pem|p12|pfx)$", re.IGNORECASE)
FORBIDDEN_DIRECTORIES = {"credentials", "private", "secrets"}


class Validation:
    def __init__(self) -> None:
        self.errors: list[str] = []

    def require(self, condition: bool, path: str, message: str) -> None:
        if not condition:
            self.errors.append(f"{path}: {message}")

    def exact_keys(self, value: Any, path: str, required: set[str], optional: set[str] | None = None) -> bool:
        if not isinstance(value, dict):
            self.errors.append(f"{path}: must be an object")
            return False
        optional = optional or set()
        keys = set(value)
        missing = required - keys
        unknown = keys - required - optional
        if missing:
            self.errors.append(f"{path}: missing keys: {', '.join(sorted(missing))}")
        if unknown:
            self.errors.append(f"{path}: unknown keys: {', '.join(sorted(unknown))}")
        return not missing


def load_json(path: Path, validation: Validation) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        validation.errors.append(f"{path}: file not found")
    except json.JSONDecodeError as exc:
        validation.errors.append(f"{path}:{exc.lineno}:{exc.colno}: invalid JSON: {exc.msg}")
    return None


def strings_in(value: Any, path: str = "$"):
    if isinstance(value, dict):
        for key, child in value.items():
            yield from strings_in(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from strings_in(child, f"{path}[{index}]")
    elif isinstance(value, str):
        yield path, value


def scan_secret_material(config: Any, validation: Validation) -> None:
    def scan_keys(value: Any, path: str = "$") -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key.lower() in FORBIDDEN_SECRET_KEYS:
                    validation.errors.append(
                        f"{path}.{key}: secret values are forbidden; store only an uppercase secret name"
                    )
                scan_keys(child, f"{path}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                scan_keys(child, f"{path}[{index}]")

    scan_keys(config)
    for path, value in strings_in(config):
        for pattern in HIGH_CONFIDENCE_SECRET_PATTERNS:
            if pattern.search(value):
                validation.errors.append(f"{path}: probable secret material is forbidden")
                break


def scan_forbidden_paths(repo_root: Path, validation: Validation) -> None:
    for path in repo_root.rglob("*"):
        try:
            relative = path.relative_to(repo_root)
        except ValueError:
            continue
        if ".git" in relative.parts:
            continue
        relative_text = relative.as_posix()
        if path.is_dir() and path.name.lower() in FORBIDDEN_DIRECTORIES:
            validation.errors.append(f"{relative_text}/: secret-bearing directory names are forbidden")
        elif path.is_file() and FORBIDDEN_FILENAMES.search(relative_text):
            validation.errors.append(f"{relative_text}: secret-bearing files are forbidden")


def validate_config(config: Any, validation: Validation, strict: bool) -> None:
    required_top = {"schema_version", "organization", "runner_pools", "host_groups", "environments", "projects"}
    if not validation.exact_keys(config, "$", required_top, {"$schema"}):
        return

    validation.require(config.get("schema_version") == 1, "$.schema_version", "must equal 1")

    organization = config.get("organization")
    organization_keys = {"slug", "registry", "delivery_engine", "workflow_ref_policy"}
    if validation.exact_keys(organization, "$.organization", organization_keys):
        slug = organization.get("slug")
        registry = organization.get("registry")
        engine = organization.get("delivery_engine")
        validation.require(isinstance(slug, str) and bool(ORG_SLUG.fullmatch(slug)), "$.organization.slug", "must be a lowercase GitHub organization slug")
        validation.require(isinstance(registry, str) and bool(IMAGE.fullmatch(registry)), "$.organization.registry", "must be a registry namespace such as ghcr.io/acme")
        validation.require(isinstance(engine, str) and bool(REPOSITORY.fullmatch(engine)), "$.organization.delivery_engine", "must be an owner/repository name")
        validation.require(organization.get("workflow_ref_policy") == "immutable-commit", "$.organization.workflow_ref_policy", "must equal immutable-commit")
        if strict:
            validation.require(slug != "example-org", "$.organization.slug", "replace the example organization before use")

    pools = config.get("runner_pools")
    if not isinstance(pools, dict) or not pools:
        validation.errors.append("$.runner_pools: must be a non-empty object")
        pools = {}
    for name, pool in pools.items():
        path = f"$.runner_pools.{name}"
        validation.require(bool(SLUG.fullmatch(name)), path, "pool name must be a lowercase slug")
        if not validation.exact_keys(pool, path, {"routing_labels", "allowed_repositories", "public_repositories", "max_concurrent_jobs"}):
            continue
        labels = pool.get("routing_labels")
        repos = pool.get("allowed_repositories")
        validation.require(isinstance(labels, list) and bool(labels), f"{path}.routing_labels", "must be a non-empty list")
        if isinstance(labels, list):
            validation.require(len(labels) == len(set(labels)), f"{path}.routing_labels", "must contain unique labels")
            for index, label in enumerate(labels):
                validation.require(isinstance(label, str) and bool(SLUG.fullmatch(label)), f"{path}.routing_labels[{index}]", "must be a lowercase slug")
                validation.require(str(label).lower() != "self-hosted", f"{path}.routing_labels[{index}]", "do not repeat GitHub's implicit self-hosted label")
        validation.require(isinstance(repos, list) and bool(repos), f"{path}.allowed_repositories", "must be a non-empty list")
        if isinstance(repos, list):
            validation.require(len(repos) == len(set(repos)), f"{path}.allowed_repositories", "must contain unique repositories")
            for index, repository in enumerate(repos):
                validation.require(isinstance(repository, str) and bool(REPOSITORY.fullmatch(repository)), f"{path}.allowed_repositories[{index}]", "must be owner/repository")
        validation.require(pool.get("public_repositories") is False, f"{path}.public_repositories", "must be false; this fleet is for trusted private repositories")
        jobs = pool.get("max_concurrent_jobs")
        validation.require(type(jobs) is int and jobs > 0, f"{path}.max_concurrent_jobs", "must be a positive integer")

    groups = config.get("host_groups")
    if not isinstance(groups, dict) or not groups:
        validation.errors.append("$.host_groups: must be a non-empty object")
        groups = {}
    for name, group in groups.items():
        path = f"$.host_groups.{name}"
        validation.require(bool(SLUG.fullmatch(name)), path, "host group name must be a lowercase slug")
        if validation.exact_keys(group, path, {"role", "environment_class"}):
            validation.require(group.get("role") == "deployment", f"{path}.role", "must equal deployment; CI workers and deployment hosts are separate")
            validation.require(group.get("environment_class") in {"development", "staging", "production"}, f"{path}.environment_class", "must be development, staging, or production")

    environments = config.get("environments")
    if not isinstance(environments, dict) or not environments:
        validation.errors.append("$.environments: must be a non-empty object")
        environments = {}
    for name, environment in environments.items():
        path = f"$.environments.{name}"
        validation.require(bool(SLUG.fullmatch(name)), path, "environment name must be a lowercase slug")
        if not validation.exact_keys(environment, path, {"host_group", "automatic", "requires_approval", "required_secret_names"}):
            continue
        host_group = environment.get("host_group")
        validation.require(host_group in groups, f"{path}.host_group", "must reference a declared deployment host group")
        validation.require(type(environment.get("automatic")) is bool, f"{path}.automatic", "must be a boolean")
        validation.require(type(environment.get("requires_approval")) is bool, f"{path}.requires_approval", "must be a boolean")
        names = environment.get("required_secret_names")
        validation.require(isinstance(names, list), f"{path}.required_secret_names", "must be a list")
        if isinstance(names, list):
            validation.require(len(names) == len(set(names)), f"{path}.required_secret_names", "must contain unique names")
            for index, secret_name in enumerate(names):
                validation.require(isinstance(secret_name, str) and bool(SECRET_NAME.fullmatch(secret_name)), f"{path}.required_secret_names[{index}]", "must be an uppercase secret name, never a value")
        if host_group in groups and groups[host_group].get("environment_class") == "production":
            validation.require(environment.get("automatic") is False, f"{path}.automatic", "production deployment must not be automatic")
            validation.require(environment.get("requires_approval") is True, f"{path}.requires_approval", "production deployment must require approval")

    projects = config.get("projects")
    if not isinstance(projects, dict) or not projects:
        validation.errors.append("$.projects: must be a non-empty object")
        projects = {}
    for name, project in projects.items():
        path = f"$.projects.{name}"
        validation.require(bool(SLUG.fullmatch(name)), path, "project name must be a lowercase slug")
        if not validation.exact_keys(project, path, {"repository", "image", "ci_pool", "ci_entrypoints", "deployments"}):
            continue
        repository = project.get("repository")
        image = project.get("image")
        pool_name = project.get("ci_pool")
        validation.require(isinstance(repository, str) and bool(REPOSITORY.fullmatch(repository)), f"{path}.repository", "must be owner/repository")
        validation.require(isinstance(image, str) and bool(IMAGE.fullmatch(image)), f"{path}.image", "must be a container image path without a mutable tag")
        validation.require(pool_name in pools, f"{path}.ci_pool", "must reference a declared runner pool")
        if pool_name in pools and isinstance(pools[pool_name].get("allowed_repositories"), list):
            validation.require(repository in pools[pool_name]["allowed_repositories"], f"{path}.repository", "must be explicitly allowed by its CI pool")
        entrypoints = project.get("ci_entrypoints")
        if validation.exact_keys(entrypoints, f"{path}.ci_entrypoints", {"fast", "full"}):
            validation.require(entrypoints.get("fast") == "./scripts/ci/run.sh fast", f"{path}.ci_entrypoints.fast", "must use the standard fast entrypoint")
            validation.require(entrypoints.get("full") == "./scripts/ci/run.sh full", f"{path}.ci_entrypoints.full", "must use the standard full entrypoint")
        deployments = project.get("deployments")
        validation.require(isinstance(deployments, list) and bool(deployments), f"{path}.deployments", "must be a non-empty list")
        if isinstance(deployments, list):
            validation.require(len(deployments) == len(set(deployments)), f"{path}.deployments", "must contain unique environments")
            for index, deployment in enumerate(deployments):
                validation.require(deployment in environments, f"{path}.deployments[{index}]", "must reference a declared environment")
        if strict:
            validation.require(repository != "example-org/example-app", f"{path}.repository", "replace the example repository before use")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "fleet.json", help="configuration file to validate")
    parser.add_argument("--strict", action="store_true", help="reject unchanged example values")
    parser.add_argument("--skip-path-scan", action="store_true", help="skip repository path checks (for external fixtures)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    validation = Validation()
    config = load_json(args.config.resolve(), validation)
    schema = load_json(ROOT / "fleet.schema.json", validation)
    if schema is not None:
        validation.require(schema.get("$schema") == "https://json-schema.org/draft/2020-12/schema", "fleet.schema.json.$schema", "must use JSON Schema draft 2020-12")
    if config is not None:
        scan_secret_material(config, validation)
        validate_config(config, validation, args.strict)
    if not args.skip_path_scan:
        scan_forbidden_paths(ROOT, validation)

    if validation.errors:
        for error in validation.errors:
            print(f"ERROR: {error}", file=sys.stderr)
        print(f"FAILED: {len(validation.errors)} validation error(s)", file=sys.stderr)
        return 1
    print(f"OK: {args.config} satisfies the ci-fleet configuration contract")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
