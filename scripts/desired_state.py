#!/usr/bin/env python3
"""Validate, select, and render schema-v3 ci-fleet controller desired state."""

from __future__ import annotations

import argparse
import importlib.util
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


class DesiredStateError(ValueError):
    """A safe operator-facing desired-state error."""


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
    short_commit = engine_commit[:12]
    rendered = {
        "CI_FLEET_CAPACITY_BUDGET": str(pool["capacity_budget"]),
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
    values, metadata = build_rendered_env(
        config,
        args.controller,
        host_values,
        config_repository=args.config_repository,
        config_ref=args.config_ref,
        docker_gid=args.docker_gid,
    )
    write_private(args.output, render_env(values))
    write_private(args.metadata_output, json.dumps(metadata, indent=2, sort_keys=True) + "\n")
    print(
        "DESIRED_STATE_RENDERED "
        f"controller={metadata['controller']} state={metadata['controller_state']} "
        f"config_ref={metadata['config_ref']} engine_ref={metadata['engine_ref']}"
    )


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
    render.add_argument("--output", type=Path, required=True)
    render.add_argument("--metadata-output", type=Path, required=True)
    render.set_defaults(function=command_render)
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
