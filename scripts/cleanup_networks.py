#!/usr/bin/env python3
"""Remove expired fleet networks and empty networks in rendered address pools."""

import argparse
from contextlib import ExitStack
from datetime import datetime
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import time
import uuid

from desired_state import MAX_DOCKER_ADDRESS_POOLS, validate_docker_address_pools

LABEL_PREFIX = "io.randomdevelopment.ci-fleet."
POOL_PREFIX = "CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_"
NETWORK_CREATION_GRACE_SECONDS = 10 * 60
IDLE_WAIT_SECONDS = 10 * 60
IDLE_POLL_SECONDS = 1
PROXY_PROTOCOL_LABEL = f"{LABEL_PREFIX}network-cleanup-lock"


def host_path(path: str) -> Path:
    return Path(os.environ.get("CI_FLEET_ROOT_PREFIX", "") + path)


def trusted_path(path: Path, *, directory: bool) -> None:
    info = path.lstat()
    valid_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not valid_type or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise ValueError("Docker maintenance coordination path is not trusted")


def open_lock(path: Path, stack: ExitStack) -> int:
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    trusted_path(path.parent, directory=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    stack.callback(os.close, fd)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise ValueError("Docker maintenance lock is not trusted")
    return fd


def host_boot_id() -> str:
    boot_id = host_path("/proc/sys/kernel/random/boot_id").read_text().strip()
    if str(uuid.UUID(boot_id)) != boot_id:
        raise ValueError("host boot identity is not canonical")
    return boot_id


def pending_requests(kind: str) -> bool:
    boot_id = host_boot_id()
    inflight = host_path(f"/run/lock/ci-fleet/{kind}")
    if not inflight.exists() and not inflight.is_symlink():
        return False
    trusted_path(inflight, directory=True)
    pending = False
    for directory in inflight.iterdir():
        try:
            canonical = str(uuid.UUID(directory.name)) == directory.name
        except ValueError:
            canonical = False
        if not canonical or not directory.is_dir() or directory.is_symlink():
            pending = True
            continue
        trusted_path(directory, directory=True)
        if directory.name != boot_id:
            shutil.rmtree(directory)
        elif any(directory.iterdir()):
            pending = True
    return pending


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def removal_marker() -> Path:
    boot_id = host_boot_id()
    root = host_path("/run/lock/ci-fleet/removals")
    directory = root / boot_id
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.mkdir(mode=0o700, exist_ok=True)
    trusted_path(root, directory=True)
    trusted_path(directory, directory=True)
    marker = directory / f"{uuid.uuid4()}.json"
    fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    for path in (directory, root, root.parent):
        sync_directory(path)
    return marker


def remove_network(network_id: str) -> None:
    marker = removal_marker()
    try:
        docker("network", "rm", network_id)
    except subprocess.CalledProcessError as error:
        if "Error response from daemon:" not in (error.stderr or ""):
            raise
        marker.unlink()
        sync_directory(marker.parent)
        raise
    else:
        marker.unlink()
        sync_directory(marker.parent)


def active_work() -> bool:
    ids = docker("ps", "-aq", "--no-trunc", "--filter", "status=running", "--filter", "status=restarting", "--filter", "status=paused").splitlines()
    for container_id in ids:
        try:
            inspected = json.loads(docker("inspect", container_id))
        except subprocess.CalledProcessError:
            if container_id not in docker("ps", "-aq", "--no-trunc").splitlines():
                continue
            raise
        if not isinstance(inspected, list) or len(inspected) != 1 or not isinstance(inspected[0], dict):
            raise ValueError("Docker returned an invalid container inspection")
        item = inspected[0]
        state = item.get("State")
        if not isinstance(state, dict) or not isinstance(state.get("Status"), str):
            raise ValueError("Docker container inspection omitted state")
        if state["Status"] not in {"running", "restarting", "paused"}:
            continue
        config = item.get("Config") or {}
        labels = config.get("Labels") or {}
        service = labels.get("com.docker.compose.service")
        mounts = item.get("Mounts") or []
        coordinated = (
            labels.get("com.docker.compose.project") == "ci-fleet"
            and labels.get(PROXY_PROTOCOL_LABEL) == "v1"
            and any(mount.get("Type") == "bind" and mount.get("RW") is True
                    and mount.get("Source") == str(host_path("/run/lock/ci-fleet"))
                    and mount.get("Destination") == "/run/ci-fleet/locks" for mount in mounts)
            and ((service == "controller" and "DOCKER_HOST=unix:///run/ci-fleet/locks/docker.sock" in (config.get("Env") or []))
                 or (service == "docker-socket-proxy" and config.get("Cmd") == ["--docker-socket-proxy"]))
        )
        if not coordinated:
            return True
    return False


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
    ipam = item.get("IPAM")
    if not isinstance(ipam, dict):
        return False
    configs = ipam.get("Config")
    if not isinstance(configs, list) or not configs:
        return False
    for config in configs:
        if not isinstance(config, dict) or not isinstance(config.get("Subnet"), str):
            return False
        try:
            subnet = ipaddress.ip_network(config["Subnet"], strict=True)
        except ValueError:
            return False
        if str(subnet) != config["Subnet"] or subnet.version != 4 or not any(subnet.subnet_of(pool) for pool in pools):
            return False
    return True


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


def remove_networks(*, apply: bool, instance: str, pools: list[ipaddress.IPv4Network]) -> None:
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
            # The exclusive proxy gate prevents new references while existing attachments are rechecked.
            current = inspect_network(network_id)
            if current is None or protected_network(current) or has_containers(current, network_id):
                continue
            for attempt in range(2):
                try:
                    remove_network(network_id)
                    print(f"REMOVE network {name} {reason}", flush=True)
                    break
                except subprocess.CalledProcessError as error:
                    if pending_requests("removals"):
                        raise
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


def cleanup_networks(*, apply: bool, instance: str) -> None:
    pools = configured_pools()
    if not apply:
        remove_networks(apply=False, instance=instance, pools=pools)
        return
    with ExitStack() as stack:
        installer_path = Path(os.environ.get("CI_FLEET_INSTALLER_LOCK", str(host_path("/run/ci-fleet-installer.lock"))))
        installer_lock = open_lock(installer_path, stack)
        try:
            fcntl.flock(installer_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("DEFER network cleanup installer-active")
            return
        maintenance_lock = open_lock(host_path("/run/lock/ci-fleet/docker-maintenance.lock"), stack)
        deadline = time.monotonic() + IDLE_WAIT_SECONDS
        idle_checks = 0
        while True:
            try:
                fcntl.flock(maintenance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                idle_checks = 0
            else:
                try:
                    if docker("info", "--format", "{{.Swarm.LocalNodeState}}").strip() != "inactive":
                        print("DEFER network cleanup unsupported-or-unknown-swarm-state")
                        return
                    if pending_requests("inflight") or pending_requests("removals"):
                        print("DEFER network cleanup unresolved-Docker-request")
                        return
                    if active_work():
                        idle_checks = 0
                    else:
                        idle_checks += 1
                        if idle_checks == 2:
                            remove_networks(apply=True, instance=instance, pools=pools)
                            return
                finally:
                    fcntl.flock(maintenance_lock, fcntl.LOCK_UN)
            if time.monotonic() >= deadline:
                print("DEFER network cleanup active-work-or-maintenance-timeout")
                return
            time.sleep(min(IDLE_POLL_SECONDS, max(0, deadline - time.monotonic())))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--instance", default="")
    args = parser.parse_args()
    cleanup_networks(apply=args.apply, instance=args.instance)


if __name__ == "__main__":
    try:
        main()
    except (KeyError, TypeError, ValueError, OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"ERROR network cleanup failed: {error}") from error
