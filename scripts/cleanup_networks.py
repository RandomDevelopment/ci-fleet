#!/usr/bin/env python3
"""Remove expired fleet networks and empty networks in rendered address pools."""

import argparse
from datetime import datetime
import ipaddress
import json
import os
import subprocess
import time

from desired_state import MAX_DOCKER_ADDRESS_POOLS, validate_docker_address_pools

LABEL_PREFIX = "io.randomdevelopment.ci-fleet."
POOL_PREFIX = "CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_"
NETWORK_CREATION_GRACE_SECONDS = 10 * 60


def configured_pools() -> list[ipaddress.IPv4Network]:
    fields = {name: value for name, value in os.environ.items() if name.startswith(POOL_PREFIX)}
    if not fields:
        return []
    count = int(fields[f"{POOL_PREFIX}COUNT"])
    if not 1 <= count <= MAX_DOCKER_ADDRESS_POOLS:
        raise ValueError("invalid Docker default address pool count")
    expected = {f"{POOL_PREFIX}COUNT"} | {
        f"{POOL_PREFIX}{index}_{field}" for index in range(count) for field in ("BASE", "SIZE")
    }
    if set(fields) != expected:
        raise ValueError("Docker address pool fields do not match configured count")
    pools = [
        {"base": fields[f"{POOL_PREFIX}{index}_BASE"], "size": int(fields[f"{POOL_PREFIX}{index}_SIZE"])}
        for index in range(count)
    ]
    return [pool["network"] for pool in validate_docker_address_pools(pools, path="cleanup address pools")]


def docker(*args: str) -> str:
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True).stdout


def in_pools(item: dict, pools: list[ipaddress.IPv4Network]) -> bool:
    configs = item.get("IPAM", {}).get("Config")
    if not isinstance(configs, list) or not configs:
        return False
    subnets = [ipaddress.ip_network(config["Subnet"], strict=True) for config in configs]
    return all(subnet.version == 4 and any(subnet.subnet_of(pool) for pool in pools) for subnet in subnets)


def past_creation_grace(item: dict, now: int) -> bool:
    created = item.get("Created")
    if not isinstance(created, str):
        return False
    try:
        timestamp = datetime.fromisoformat(created)
        if timestamp.tzinfo is None:
            return False
        return now - timestamp.timestamp() >= NETWORK_CREATION_GRACE_SECONDS
    except (ValueError, OverflowError):
        return False


def inspect_network(network_id: str) -> dict | None:
    try:
        payload = json.loads(docker("network", "inspect", network_id))
    except subprocess.CalledProcessError:
        if network_id not in docker("network", "ls", "-q", "--no-trunc").splitlines():
            return None
        raise
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise ValueError("Docker returned an invalid network inspection")
    return payload[0]


def has_containers(item: dict, network_id: str) -> bool:
    containers = item.get("Containers")
    if not isinstance(containers, dict):
        raise ValueError("Docker network inspection omitted container attachments")
    return bool(containers) or bool(docker(
        "ps", "-aq", "--filter", f"network={network_id}", "--filter", f"network={item['Name']}"
    ).strip())


def protected_network(item: dict) -> bool:
    return (
        item["Name"] in {"bridge", "host", "none", "ci-fleet_default"}
        or (item.get("Labels") or {}).get("com.docker.compose.project") == "ci-fleet"
        or (item.get("Options") or {}).get("com.docker.network.bridge.default_bridge") == "true"
    )


def cleanup_networks(*, apply: bool, instance: str) -> None:
    pools = configured_pools()
    now = int(time.time())
    for network_id in docker("network", "ls", "-q", "--no-trunc").splitlines():
        item = inspect_network(network_id)
        if item is None:
            continue
        name = item["Name"]
        labels = item.get("Labels") or {}
        if protected_network(item):
            continue
        if has_containers(item, network_id):
            continue
        expires = labels.get(f"{LABEL_PREFIX}expires-at", "")
        expired = (
            labels.get(f"{LABEL_PREFIX}managed") == "true"
            and (not instance or labels.get(f"{LABEL_PREFIX}instance") == instance)
            and expires.isascii() and expires.isdigit() and int(expires) <= now
        )
        if expired:
            reason = f"expired={expires}"
        elif pools and in_pools(item, pools) and past_creation_grace(item, now):
            reason = "empty-default-address-pool"
        else:
            print(f"REPORT network {name} outside-cleanup-scope")
            continue
        if apply:
            # Inspect immediately before removal; Docker refuses a concurrent attachment.
            current = inspect_network(network_id)
            if current is None or protected_network(current) or has_containers(current, network_id):
                continue
            for attempt in range(2):
                try:
                    docker("network", "rm", network_id)
                    print(f"REMOVE network {name} {reason}", flush=True)
                    break
                except subprocess.CalledProcessError as error:
                    current = inspect_network(network_id)
                    if current is None or protected_network(current) or has_containers(current, network_id):
                        break
                    if attempt == 1:
                        if "has active endpoints" in (error.stderr or ""):
                            print(f"DEFER network {name} active-endpoint-race")
                            break
                        raise
        else:
            print(f"WOULD_REMOVE network {name} {reason}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--instance", default="")
    args = parser.parse_args()
    cleanup_networks(apply=args.apply, instance=args.instance)


if __name__ == "__main__":
    try:
        main()
    except (KeyError, TypeError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"ERROR network cleanup failed: {error}") from error
