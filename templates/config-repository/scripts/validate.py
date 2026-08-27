#!/usr/bin/env python3
"""Validate a ci-fleet organization configuration without third-party packages."""

from __future__ import annotations

import argparse
import ipaddress
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
COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
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
FORBIDDEN_INFRASTRUCTURE_KEYS = {
    "backup_id",
    "backup_snapshot",
    "backup_storage",
    "disk_storage",
    "host_address",
    "hostname",
    "ip",
    "ip_address",
    "proxmox_vmid",
    "ssh_host",
    "ssh_password",
    "ssh_private_key",
    "vm_id",
    "vmid",
}
FORBIDDEN_FILENAMES = re.compile(r"(?:^|/)(?:\.env(?:\..+)?|host\.env|ci-fleet\.env)$|\.(?:key|pem|p12|pfx)$", re.IGNORECASE)
FORBIDDEN_DIRECTORIES = {"credentials", "private", "secrets"}
RFC_5737_NETWORKS = tuple(
    ipaddress.ip_network(value) for value in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")
)


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
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, child in pairs:
            if key in value:
                raise ValueError(f"duplicate object key: {key}")
            value[key] = child
        return value

    if path.is_symlink():
        validation.errors.append(f"{path}: symlinked JSON files are forbidden")
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicate_keys)
    except FileNotFoundError:
        validation.errors.append(f"{path}: file not found")
    except json.JSONDecodeError as exc:
        validation.errors.append(f"{path}:{exc.lineno}:{exc.colno}: invalid JSON: {exc.msg}")
    except ValueError as exc:
        validation.errors.append(f"{path}: invalid JSON: {exc}")
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
                normalized = key.lower()
                if normalized in FORBIDDEN_SECRET_KEYS:
                    validation.errors.append(
                        f"{path}.{key}: secret values are forbidden; store only an uppercase secret name"
                    )
                if normalized in FORBIDDEN_INFRASTRUCTURE_KEYS:
                    validation.errors.append(
                        f"{path}.{key}: host-local infrastructure details are forbidden in Git-authored policy"
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


def validate_docker_network_policy(
    policy: Any,
    path: str,
    max_runners: int,
    validation: Validation,
    *,
    strict: bool = False,
) -> tuple[int, int, list[dict[str, Any]]]:
    if not isinstance(policy, dict):
        validation.errors.append(f"{path}: must be an object")
        return 0, 0, []
    required = {"default_address_pools", "networks_per_runner", "reserve_subnets"}
    keys = set(policy)
    missing = sorted(required - keys)
    unknown = sorted(keys - required)
    if missing or unknown:
        parts = []
        if missing:
            parts.append(f"missing keys: {', '.join(missing)}")
        if unknown:
            parts.append(f"unknown keys: {', '.join(unknown)}")
        validation.errors.append(f"{path}: {'; '.join(parts)}")
        return 0, 0, []
    reserve = policy["reserve_subnets"]
    if type(reserve) is not int or reserve < 1:
        validation.errors.append(f"{path}.reserve_subnets: must be a positive integer")
        return 0, 0, []
    networks_per_runner = policy["networks_per_runner"]
    if type(networks_per_runner) is not int or networks_per_runner < 1:
        validation.errors.append(f"{path}.networks_per_runner: must be a positive integer")
        return 0, 0, []
    pools = policy["default_address_pools"]
    if type(pools) is not list or not pools:
        validation.errors.append(f"{path}.default_address_pools: must be a non-empty list")
        return 0, 0, []
    parsed: list[dict[str, Any]] = []
    for index, pool in enumerate(pools):
        pool_path = f"{path}.default_address_pools[{index}]"
        if not isinstance(pool, dict) or set(pool) != {"base", "size"}:
            validation.errors.append(f"{pool_path}: must contain only base and size")
            return 0, 0, []
        base = pool["base"]
        size = pool["size"]
        if not isinstance(base, str):
            validation.errors.append(f"{pool_path}.base: must be a CIDR prefix")
            return 0, 0, []
        if type(size) is not int or size < 0 or size > 29:
            validation.errors.append(f"{pool_path}.size: must be an IPv4 prefix length between 0 and 29")
            return 0, 0, []
        try:
            network = ipaddress.ip_network(base, strict=True)
        except ValueError:
            validation.errors.append(f"{pool_path}.base: malformed address pool IPv4 prefix")
            return 0, 0, []
        if network.version != 4:
            validation.errors.append(f"{pool_path}.base: malformed address pool IPv4 prefix")
            return 0, 0, []
        if strict and any(network.overlaps(documentation) for documentation in RFC_5737_NETWORKS):
            validation.errors.append(
                f"{pool_path}.base: replace the RFC 5737 documentation address pool with a reviewed operational Docker pool CIDR"
            )
            return 0, 0, []
        if size < network.prefixlen:
            validation.errors.append(f"{pool_path}.size: impossible subnet count for {base}")
            return 0, 0, []
        parsed.append({"base": base, "network": network, "size": size})
    for left, item in enumerate(parsed):
        for right in range(left + 1, len(parsed)):
            if item["network"].overlaps(parsed[right]["network"]):
                validation.errors.append(f"{path}.default_address_pools[{left}].base: overlaps configured pool {right}")
                return 0, 0, []
    configured = sum(1 << (item["size"] - item["network"].prefixlen) for item in parsed)
    if configured < max_runners * networks_per_runner + reserve + 1:
        validation.errors.append(
            f"{path}: network capacity cannot satisfy max_runners * networks_per_runner + reserve_subnets + one controller Compose network"
        )
    return configured, reserve, parsed


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


def scan_tree_path_list(path_list: Path, validation: Validation) -> None:
    try:
        raw_paths = path_list.read_bytes().split(b"\0")
    except OSError as error:
        validation.errors.append(f"{path_list}: cannot read tree path list: {error}")
        return
    for raw_path in raw_paths:
        if not raw_path:
            continue
        try:
            relative_text = raw_path.decode("utf-8")
        except UnicodeDecodeError:
            validation.errors.append("repository tree contains a non-UTF-8 path")
            continue
        parts = Path(relative_text).parts
        if any(part.lower() in FORBIDDEN_DIRECTORIES for part in parts[:-1]):
            validation.errors.append(f"{relative_text}: secret-bearing directory names are forbidden")
        elif FORBIDDEN_FILENAMES.search(relative_text):
            validation.errors.append(f"{relative_text}: secret-bearing files are forbidden")


def validate_config(config: Any, validation: Validation, strict: bool) -> None:
    required_top = {
        "schema_version",
        "organization",
        "runner_pools",
        "controllers",
        "host_groups",
        "environments",
        "projects",
    }
    if not validation.exact_keys(config, "$", required_top, {"$schema"}):
        return

    validation.require(config.get("schema_version") == 3, "$.schema_version", "must equal 3")

    organization = config.get("organization")
    organization_keys = {"slug", "registry", "delivery_engine", "workflow_ref_policy"}
    if validation.exact_keys(organization, "$.organization", organization_keys):
        slug = organization.get("slug")
        registry = organization.get("registry")
        engine = organization.get("delivery_engine")
        validation.require(isinstance(slug, str) and bool(ORG_SLUG.fullmatch(slug)), "$.organization.slug", "must be a lowercase GitHub organization slug")
        validation.require(isinstance(registry, str) and bool(IMAGE.fullmatch(registry)), "$.organization.registry", "must be a registry namespace such as ghcr.io/acme")
        validation.require(isinstance(engine, str) and bool(REPOSITORY.fullmatch(engine)), "$.organization.delivery_engine", "must be an owner/repository name")
        validation.require(engine == "RandomDevelopment/ci-fleet", "$.organization.delivery_engine", "must use the fixed reviewed public engine repository")
        validation.require(organization.get("workflow_ref_policy") == "immutable-commit", "$.organization.workflow_ref_policy", "must equal immutable-commit")
        if strict:
            validation.require(slug != "example-org", "$.organization.slug", "replace the example organization before use")

    pools = config.get("runner_pools")
    if not isinstance(pools, dict) or not pools:
        validation.errors.append("$.runner_pools: must be a non-empty object")
        pools = {}
    pool_capacity: dict[str, int] = {}
    runner_groups: dict[str, str] = {}
    for name, pool in pools.items():
        path = f"$.runner_pools.{name}"
        validation.require(isinstance(name, str) and bool(SLUG.fullmatch(name)), path, "pool name must be a lowercase slug")
        pool_keys = {
            "runner_group",
            "routing_labels",
            "allowed_repositories",
            "public_repositories",
            "capacity_budget",
            "job_submission_policy",
        }
        if not validation.exact_keys(pool, path, pool_keys):
            continue
        runner_group = pool.get("runner_group")
        labels = pool.get("routing_labels")
        repos = pool.get("allowed_repositories")
        validation.require(isinstance(runner_group, str) and bool(SLUG.fullmatch(runner_group)), f"{path}.runner_group", "must be a lowercase logical runner-group slug")
        if isinstance(runner_group, str) and SLUG.fullmatch(runner_group):
            if runner_group in runner_groups:
                validation.errors.append(f"{path}.runner_group: must be unique; also used by {runner_groups[runner_group]}")
            else:
                runner_groups[runner_group] = name
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
        budget = pool.get("capacity_budget")
        validation.require(type(budget) is int and budget > 0, f"{path}.capacity_budget", "must be a positive infrastructure capacity budget")
        if type(budget) is int and budget > 0:
            pool_capacity[name] = budget
        validation.require(pool.get("job_submission_policy") == "all-independent-jobs", f"{path}.job_submission_policy", "must submit every independent job and leave capacity control to infrastructure")

    controllers = config.get("controllers")
    if not isinstance(controllers, dict) or not controllers:
        validation.errors.append("$.controllers: must be a non-empty object")
        controllers = {}
    reserved_capacity = {name: 0 for name in pools}
    scale_sets: dict[str, str] = {}
    controller_keys = {
        "pool",
        "location",
        "state",
        "scale_set_name",
        "lifecycle",
        "engine_ref",
        "min_runners",
        "max_runners",
        "runner_resources",
        "docker_network_policy",
    }
    for name, controller in controllers.items():
        path = f"$.controllers.{name}"
        validation.require(isinstance(name, str) and bool(SLUG.fullmatch(name)), path, "controller ID must be a unique lowercase slug")
        if not validation.exact_keys(controller, path, controller_keys - {"docker_network_policy"}, {"status_reporting", "docker_network_policy"}):
            continue
        pool_name = controller.get("pool")
        location = controller.get("location")
        state = controller.get("state")
        scale_set = controller.get("scale_set_name")
        lifecycle = controller.get("lifecycle")
        engine_ref = controller.get("engine_ref")
        minimum = controller.get("min_runners")
        maximum = controller.get("max_runners")
        validation.require(isinstance(pool_name, str) and pool_name in pools, f"{path}.pool", "must reference a declared runner pool")
        validation.require(isinstance(location, str) and bool(SLUG.fullmatch(location)), f"{path}.location", "must be a logical location slug, never an address")
        validation.require(state in {"active", "drained", "disabled"}, f"{path}.state", "must be active, drained, or disabled")
        validation.require(isinstance(scale_set, str) and bool(SLUG.fullmatch(scale_set)), f"{path}.scale_set_name", "must be a lowercase scale-set slug")
        if isinstance(scale_set, str) and isinstance(name, str):
            validation.require(name in scale_set, f"{path}.scale_set_name", "must include the controller ID required by managed preflight")
        if isinstance(scale_set, str):
            if scale_set in scale_sets:
                validation.errors.append(f"{path}.scale_set_name: must be unique; also used by {scale_sets[scale_set]}")
            else:
                scale_sets[scale_set] = name
        validation.require(lifecycle in {"experimental", "stable", "retiring"}, f"{path}.lifecycle", "must be experimental, stable, or retiring")
        validation.require(isinstance(engine_ref, str) and bool(COMMIT_SHA.fullmatch(engine_ref)) and engine_ref != "0" * 40, f"{path}.engine_ref", "must be a nonzero full lowercase commit SHA")
        validation.require(type(minimum) is int and minimum >= 0, f"{path}.min_runners", "must be a non-negative integer")
        validation.require(type(maximum) is int and maximum > 0, f"{path}.max_runners", "must be a positive integer")
        if type(minimum) is int and type(maximum) is int:
            validation.require(minimum <= maximum, f"{path}.min_runners", "must not exceed max_runners")
        validation.require(minimum == 0, f"{path}.min_runners", "must be zero because managed prewarmed runners are not supported")
        status_reporting = controller.get("status_reporting")
        if "status_reporting" in controller and validation.exact_keys(status_reporting, f"{path}.status_reporting", {"enabled", "config_file"}):
            validation.require(type(status_reporting.get("enabled")) is bool, f"{path}.status_reporting.enabled", "must be a boolean")
            validation.require(status_reporting.get("config_file") == "/etc/ci-fleet/monitoring.env", f"{path}.status_reporting.config_file", "must use the fixed host-local monitoring configuration")
        resources = controller.get("runner_resources")
        if validation.exact_keys(resources, f"{path}.runner_resources", {"cpu_cores", "memory_mib"}):
            cpu = resources.get("cpu_cores")
            memory = resources.get("memory_mib")
            validation.require(type(cpu) is int and cpu > 0, f"{path}.runner_resources.cpu_cores", "must be a positive integer")
            validation.require(type(memory) is int and memory >= 512, f"{path}.runner_resources.memory_mib", "must be at least 512 MiB")
        network_policy = controller.get("docker_network_policy")
        capacity_maximum = maximum if state != "disabled" and type(maximum) is int and maximum > 0 else 0
        if "docker_network_policy" in controller:
            validate_docker_network_policy(
                network_policy,
                f"{path}.docker_network_policy",
                capacity_maximum,
                validation,
                strict=strict,
            )
        if isinstance(pool_name, str) and pool_name in pools and state != "disabled" and type(maximum) is int and maximum > 0:
            reserved_capacity[pool_name] += maximum

    for pool_name, pool in pools.items():
        labels = pool.get("routing_labels") if isinstance(pool, dict) else None
        if isinstance(labels, list):
            for index, label in enumerate(labels):
                if isinstance(label, str):
                    validation.require(label not in scale_sets, f"$.runner_pools.{pool_name}.routing_labels[{index}]", "must not equal a controller scale-set name")

    for name, reserved in reserved_capacity.items():
        budget = pool_capacity.get(name)
        if budget is not None:
            validation.require(reserved <= budget, f"$.runner_pools.{name}.capacity_budget", f"must cover {reserved} runners reserved by active or drained controllers")

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
        if not validation.exact_keys(project, path, {"repository", "image", "ci_pool", "ci_contract", "deployments"}):
            continue
        repository = project.get("repository")
        image = project.get("image")
        pool_name = project.get("ci_pool")
        validation.require(isinstance(repository, str) and bool(REPOSITORY.fullmatch(repository)), f"{path}.repository", "must be owner/repository")
        validation.require(isinstance(image, str) and bool(IMAGE.fullmatch(image)), f"{path}.image", "must be a container image path without a mutable tag")
        validation.require(isinstance(pool_name, str) and pool_name in pools, f"{path}.ci_pool", "must reference a declared runner pool")
        if isinstance(pool_name, str) and pool_name in pools and isinstance(pools[pool_name].get("allowed_repositories"), list):
            validation.require(repository in pools[pool_name]["allowed_repositories"], f"{path}.repository", "must be explicitly allowed by its CI pool")
        contract = project.get("ci_contract")
        contract_path = f"{path}.ci_contract"
        contract_keys = {
            "runner_entrypoint",
            "task_plan",
            "aggregate_entrypoints",
            "target_wall_clock_minutes",
            "max_job_minutes",
            "shard_target_minutes",
        }
        if validation.exact_keys(contract, contract_path, contract_keys):
            validation.require(contract.get("runner_entrypoint") == "./scripts/ci/run.sh", f"{contract_path}.runner_entrypoint", "must use the standard task runner")
            validation.require(contract.get("task_plan") == "./scripts/ci/plan.json", f"{contract_path}.task_plan", "must use the standard task-plan path")
            validation.require(contract.get("target_wall_clock_minutes") == 5, f"{contract_path}.target_wall_clock_minutes", "must equal the five-minute fleet goal")
            validation.require(contract.get("max_job_minutes") == 5, f"{contract_path}.max_job_minutes", "must enforce a five-minute hard job ceiling")
            shard_target = contract.get("shard_target_minutes")
            validation.require(type(shard_target) is int and 1 <= shard_target <= 4, f"{contract_path}.shard_target_minutes", "must be between one and four minutes to reserve startup time")
            entrypoints = contract.get("aggregate_entrypoints")
            aggregate_path = f"{contract_path}.aggregate_entrypoints"
            if validation.exact_keys(entrypoints, aggregate_path, {"fast", "full"}):
                validation.require(entrypoints.get("fast") == "./scripts/ci/run.sh fast", f"{aggregate_path}.fast", "must use the standard aggregate fast entrypoint")
                validation.require(entrypoints.get("full") == "./scripts/ci/run.sh full", f"{aggregate_path}.full", "must use the standard aggregate full entrypoint")
        deployments = project.get("deployments")
        validation.require(isinstance(deployments, list) and bool(deployments), f"{path}.deployments", "must be a non-empty list")
        if isinstance(deployments, list):
            validation.require(len(deployments) == len(set(deployments)), f"{path}.deployments", "must contain unique environments")
            for index, deployment in enumerate(deployments):
                validation.require(deployment in environments, f"{path}.deployments[{index}]", "must reference a declared environment")
        if strict:
            validation.require(repository != "example-org/example-app", f"{path}.repository", "replace the example repository before use")


def validate_rollout_evidence(value: Any, validation: Validation) -> dict[str, dict[str, Any]]:
    if not validation.exact_keys(
        value,
        "engine-rollout-evidence.json",
        {"schema_version", "status_reporting_engine_capabilities"},
    ):
        return {}
    validation.require(value.get("schema_version") == 1, "engine-rollout-evidence.json.schema_version", "must equal 1")
    capabilities = value.get("status_reporting_engine_capabilities")
    if not isinstance(capabilities, dict):
        validation.errors.append("engine-rollout-evidence.json.status_reporting_engine_capabilities: must be an object mapping controller IDs to capability evidence")
        return {}
    valid: dict[str, dict[str, Any]] = {}
    for controller, evidence in capabilities.items():
        path = f"engine-rollout-evidence.json.status_reporting_engine_capabilities.{controller}"
        controller_valid = bool(SLUG.fullmatch(controller))
        validation.require(controller_valid, path, "controller ID must be a lowercase slug")
        if not validation.exact_keys(
            evidence,
            path,
            {"engine_ref", "status_reporting_config", "required_status_reporting"},
            {"docker_network_policy_config"},
        ):
            continue
        ref = evidence.get("engine_ref")
        configured = evidence.get("status_reporting_config")
        required = evidence.get("required_status_reporting")
        network_policy = evidence.get("docker_network_policy_config")
        ref_valid = isinstance(ref, str) and bool(COMMIT_SHA.fullmatch(ref)) and ref != "0" * 40
        validation.require(ref_valid, f"{path}.engine_ref", "must be a nonzero full lowercase commit SHA")
        validation.require(type(configured) is bool, f"{path}.status_reporting_config", "must be a boolean")
        validation.require(type(required) is bool, f"{path}.required_status_reporting", "must be a boolean")
        if "docker_network_policy_config" in evidence:
            validation.require(type(network_policy) is bool, f"{path}.docker_network_policy_config", "must be a boolean")
        network_policy_valid = "docker_network_policy_config" not in evidence or type(network_policy) is bool
        if controller_valid and ref_valid and type(configured) is bool and type(required) is bool and network_policy_valid:
            valid[controller] = {
                "engine_ref": ref,
                "status_reporting_config": configured,
                "required_status_reporting": required,
            }
            if "docker_network_policy_config" in evidence:
                valid[controller]["docker_network_policy_config"] = network_policy
    return valid


def validate_reporting_evidence(
    name: str,
    controller: dict[str, Any],
    evidence: dict[str, Any],
    validation: Validation,
) -> None:
    reporting = controller.get("status_reporting")
    if not isinstance(reporting, dict):
        return
    validation.require(
        evidence.get("engine_ref") == controller.get("engine_ref")
        and evidence.get("status_reporting_config") is True,
        f"$.controllers.{name}.status_reporting",
        "requires status-reporting configuration capability evidence for this controller and engine_ref",
    )
    if reporting.get("enabled") is True:
        validation.require(
            evidence.get("engine_ref") == controller.get("engine_ref")
            and evidence.get("required_status_reporting") is True,
            f"$.controllers.{name}.status_reporting.enabled",
            "requires required status-reporting rollout evidence for this controller and engine_ref",
        )


def validate_transition(
    previous: Any,
    current: Any,
    compatible_engine_refs: dict[str, dict[str, Any]],
    validation: Validation,
    previous_compatible_engine_refs: dict[str, dict[str, Any]] | None = None,
) -> None:
    if not isinstance(previous, dict) or not isinstance(current, dict):
        return
    old_controllers = previous.get("controllers")
    new_controllers = current.get("controllers")
    if not isinstance(old_controllers, dict) or not isinstance(new_controllers, dict):
        return
    for name, new in new_controllers.items():
        old = old_controllers.get(name)
        if not isinstance(new, dict):
            continue
        if name not in old_controllers:
            if "status_reporting" in new:
                validation.errors.append(
                    f"$.controllers.{name}.status_reporting: must be omitted from a new controller until its engine rollout is proven"
                )
            continue
        if not isinstance(old, dict):
            continue
        current_evidence = compatible_engine_refs.get(name, {})
        previous_evidence_source = (
            compatible_engine_refs
            if previous_compatible_engine_refs is None
            else previous_compatible_engine_refs
        )
        previous_evidence = previous_evidence_source.get(name, {})
        old_reporting = old.get("status_reporting")
        new_reporting = new.get("status_reporting")
        if "docker_network_policy" not in old and "docker_network_policy" in new:
            validation.require(
                old.get("engine_ref") == new.get("engine_ref"),
                f"$.controllers.{name}.docker_network_policy",
                "must be introduced in a later commit after the compatible engine_ref is active",
            )
            validation.require(
                previous_evidence.get("engine_ref") == new.get("engine_ref")
                and previous_evidence.get("docker_network_policy_config") is True,
                f"$.controllers.{name}.docker_network_policy",
                "requires reviewed evidence from the previous integrated state that this controller activated the same engine_ref with Docker network policy configuration capability",
            )
        if "docker_network_policy" in new:
            validation.require(
                current_evidence.get("engine_ref") == new.get("engine_ref")
                and current_evidence.get("docker_network_policy_config") is True,
                f"$.controllers.{name}.docker_network_policy",
                "requires Docker network policy configuration capability evidence for this controller and engine_ref",
            )
        staged_capability_required = (
            "status_reporting" not in old
            or (
                isinstance(new_reporting, dict)
                and new_reporting.get("enabled") is True
                and (not isinstance(old_reporting, dict) or old_reporting.get("enabled") is not True)
            )
        )
        evidence = previous_evidence if staged_capability_required else current_evidence
        validate_reporting_evidence(name, new, evidence, validation)
        if "status_reporting" not in old and "status_reporting" in new:
            validation.require(
                old.get("engine_ref") == new.get("engine_ref"),
                f"$.controllers.{name}.status_reporting",
                "must be introduced in a later commit after the compatible engine_ref is active",
            )
            validation.require(
                evidence.get("engine_ref") == old.get("engine_ref"),
                f"$.controllers.{name}.status_reporting",
                "requires reviewed rollout evidence for this controller and its already-active compatible engine_ref",
            )
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "fleet.json", help="configuration file to validate")
    parser.add_argument("--previous-config", type=Path, help="previous integrated configuration for rollout validation")
    parser.add_argument("--rollout-evidence", type=Path, help="rollout evidence file (defaults to engine-rollout-evidence.json only for the default fleet.json)")
    parser.add_argument("--previous-rollout-evidence", type=Path, help="previous integrated rollout evidence")
    parser.add_argument("--strict", action="store_true", help="reject unchanged example values")
    parser.add_argument("--skip-path-scan", action="store_true", help="skip repository path checks (for external fixtures)")
    parser.add_argument("--tree-paths", type=Path, help="NUL-delimited committed paths to scan instead of the local template tree")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    validation = Validation()
    config_path = args.config.absolute()
    config = load_json(config_path, validation)
    evidence_path = args.rollout_evidence.absolute() if args.rollout_evidence else None
    if evidence_path is None and config_path == ROOT / "fleet.json":
        evidence_path = ROOT / "engine-rollout-evidence.json"
    evidence = load_json(evidence_path, validation) if evidence_path is not None else None
    current_compatible_engine_refs = (
        validate_rollout_evidence(evidence, validation)
        if evidence is not None
        else {}
    )
    schema = load_json(ROOT / "fleet.schema.json", validation)
    if schema is not None:
        validation.require(schema.get("$schema") == "https://json-schema.org/draft/2020-12/schema", "fleet.schema.json.$schema", "must use JSON Schema draft 2020-12")
    if config is not None:
        scan_secret_material(config, validation)
        validate_config(config, validation, args.strict)
        current_controllers = config.get("controllers", {}) if isinstance(config, dict) else {}
        for controller, evidence in current_compatible_engine_refs.items():
            current_controller = current_controllers.get(controller) if isinstance(current_controllers, dict) else None
            validation.require(
                isinstance(current_controller, dict) and current_controller.get("engine_ref") == evidence["engine_ref"],
                f"engine-rollout-evidence.json.status_reporting_engine_capabilities.{controller}.engine_ref",
                "must match the current controller engine_ref; remove stale evidence before changing or removing the controller",
            )
        if isinstance(current_controllers, dict):
            for controller, value in current_controllers.items():
                if isinstance(value, dict):
                    validate_reporting_evidence(
                        controller,
                        value,
                        current_compatible_engine_refs.get(controller, {}),
                        validation,
                    )
        if args.previous_config is not None:
            previous = load_json(args.previous_config.absolute(), validation)
            previous_evidence = (
                load_json(args.previous_rollout_evidence.absolute(), validation)
                if args.previous_rollout_evidence
                else None
            )
            previous_compatible_engine_refs = (
                validate_rollout_evidence(previous_evidence, validation)
                if previous_evidence is not None
                else {}
            )
            if previous is not None:
                previous_controllers = previous.get("controllers", {}) if isinstance(previous, dict) else {}
                for controller, evidence in current_compatible_engine_refs.items():
                    if previous_compatible_engine_refs.get(controller) == evidence:
                        continue
                    ref = evidence["engine_ref"]
                    previous_controller = previous_controllers.get(controller) if isinstance(previous_controllers, dict) else None
                    validation.require(
                        isinstance(previous_controller, dict) and previous_controller.get("engine_ref") == ref,
                        f"engine-rollout-evidence.json.status_reporting_engine_capabilities.{controller}.engine_ref",
                        f"{ref} must already be selected for this controller in the previous integrated fleet configuration",
                    )
                validate_transition(
                    previous,
                    config,
                    current_compatible_engine_refs,
                    validation,
                    previous_compatible_engine_refs,
                )
    if args.tree_paths is not None:
        scan_tree_path_list(args.tree_paths, validation)
    elif not args.skip_path_scan:
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
