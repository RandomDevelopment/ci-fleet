#!/usr/bin/env python3
"""Validate, select, and render schema-v3 ci-fleet controller desired state."""

from __future__ import annotations

import argparse
import importlib.util
import ipaddress
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_VALIDATOR = ROOT / "templates" / "config-repository" / "scripts" / "validate.py"
COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9._-]+$")
SAFE_DURATION = re.compile(r"^[1-9][0-9]*(?:s|m|h)$")
SAFE_ABSOLUTE_PATH = re.compile(r"^/[A-Za-z0-9._/-]+$")
SAFE_ENV_VALUE = re.compile(r"^[A-Za-z0-9._/:,-]+$")
HOST_REQUIRED = {
    "CI_FLEET_GITHUB_APP_CLIENT_ID",
    "CI_FLEET_GITHUB_APP_INSTALLATION_ID",
    "CI_FLEET_GITHUB_APP_PRIVATE_KEY_FILE",
}
HOST_OPTIONAL = {"CI_FLEET_RUNNER_TTL"}
RENDERED_ENV_NAMES = HOST_REQUIRED | HOST_OPTIONAL | {
    "CI_FLEET_CAPACITY_BUDGET",
    "CI_FLEET_COMMIT",
    "CI_FLEET_CONFIGURED_MAX_RUNNERS",
    "CI_FLEET_CONFIG_REF",
    "CI_FLEET_CONFIG_REPOSITORY",
    "CI_FLEET_CONTROLLER_IMAGE",
    "CI_FLEET_CONTROLLER_STATE",
    "CI_FLEET_DESIRED_STATE_SCHEMA",
    "CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_COUNT",
    "CI_FLEET_DOCKER_GID",
    "CI_FLEET_DOCKER_NETWORKS_PER_RUNNER",
    "CI_FLEET_DOCKER_NETWORK_RESERVE_SUBNETS",
    "CI_FLEET_ENGINE_REF",
    "CI_FLEET_GITHUB_URL",
    "CI_FLEET_INSTANCE",
    "CI_FLEET_LABELS",
    "CI_FLEET_MAX_RUNNERS",
    "CI_FLEET_MIN_RUNNERS",
    "CI_FLEET_RUNNER_CPUS",
    "CI_FLEET_RUNNER_GROUP",
    "CI_FLEET_RUNNER_IMAGE",
    "CI_FLEET_RUNNER_MEMORY_MIB",
    "CI_FLEET_SCALE_SET_NAME",
    "CI_FLEET_STATUS_REPORTING_REQUIRED",
    "CI_FLEET_VERSION",
}
REQUIRED_STATUS_CAPABILITY = "required_status_reporting"
STATUS_REPORTING_CONFIG_CAPABILITY = "status_reporting_config"
DOCKER_NETWORK_POLICY_CONFIG_CAPABILITY = "docker_network_policy_config"
MAX_DOCKER_ADDRESS_POOLS = 64


class DesiredStateError(ValueError):
    """A safe operator-facing desired-state error."""


def load_engine_capabilities(path: Path) -> set[str]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for name, item in pairs:
            if name in value:
                raise ValueError("duplicate capability key")
            value[name] = item
        return value

    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise DesiredStateError("engine capability declaration must be a regular file")
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicates)
    except FileNotFoundError as exc:
        raise DesiredStateError("engine capability declaration is missing") from exc
    except (json.JSONDecodeError, ValueError) as exc:
        raise DesiredStateError("engine capability declaration is malformed") from exc
    if not isinstance(value, dict) or set(value) != {"schema_version", "capabilities"} or value.get("schema_version") != 1:
        raise DesiredStateError("engine capability declaration is malformed")
    capabilities = value.get("capabilities")
    if not isinstance(capabilities, dict) or any(type(supported) is not bool for supported in capabilities.values()):
        raise DesiredStateError("engine capability declaration is malformed")
    return {name for name, supported in capabilities.items() if supported}


def load_template_validator():
    spec = importlib.util.spec_from_file_location("ci_fleet_template_validator", TEMPLATE_VALIDATOR)
    if spec is None or spec.loader is None:
        raise DesiredStateError("public configuration validator could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_and_validate_config(path: Path) -> dict[str, Any]:
    module = load_template_validator()
    validation = module.Validation()
    config = module.load_json(path, validation)
    if config is not None:
        module.scan_secret_material(config, validation)
        module.validate_config(config, validation, False)
    if validation.errors:
        raise DesiredStateError("configuration rejected:\n" + "\n".join(f"- {error}" for error in validation.errors))
    if not isinstance(config, dict) or config.get("schema_version") != 3:
        raise DesiredStateError("configuration must use schema_version 3")
    return config


def parse_env(path: Path, *, allow_unknown: bool) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise DesiredStateError(f"host configuration does not exist: {path}") from exc
    values: dict[str, str] = {}
    for number, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise DesiredStateError(f"{path}:{number}: expected NAME=value")
        name, value = line.split("=", 1)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
            raise DesiredStateError(f"{path}:{number}: invalid variable name")
        if name in values:
            raise DesiredStateError(f"{path}:{number}: duplicate variable {name}")
        if any(character in value for character in "\r\n\0"):
            raise DesiredStateError(f"{path}:{number}: multiline values are forbidden")
        if not allow_unknown and name not in HOST_REQUIRED | HOST_OPTIONAL:
            raise DesiredStateError(f"{path}:{number}: unsupported host-local variable {name}")
        values[name] = value
    return values


def rendered_env_names(values: dict[str, str]) -> set[str]:
    names = set(RENDERED_ENV_NAMES)
    try:
        count = int(values.get("CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_COUNT", "0"))
    except ValueError:
        count = 0
    if 0 < count <= MAX_DOCKER_ADDRESS_POOLS:
        names.update(
            f"CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_{index}_{field}"
            for index in range(count)
            for field in ("BASE", "SIZE")
        )
    return names


def validate_host_values(values: dict[str, str]) -> dict[str, str]:
    missing = sorted(HOST_REQUIRED - values.keys())
    if missing:
        raise DesiredStateError("host configuration is missing: " + ", ".join(missing))
    client_id = values["CI_FLEET_GITHUB_APP_CLIENT_ID"]
    installation_id = values["CI_FLEET_GITHUB_APP_INSTALLATION_ID"]
    key_file = values["CI_FLEET_GITHUB_APP_PRIVATE_KEY_FILE"]
    if not SAFE_IDENTIFIER.fullmatch(client_id):
        raise DesiredStateError("GitHub App client ID contains unsupported characters")
    if not installation_id.isdigit() or int(installation_id) < 1:
        raise DesiredStateError("GitHub App installation ID must be a positive integer")
    if not SAFE_ABSOLUTE_PATH.fullmatch(key_file):
        raise DesiredStateError("GitHub App private-key path must be an absolute shell-safe path")
    ttl = values.get("CI_FLEET_RUNNER_TTL", "6h")
    if not SAFE_DURATION.fullmatch(ttl):
        raise DesiredStateError("runner TTL must be a positive duration ending in s, m, or h")
    multiplier = {"s": 1, "m": 60, "h": 3600}[ttl[-1]]
    if int(ttl[:-1]) * multiplier < 3600:
        raise DesiredStateError("runner TTL must be at least one hour")
    return {
        "CI_FLEET_GITHUB_APP_CLIENT_ID": client_id,
        "CI_FLEET_GITHUB_APP_INSTALLATION_ID": installation_id,
        "CI_FLEET_GITHUB_APP_PRIVATE_KEY_FILE": key_file,
        "CI_FLEET_RUNNER_TTL": ttl,
    }


def validate_docker_address_pools(pools: Any, *, path: str) -> list[dict[str, Any]]:
    if type(pools) is not list or not pools:
        raise DesiredStateError(f"{path}: must be a non-empty list")
    if len(pools) > MAX_DOCKER_ADDRESS_POOLS:
        raise DesiredStateError(f"{path}: must not exceed {MAX_DOCKER_ADDRESS_POOLS} pools")
    parsed: list[dict[str, Any]] = []
    for index, pool in enumerate(pools):
        pool_path = f"{path}[{index}]"
        if not isinstance(pool, dict) or set(pool) != {"base", "size"}:
            raise DesiredStateError(f"{pool_path}: must contain only base and size")
        base = pool.get("base")
        size = pool.get("size")
        if not isinstance(base, str):
            raise DesiredStateError(f"{pool_path}.base: must be a CIDR prefix")
        if type(size) is not int or size < 0 or size > 29:
            raise DesiredStateError(f"{pool_path}.size: must be an IPv4 prefix length between 0 and 29")
        try:
            network = ipaddress.ip_network(base, strict=True)
        except ValueError as exc:
            raise DesiredStateError(f"{pool_path}.base: malformed IPv4 prefix") from exc
        if network.version != 4:
            raise DesiredStateError(f"{pool_path}.base: malformed IPv4 prefix")
        if size < network.prefixlen:
            raise DesiredStateError(f"{pool_path}.size: impossible subnet count for {base}")
        parsed.append({"base": base, "network": network, "size": size})
    for left, item in enumerate(parsed):
        for right in range(left + 1, len(parsed)):
            other = parsed[right]
            if item["network"].overlaps(other["network"]):
                raise DesiredStateError(f"{path}[{left}].base: overlaps configured pool {right}")
    return parsed


def validate_docker_network_policy(policy: dict[str, Any], *, path: str, max_runners: int) -> tuple[int, int, int, list[dict[str, Any]]]:
    if not isinstance(policy, dict):
        raise DesiredStateError(f"{path}: must be an object")
    required = {"default_address_pools", "networks_per_runner", "reserve_subnets"}
    if set(policy) != required:
        unknown = sorted(set(policy) - required)
        missing = sorted(required - set(policy))
        messages: list[str] = []
        if missing:
            messages.append(f"missing keys: {', '.join(missing)}")
        if unknown:
            messages.append(f"unknown keys: {', '.join(unknown)}")
        raise DesiredStateError(f"{path}: " + "; ".join(messages))
    reserve = policy.get("reserve_subnets")
    if type(reserve) is not int or reserve < 1:
        raise DesiredStateError(f"{path}.reserve_subnets: must be a positive integer")
    networks_per_runner = policy.get("networks_per_runner")
    if type(networks_per_runner) is not int or networks_per_runner < 1:
        raise DesiredStateError(f"{path}.networks_per_runner: must be a positive integer")
    parsed = validate_docker_address_pools(policy.get("default_address_pools"), path=f"{path}.default_address_pools")
    configured = sum(1 << (item["size"] - item["network"].prefixlen) for item in parsed)
    if configured < max_runners * networks_per_runner + reserve + 1:
        raise DesiredStateError(
            f"{path}: network capacity cannot satisfy max_runners * networks_per_runner + reserve_subnets + one controller Compose network"
        )
    return configured, reserve, networks_per_runner, parsed


def render_docker_daemon_config(rendered: dict[str, str]) -> dict[str, Any]:
    """Build the Docker daemon.json ``default-address-pools`` block from rendered env.

    Reads only the already-validated ``CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_*``
    values produced by ``build_rendered_env``. Returns a dict suitable for
    merging into ``daemon.json``. When no policy was rendered, returns an empty
    dict (no ``default-address-pools`` key).
    """
    count_name = "CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_COUNT"
    pool_prefix = "CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_"
    policy_configured = count_name in rendered
    if not policy_configured:
        if any(
            name.startswith(pool_prefix)
            or name in {"CI_FLEET_DOCKER_NETWORKS_PER_RUNNER", "CI_FLEET_DOCKER_NETWORK_RESERVE_SUBNETS"}
            for name in rendered
        ):
            raise ValueError("rendered Docker network policy fields must include the pool count")
        if not rendered:
            return {}
    count_str = rendered.get(count_name, "0")
    try:
        count = int(count_str)
    except ValueError as exc:
        raise ValueError(f"CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_COUNT: must be an integer, got {count_str!r}") from exc
    if count < 0:
        raise ValueError("CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_COUNT: must be non-negative")
    if count > MAX_DOCKER_ADDRESS_POOLS:
        raise ValueError(f"CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_COUNT: must not exceed {MAX_DOCKER_ADDRESS_POOLS}")
    actual_pool_fields = {name for name in rendered if name.startswith(pool_prefix) and name != f"{pool_prefix}COUNT"}
    if len(actual_pool_fields) != count * 2:
        raise ValueError("rendered Docker address-pool indexed fields must match the declared count")
    expected_pool_fields = {
        f"{pool_prefix}{index}_{field}"
        for index in range(count)
        for field in ("BASE", "SIZE")
    }
    if actual_pool_fields != expected_pool_fields:
        raise ValueError("rendered Docker address-pool indexed fields must match the declared count")
    pools: list[dict[str, Any]] = []
    for index in range(count):
        base = rendered.get(f"CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_{index}_BASE")
        size_str = rendered.get(f"CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_{index}_SIZE")
        if base is None or size_str is None:
            raise ValueError(f"CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_{index}_BASE/SIZE: both required when count > 0")
        try:
            network = ipaddress.ip_network(base, strict=True)
        except ValueError as exc:
            raise ValueError(f"CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_{index}_BASE: malformed CIDR") from exc
        if network.version != 4:
            raise ValueError(f"CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_{index}_BASE: must be IPv4")
        try:
            size = int(size_str)
        except ValueError as exc:
            raise ValueError(f"CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_{index}_SIZE: must be an integer, got {size_str!r}") from exc
        if not isinstance(size, int) or size < 0 or size > 29:
            raise ValueError(f"CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_{index}_SIZE: must be between 0 and 29")
        if size < network.prefixlen:
            raise ValueError(f"CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_{index}_SIZE: impossible subnet count")
        pools.append({"base": base, "size": size})
    state = rendered.get("CI_FLEET_CONTROLLER_STATE")
    if state not in {"active", "drained", "disabled"}:
        raise ValueError("CI_FLEET_CONTROLLER_STATE: must be active, drained, or disabled")
    try:
        configured_max_runners = int(rendered["CI_FLEET_CONFIGURED_MAX_RUNNERS"])
        effective_max_runners = int(rendered["CI_FLEET_MAX_RUNNERS"])
        minimum_runners = int(rendered["CI_FLEET_MIN_RUNNERS"])
        capacity_budget = int(rendered["CI_FLEET_CAPACITY_BUDGET"])
    except (KeyError, ValueError) as exc:
        raise ValueError("rendered controller capacity fields must be present integers") from exc
    if minimum_runners != 0:
        raise ValueError("CI_FLEET_MIN_RUNNERS: must be zero")
    if capacity_budget < 1:
        raise ValueError("CI_FLEET_CAPACITY_BUDGET: must be a positive integer")
    if configured_max_runners < 1:
        raise ValueError("CI_FLEET_CONFIGURED_MAX_RUNNERS: must be a positive integer")
    if state != "disabled" and configured_max_runners > capacity_budget:
        raise ValueError("CI_FLEET_CONFIGURED_MAX_RUNNERS: must not exceed CI_FLEET_CAPACITY_BUDGET")
    expected_effective_max = configured_max_runners if state == "active" else 0
    if effective_max_runners != expected_effective_max:
        raise ValueError("CI_FLEET_MAX_RUNNERS: must match effective controller capacity")
    if not policy_configured:
        return {}
    try:
        policy = {
            "default_address_pools": pools,
            "networks_per_runner": int(rendered["CI_FLEET_DOCKER_NETWORKS_PER_RUNNER"]),
            "reserve_subnets": int(rendered["CI_FLEET_DOCKER_NETWORK_RESERVE_SUBNETS"]),
        }
    except (KeyError, ValueError) as exc:
        raise ValueError("rendered Docker network policy fields must be present integers") from exc
    max_runners = 0 if state == "disabled" else configured_max_runners
    validate_docker_network_policy(policy, path="rendered Docker network policy", max_runners=max_runners)
    return {"default-address-pools": pools}


def select_controller(config: dict[str, Any], controller_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    controllers = config["controllers"]
    if controller_id not in controllers:
        available = ", ".join(sorted(controllers))
        raise DesiredStateError(f"controller {controller_id!r} is not declared; available: {available}")
    controller = controllers[controller_id]
    pool = config["runner_pools"][controller["pool"]]
    return controller, pool


def build_rendered_env(
    config: dict[str, Any],
    controller_id: str,
    host_values: dict[str, str],
    *,
    config_repository: str,
    config_ref: str,
    docker_gid: int,
    engine_capabilities: set[str] | None = None,
) -> tuple[dict[str, str], dict[str, Any]]:
    controller, pool = select_controller(config, controller_id)
    engine_commit = controller["engine_ref"]
    if not COMMIT_SHA.fullmatch(config_ref):
        raise DesiredStateError("configuration ref must be a full lowercase commit SHA")
    if docker_gid < 0:
        raise DesiredStateError("Docker socket GID must be a non-negative integer")
    if not SAFE_ENV_VALUE.fullmatch(config_repository):
        raise DesiredStateError("configuration repository identity must be shell-safe")

    state = controller["state"]
    configured_max = controller["max_runners"]
    effective_max = configured_max if state == "active" else 0
    network_policy_configured = "docker_network_policy" in controller
    network_policy = controller.get("docker_network_policy")
    configured_subnets, reserve_subnets, networks_per_runner, parsed_pools = (0, 0, 0, [])
    if network_policy_configured:
        configured_subnets, reserve_subnets, networks_per_runner, parsed_pools = validate_docker_network_policy(
            network_policy,
            path=f"$.controllers.{controller_id}.docker_network_policy",
            max_runners=configured_max if state != "disabled" else 0,
        )
        if DOCKER_NETWORK_POLICY_CONFIG_CAPABILITY not in (engine_capabilities or set()):
            raise DesiredStateError("selected engine does not support Docker network policy configuration")
    short_commit = engine_commit[:12]
    rendered = {
        "CI_FLEET_CAPACITY_BUDGET": str(pool["capacity_budget"]),
        "CI_FLEET_COMMIT": engine_commit,
        "CI_FLEET_CONFIGURED_MAX_RUNNERS": str(configured_max),
        "CI_FLEET_CONFIG_REF": config_ref,
        "CI_FLEET_CONFIG_REPOSITORY": config_repository,
        "CI_FLEET_CONTROLLER_IMAGE": f"ci-fleet-controller:{short_commit}",
        "CI_FLEET_CONTROLLER_STATE": state,
        "CI_FLEET_DESIRED_STATE_SCHEMA": "3",
        "CI_FLEET_DOCKER_GID": str(docker_gid),
        "CI_FLEET_ENGINE_REF": engine_commit,
        "CI_FLEET_GITHUB_URL": f"https://github.com/{config['organization']['slug']}",
        "CI_FLEET_INSTANCE": controller_id,
        "CI_FLEET_LABELS": ",".join(pool["routing_labels"]),
        "CI_FLEET_MAX_RUNNERS": str(effective_max),
        "CI_FLEET_MIN_RUNNERS": str(controller["min_runners"] if state == "active" else 0),
        "CI_FLEET_RUNNER_CPUS": str(controller["runner_resources"]["cpu_cores"]),
        "CI_FLEET_RUNNER_GROUP": pool["runner_group"],
        "CI_FLEET_RUNNER_IMAGE": f"ci-fleet-runner:{short_commit}",
        "CI_FLEET_RUNNER_MEMORY_MIB": str(controller["runner_resources"]["memory_mib"]),
        "CI_FLEET_SCALE_SET_NAME": controller["scale_set_name"],
        "CI_FLEET_VERSION": short_commit,
        **validate_host_values(host_values),
    }
    if network_policy_configured:
        rendered["CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_COUNT"] = str(len(parsed_pools))
        rendered["CI_FLEET_DOCKER_NETWORKS_PER_RUNNER"] = str(networks_per_runner)
        rendered["CI_FLEET_DOCKER_NETWORK_RESERVE_SUBNETS"] = str(reserve_subnets)
        for index, pool_config in enumerate(parsed_pools):
            rendered[f"CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_{index}_BASE"] = pool_config["base"]
            rendered[f"CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_{index}_SIZE"] = str(pool_config["size"])
    reporting_configured = "status_reporting" in controller
    reporting_required = (controller.get("status_reporting") or {}).get("enabled") is True
    if reporting_required and REQUIRED_STATUS_CAPABILITY not in (engine_capabilities or set()):
        raise DesiredStateError("selected engine does not advertise required status reporting")
    if reporting_configured and STATUS_REPORTING_CONFIG_CAPABILITY not in (engine_capabilities or set()):
        raise DesiredStateError("selected engine does not support status reporting configuration")
    if reporting_required:
        rendered["CI_FLEET_STATUS_REPORTING_REQUIRED"] = "1"
    if set(rendered) - rendered_env_names(rendered):
        raise DesiredStateError("renderer produced unsupported environment fields")
    for name, value in rendered.items():
        if not SAFE_ENV_VALUE.fullmatch(value):
            raise DesiredStateError(f"rendered value for {name} contains unsafe characters")
    metadata = {
        "schema_version": 1,
        "controller": controller_id,
        "controller_state": state,
        "pool": controller["pool"],
        "location": controller["location"],
        "lifecycle": controller["lifecycle"],
        "scale_set_name": controller["scale_set_name"],
        "configured_max_runners": configured_max,
        "effective_max_runners": effective_max,
        "capacity_budget": pool["capacity_budget"],
        "config_repository": config_repository,
        "config_ref": config_ref,
        "engine_ref": engine_commit,
        "engine_repository": config["organization"]["delivery_engine"],
        "status_reporting_configured": reporting_configured,
        "status_reporting_required": reporting_required,
        "docker_network_policy_configured": network_policy_configured,
        "docker_network_default_address_pools": len(parsed_pools),
        "docker_networks_per_runner": networks_per_runner,
        "docker_network_reserve_subnets": reserve_subnets,
        "docker_network_configured_subnets": configured_subnets,
    }
    return rendered, metadata


def write_private(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def render_env(values: dict[str, str]) -> str:
    return "".join(f"{name}={values[name]}\n" for name in sorted(values))


def command_validate(args: argparse.Namespace) -> None:
    config = load_and_validate_config(args.config)
    print(f"DESIRED_STATE_OK schema={config['schema_version']} controllers={len(config['controllers'])}")


def command_extract_host(args: argparse.Namespace) -> None:
    values = parse_env(args.source, allow_unknown=True)
    selected = validate_host_values({name: value for name, value in values.items() if name in HOST_REQUIRED | HOST_OPTIONAL})
    write_private(args.output, render_env(selected))
    print(f"HOST_CONFIG_WRITTEN path={args.output}")


def command_render(args: argparse.Namespace) -> None:
    config = load_and_validate_config(args.config)
    host_values = parse_env(args.host_config, allow_unknown=False)
    capabilities = load_engine_capabilities(args.engine_capabilities) if args.engine_capabilities else set()
    values, metadata = build_rendered_env(
        config,
        args.controller,
        host_values,
        config_repository=args.config_repository,
        config_ref=args.config_ref,
        docker_gid=args.docker_gid,
        engine_capabilities=capabilities,
    )
    write_private(args.output, render_env(values))
    write_private(args.metadata_output, json.dumps(metadata, indent=2, sort_keys=True) + "\n")
    print(
        "DESIRED_STATE_RENDERED "
        f"controller={metadata['controller']} state={metadata['controller_state']} "
        f"config_ref={metadata['config_ref']} engine_ref={metadata['engine_ref']}"
    )


def command_engine(args: argparse.Namespace) -> None:
    config = load_and_validate_config(args.config)
    controller, _ = select_controller(config, args.controller)
    print(controller["engine_ref"])
    print(config["organization"]["delivery_engine"])


def command_validate_engine_capabilities(args: argparse.Namespace) -> None:
    capabilities = load_engine_capabilities(args.manifest)
    if args.require_docker_network_policy_config and DOCKER_NETWORK_POLICY_CONFIG_CAPABILITY not in capabilities:
        raise DesiredStateError("selected engine does not support Docker network policy configuration")
    if args.require_status_reporting_config and STATUS_REPORTING_CONFIG_CAPABILITY not in capabilities:
        raise DesiredStateError("selected engine does not support status reporting configuration")
    if args.require_status_reporting and REQUIRED_STATUS_CAPABILITY not in capabilities:
        raise DesiredStateError("selected engine does not advertise required status reporting")
    print("ENGINE_CAPABILITIES_OK")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="validate a complete schema-v3 configuration")
    validate.add_argument("--config", type=Path, required=True)
    validate.set_defaults(function=command_validate)

    extract = subparsers.add_parser("extract-host-env", help="extract approved host-local values during adoption")
    extract.add_argument("--source", type=Path, required=True)
    extract.add_argument("--output", type=Path, required=True)
    extract.set_defaults(function=command_extract_host)

    render = subparsers.add_parser("render", help="render one controller into a host-local runtime environment")
    render.add_argument("--config", type=Path, required=True)
    render.add_argument("--controller", required=True)
    render.add_argument("--host-config", type=Path, required=True)
    render.add_argument("--config-repository", required=True)
    render.add_argument("--config-ref", required=True)
    render.add_argument("--docker-gid", type=int, required=True)
    render.add_argument("--engine-capabilities", type=Path)
    render.add_argument("--output", type=Path, required=True)
    render.add_argument("--metadata-output", type=Path, required=True)
    render.set_defaults(function=command_render)

    engine = subparsers.add_parser("engine", help="select the immutable engine for one controller")
    engine.add_argument("--config", type=Path, required=True)
    engine.add_argument("--controller", required=True)
    engine.set_defaults(function=command_engine)

    capabilities = subparsers.add_parser("validate-engine-capabilities", help="validate an engine capability declaration")
    capabilities.add_argument("--manifest", type=Path, required=True)
    capabilities.add_argument("--require-docker-network-policy-config", action="store_true")
    capabilities.add_argument("--require-status-reporting-config", action="store_true")
    capabilities.add_argument("--require-status-reporting", action="store_true")
    capabilities.set_defaults(function=command_validate_engine_capabilities)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        args.function(args)
    except DesiredStateError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
