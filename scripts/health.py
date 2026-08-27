#!/usr/bin/env python3
from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from status_auth import sign_headers


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


@dataclass(frozen=True)
class Thresholds:
    disk_warn_percent: int = 80
    disk_critical_percent: int = 90
    inode_warn_percent: int = 80
    inode_critical_percent: int = 90
    memory_warn_available_percent: int = 15
    memory_critical_available_percent: int = 8
    swap_warn_percent: int = 25
    swap_critical_percent: int = 50
    restart_warn_count: int = 3
    load_warn_per_cpu: float = 1.0
    load_critical_per_cpu: float = 1.5


def thresholds_from(values: dict[str, str]) -> Thresholds:
    mapping = {
        "disk_warn_percent": "CI_FLEET_HEALTH_DISK_WARN_PERCENT",
        "disk_critical_percent": "CI_FLEET_HEALTH_DISK_CRITICAL_PERCENT",
        "inode_warn_percent": "CI_FLEET_HEALTH_INODE_WARN_PERCENT",
        "inode_critical_percent": "CI_FLEET_HEALTH_INODE_CRITICAL_PERCENT",
        "memory_warn_available_percent": "CI_FLEET_HEALTH_MEMORY_WARN_AVAILABLE_PERCENT",
        "memory_critical_available_percent": "CI_FLEET_HEALTH_MEMORY_CRITICAL_AVAILABLE_PERCENT",
        "swap_warn_percent": "CI_FLEET_HEALTH_SWAP_WARN_PERCENT",
        "swap_critical_percent": "CI_FLEET_HEALTH_SWAP_CRITICAL_PERCENT",
        "restart_warn_count": "CI_FLEET_HEALTH_RESTART_WARN_COUNT",
    }
    defaults = Thresholds()
    kwargs = {field: int(values.get(env, getattr(defaults, field))) for field, env in mapping.items()}
    kwargs["load_warn_per_cpu"] = float(values.get("CI_FLEET_HEALTH_LOAD_WARN_PER_CPU", defaults.load_warn_per_cpu))
    kwargs["load_critical_per_cpu"] = float(values.get("CI_FLEET_HEALTH_LOAD_CRITICAL_PER_CPU", defaults.load_critical_per_cpu))
    for field, value in kwargs.items():
        if value < 0 or (field.endswith("percent") and value > 100):
            raise ValueError(f"invalid health threshold: {field}")
    if kwargs["disk_warn_percent"] >= kwargs["disk_critical_percent"]:
        raise ValueError("disk thresholds must increase from warning to critical")
    if kwargs["inode_warn_percent"] >= kwargs["inode_critical_percent"]:
        raise ValueError("inode thresholds must increase from warning to critical")
    if kwargs["memory_critical_available_percent"] >= kwargs["memory_warn_available_percent"]:
        raise ValueError("memory available thresholds must decrease from warning to critical")
    if kwargs["swap_warn_percent"] >= kwargs["swap_critical_percent"]:
        raise ValueError("swap thresholds must increase from warning to critical")
    if kwargs["load_warn_per_cpu"] >= kwargs["load_critical_per_cpu"]:
        raise ValueError("load thresholds must increase from warning to critical")
    return Thresholds(**kwargs)


def render_human(report: dict[str, Any]) -> str:
    lines = [f'{report["status"].upper()} controller={report.get("controller", "fleet")}']
    lines.extend(f'{check["status"].upper()} {check["id"]}' for check in report.get("checks", []) if check["status"] != "ok")
    return "\n".join(lines)


def evaluate_heartbeats(
    controllers: dict[str, dict[str, Any]],
    records: dict[str, dict[str, Any]],
    *,
    now: int,
    grace_seconds: int,
) -> dict[str, Any]:
    hosts = []
    rank = 0
    for controller, desired in sorted(controllers.items()):
        state = desired["state"]
        if state == "disabled":
            status = "retired"
        elif state == "drained":
            status = "maintenance"
        else:
            record = records.get(controller)
            try:
                timestamp = int(record["timestamp"]) if record else 0
                reported = record["status"] if record else ""
                reported_controller = record["controller"] if record else ""
            except (KeyError, TypeError, ValueError):
                timestamp, reported, reported_controller = 0, "", ""
            if reported_controller != controller or abs(now - timestamp) > grace_seconds or reported not in {"healthy", "warning", "unhealthy"}:
                status = "missing"
                rank = 2
            else:
                status = reported
                rank = max(rank, {"healthy": 0, "warning": 1, "unhealthy": 2}[status])
        hosts.append({"controller": controller, "status": status})
    return {"schema_version": 1, "status": ("healthy", "warning", "unhealthy")[rank], "exit_code": rank, "hosts": hosts}


def evaluate(snapshot: dict[str, Any], thresholds: Thresholds) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(check_id: str, severity: str, **details: Any) -> None:
        checks.append({"id": check_id, "status": severity, **details})

    for name, usage in snapshot["disks"].items():
        used = usage["used_percent"]
        severity = "critical" if used >= thresholds.disk_critical_percent else "warning" if used >= thresholds.disk_warn_percent else "ok"
        add(f"disk_{name}", severity, used_percent=used)
        inodes = usage["inode_used_percent"]
        severity = "critical" if inodes >= thresholds.inode_critical_percent else "warning" if inodes >= thresholds.inode_warn_percent else "ok"
        add(f"inode_{name}", severity, used_percent=inodes)

    available = snapshot["memory_available_percent"]
    add("memory", "critical" if available <= thresholds.memory_critical_available_percent else "warning" if available <= thresholds.memory_warn_available_percent else "ok", available_percent=available)
    load = snapshot.get("load_per_cpu", 0)
    add("load", "critical" if load >= thresholds.load_critical_per_cpu else "warning" if load >= thresholds.load_warn_per_cpu else "ok", load_per_cpu=load)
    swap = snapshot["swap_used_percent"]
    add("swap", "critical" if swap >= thresholds.swap_critical_percent else "warning" if swap >= thresholds.swap_warn_percent else "ok", used_percent=swap)
    add("oom", "critical" if snapshot["recent_oom"] or snapshot["controller"]["oom_killed"] else "ok")
    add("docker", "ok" if snapshot["docker_available"] else "critical")
    network = snapshot.get("docker_network_headroom")
    if network and network.get("state") != "not_configured":
        if network.get("state") == "unavailable":
            add("docker_network_inspection", "critical")
        else:
            severity = {"healthy": "ok", "warning": "warning", "critical": "critical"}.get(network.get("state"), "critical")
            add(
                "docker_network_headroom",
                severity,
                configured=network.get("configured", 0),
                used=network.get("used", 0),
                free=network.get("free", 0),
                reserve=network.get("reserve", 0),
            )
            add("docker_network_legacy", "warning" if network.get("legacy", 0) else "ok", count=network.get("legacy", 0))

    desired = snapshot["desired_state"]
    controller_state = snapshot["controller"]["state"]
    controller_ok = controller_state == "running" if desired == "active" else controller_state in {"missing", "exited", "created"}
    add("controller", "ok" if controller_ok else "critical", state=controller_state, desired_state=desired)
    status_expected = desired == "active" and controller_state == "running"
    add("controller_status", "warning" if status_expected and not snapshot.get("controller_status_valid", True) else "ok")
    restarts = snapshot["controller"]["restart_count"]
    add("restarts", "warning" if restarts >= thresholds.restart_warn_count else "ok", count=restarts)

    configured = snapshot["configured_capacity"]
    effective = snapshot["effective_capacity"]
    expected = configured if desired == "active" else {"min": 0, "max": 0}
    add("capacity", "ok" if effective == expected else "critical", configured=configured, effective=effective)

    managed = snapshot["managed"]
    add("managed_unhealthy", "critical" if managed["unhealthy"] or managed["restarting"] else "ok", unhealthy=managed["unhealthy"], restarting=managed["restarting"])
    add("managed_inactive", "warning" if managed["inactive"] else "ok", count=managed["inactive"])
    for kind, count in snapshot["stale"].items():
        add(f"stale_{kind[:-1] if kind.endswith('s') else kind}", "warning" if count else "ok", count=count)

    for name, state in snapshot["services"].items():
        add(name, "ok" if state == "ok" else "critical" if state == "failed" else "warning", state=state)
    for name, state in snapshot["timers"].items():
        add(f"timer_{name}", "ok" if state == "ok" else "critical" if state == "failed" else "warning", state=state)
    add("updates", "critical" if snapshot["failed_packages"] else "ok")
    add("reboot", "warning" if snapshot["pending_reboot"] else "ok")
    add("clock", "ok" if snapshot["clock_synchronized"] else "warning")
    backup = snapshot["backup"]
    add("backup", "warning" if backup == "failed" else "ok", state=backup)
    reconciliation = snapshot.get("reconciliation")
    if reconciliation:
        reconciliation_severity = (
            "ok" if reconciliation["status"] in {"converged", "bootstrap"}
            else "warning" if reconciliation["status"] in {"missing", "pending", "reconciling"}
            else "critical"
        )
        add(
            "reconciliation",
            reconciliation_severity,
            state=reconciliation["status"],
            desired_commit=reconciliation["desired_commit"],
            applied_commit=reconciliation["applied_commit"],
            reported_health=reconciliation["health"],
        )

    rank = max(({"ok": 0, "warning": 1, "critical": 2}[check["status"]] for check in checks), default=0)
    overall = ("healthy", "warning", "unhealthy")[rank]
    if rank == 0 and desired in {"drained", "disabled"}:
        overall = "maintenance"
    return {
        "schema_version": 1,
        "controller": snapshot["controller_id"],
        "desired_state": desired,
        "status": overall,
        "exit_code": rank,
        "checks": checks,
    }


def build_status_report(snapshot: dict[str, Any], health_report: dict[str, Any], *, generated_at: int) -> dict[str, Any]:
    reconciliation = snapshot.get("reconciliation") or {}
    state = reconciliation.get("status", "missing")
    status_expected = snapshot.get("desired_state") == "active" and snapshot.get("controller", {}).get("state") == "running"
    error_code = "health_controller_status" if status_expected and snapshot.get("controller_status_valid") is False else ""
    if not error_code:
        error_code = f"reconciliation_{state}" if state in {"drift", "failed", "invalid", "rolled_back"} else ""
    if not error_code:
        checks = health_report.get("checks", [])
        failed = next((check for check in checks if check.get("status") == "critical"), None)
        failed = failed or next((check for check in checks if check.get("status") == "warning"), None)
        error_code = f"health_{failed['id']}" if failed else ""
    error = {"code": error_code, "message": error_code.replace("_", " ")} if error_code else None
    timers = snapshot.get("timers", {})
    disks = snapshot["disks"]
    process_state = snapshot["controller"].get("state", "unknown")
    if process_state not in {"created", "exited", "missing", "paused", "restarting", "running"}:
        process_state = "unknown"
    commits = [reconciliation.get(name, "") for name in ("desired_commit", "applied_commit")]
    commits = [commit if isinstance(commit, str) and (not commit or re.fullmatch(r"[0-9a-f]{40}", commit)) else "" for commit in commits]
    last_success = reconciliation.get("last_success_at")
    if not isinstance(last_success, int) or isinstance(last_success, bool) or last_success > generated_at:
        last_success = None
    docker = {
        "healthy": bool(snapshot.get("docker_available")),
        "oom": bool(snapshot.get("recent_oom") or snapshot["controller"].get("oom_killed")),
    }
    network = snapshot.get("docker_network_headroom")
    if network and network.get("state") != "not_configured":
        docker["network"] = {key: network.get(key, 0) for key in ("configured", "used", "free", "legacy")}
    return {
        "schema_version": 1,
        "controller": {
            "id": snapshot["controller_id"],
            "software_version": snapshot.get("software_version", "unknown"),
            "boot_time": snapshot.get("boot_time", 0),
            "ssh": snapshot.get("ssh", "unknown"),
        },
        "configuration": {
            "desired_commit": commits[0],
            "applied_commit": commits[1],
        },
        "reconciliation": {"state": state, "last_success_at": last_success},
        "drift": {"state": snapshot.get("services", {}).get("drift", "unknown")},
        "process": {
            "state": process_state,
            "restart_count": snapshot["controller"].get("restart_count", 0),
        },
        "timers": {
            "reconciliation": timers.get("reconcile", "unknown"),
            "drift": timers.get("drift", "unknown"),
            "health": timers.get("health", "unknown"),
            "cleanup": timers.get("cleanup", "unknown"),
        },
        "runners": snapshot.get("runners", {"current": 0, "busy": 0, "maximum": 0}),
        "metrics": {
            "cpu": snapshot.get("cpu", {"logical": 1, "used_percent": 0}),
            "memory": snapshot.get("memory", {"total_bytes": 0, "available_bytes": 0}),
            "swap": snapshot.get("swap", {"total_bytes": 0, "used_bytes": 0}),
            "disk": {name: {"total_bytes": value.get("total_bytes", 0), "used_bytes": value.get("used_bytes", 0)} for name, value in disks.items()},
            "inodes": {name: {"total": value.get("inode_total", 0), "used": value.get("inode_used", 0)} for name, value in disks.items()},
            "load": snapshot.get("load", {"one": 0, "five": 0, "fifteen": 0}),
        },
        "docker": docker,
        "error": error,
        "generated_at": generated_at,
    }


Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as error:
        return subprocess.CompletedProcess(args, 124 if isinstance(error, subprocess.TimeoutExpired) else 127, "", "")


def _disk(path: str) -> dict[str, int]:
    try:
        value = os.statvfs(path)
    except OSError:
        value = os.statvfs("/")
    used = value.f_blocks - value.f_bfree
    iused = value.f_files - value.f_ffree
    return {
        "used_percent": round(100 * used / max(value.f_blocks, 1)),
        "inode_used_percent": round(100 * iused / max(value.f_files, 1)),
        "total_bytes": value.f_blocks * value.f_frsize,
        "used_bytes": used * value.f_frsize,
        "inode_total": value.f_files,
        "inode_used": iused,
    }


def _count(run: Runner, args: list[str]) -> int:
    result = run(args)
    return len([line for line in result.stdout.splitlines() if line.strip()]) if result.returncode == 0 else 0


def _stale_resources(run: Runner, instance: str) -> dict[str, int]:
    result = run([str(Path(__file__).with_name("cleanup.sh")), "--instance", instance])
    stale = {"containers": 0, "networks": 0, "volumes": 0}
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            match = re.match(r"(?:WOULD_REMOVE|KEEP) (container|network|volume) ", line)
            if match:
                stale[f"{match.group(1)}s"] += 1
    return stale


def _parse_network_pool(values: dict[str, str], index: int) -> tuple[ipaddress.IPv4Network, int] | None:
    base = values.get(f"CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_{index}_BASE")
    size = values.get(f"CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_{index}_SIZE")
    if not base or not size:
        return None
    try:
        network = ipaddress.ip_network(base, strict=True)
        subnet_size = int(size)
    except ValueError:
        return None
    if network.version != 4 or subnet_size < network.prefixlen or subnet_size > 29:
        return None
    return network, subnet_size


def _docker_network_headroom(run: Runner, values: dict[str, str], *, docker_ok: bool) -> dict[str, Any]:
    empty = {"configured": 0, "used": 0, "free": 0, "reserve": 0, "legacy": 0, "state": "unavailable"}
    try:
        configured_count = int(values.get("CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_COUNT", "0"))
        configured_max = int(values.get("CI_FLEET_CONFIGURED_MAX_RUNNERS", values.get("CI_FLEET_MAX_RUNNERS", "0")))
        networks_per_runner = int(values.get("CI_FLEET_DOCKER_NETWORKS_PER_RUNNER", "1"))
        reserve = int(values.get("CI_FLEET_DOCKER_NETWORK_RESERVE_SUBNETS", "0"))
    except ValueError:
        return empty
    if configured_count == 0 and "CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_COUNT" not in values:
        return {**empty, "state": "not_configured"}
    if not docker_ok:
        return empty
    pools: list[tuple[ipaddress.IPv4Network, int]] = []
    for index in range(configured_count):
        pool = _parse_network_pool(values, index)
        if pool is None:
            return empty
        pools.append(pool)
    if not pools or configured_max < 0 or networks_per_runner < 1 or reserve < 1:
        return empty
    listed = run(["docker", "network", "ls", "--format", "{{.Name}}"])
    if listed.returncode != 0:
        return empty
    configured = sum(1 << (size - pool.prefixlen) for pool, size in pools)
    occupied: list[list[tuple[int, int]]] = [[] for _ in pools]
    bridge_occupied: list[list[tuple[int, int]]] = [[] for _ in pools]
    legacy_networks = 0
    for name in [line.strip() for line in listed.stdout.splitlines() if line.strip()]:
        inspected = run(["docker", "network", "inspect", name])
        if inspected.returncode != 0:
            refreshed = run(["docker", "network", "ls", "--format", "{{.Name}}"])
            if refreshed.returncode != 0:
                return empty
            if name not in {line.strip() for line in refreshed.stdout.splitlines() if line.strip()}:
                continue
            return empty
        try:
            payload = json.loads(inspected.stdout)
        except json.JSONDecodeError:
            return empty
        if not isinstance(payload, list) or not payload:
            return empty
        subnets: list[ipaddress.IPv4Network] = []
        saw_ipv6 = False
        for entry in payload:
            configs = entry.get("IPAM", {}).get("Config", []) if isinstance(entry, dict) else []
            if not isinstance(configs, list):
                return empty
            for config in configs:
                subnet = config.get("Subnet") if isinstance(config, dict) else None
                if not isinstance(subnet, str):
                    return empty
                try:
                    network = ipaddress.ip_network(subnet, strict=False)
                except ValueError:
                    return empty
                if network.version == 6:
                    saw_ipv6 = True
                    continue
                subnets.append(network)
        if not subnets:
            if saw_ipv6 or name in {"bridge", "host", "none"}:
                continue
            legacy_networks += 1
            continue
        network_legacy = False
        for subnet in subnets:
            overlaps = 0
            for index, (pool, size) in enumerate(pools):
                first = max(int(subnet.network_address), int(pool.network_address))
                last = min(int(subnet.broadcast_address), int(pool.broadcast_address))
                if first > last:
                    continue
                block_size = 1 << (32 - size)
                pool_start = int(pool.network_address)
                interval = ((first - pool_start) // block_size, (last - pool_start) // block_size)
                occupied[index].append(interval)
                if name == "bridge":
                    bridge_occupied[index].append(interval)
                overlaps += 1
            if overlaps == 0:
                network_legacy = True
            elif overlaps != 1 or not any(subnet.subnet_of(pool) and subnet.prefixlen == size for pool, size in pools):
                network_legacy = True
        if network_legacy and name != "bridge":
            legacy_networks += 1
    used = 0
    for intervals in occupied:
        end = -1
        for start, stop in sorted(intervals):
            if start > end:
                used += stop - start + 1
            elif stop > end:
                used += stop - end
            end = max(end, stop)
    bridge_used = 0
    for intervals in bridge_occupied:
        end = -1
        for start, stop in sorted(intervals):
            if start > end:
                bridge_used += stop - start + 1
            elif stop > end:
                bridge_used += stop - end
            end = max(end, stop)
    free = max(configured - used, 0)
    policy_max = 0 if values.get("CI_FLEET_CONTROLLER_STATE") == "disabled" else configured_max
    required = policy_max * networks_per_runner + reserve + 1
    if free == 0:
        state = "critical"
    elif legacy_networks > 0 or configured - bridge_used < required or free <= reserve:
        state = "warning"
    else:
        state = "healthy"
    return {
        "configured": configured,
        "used": used,
        "free": free,
        "reserve": reserve,
        "legacy": legacy_networks,
        "state": state,
    }


def _timespan_seconds(value: str) -> float | None:
    units = {"y": 365.25 * 86400, "month": 365.25 * 86400 / 12, "w": 7 * 86400, "d": 86400, "h": 3600, "min": 60, "s": 1, "ms": 0.001, "us": 0.000001, "µs": 0.000001, "ns": 0.000000001}
    matches = list(re.finditer(r"([0-9]+(?:\.[0-9]+)?)(month|min|ms|us|µs|ns|y|w|d|h|s)", value))
    if not matches or "".join(match.group(0) for match in matches) != re.sub(r"\s+", "", value):
        return None
    return sum(float(match.group(1)) * units[match.group(2)] for match in matches)


def _ssh_state(run: Runner) -> str:
    results = [run(["systemctl", action, unit]) for unit in ("ssh.service", "ssh.socket", "sshd.service") for action in ("is-enabled", "is-active")]
    states = [result.stdout.strip().lower() for result in results]
    if any(result.returncode == 0 and state in {"active", "enabled"} for result, state in zip(results, states)):
        return "enabled"
    disabled = {"disabled", "inactive", "masked", "not-found", "failed", "static"}
    return "disabled" if all(state in disabled for state in states) else "unknown"


def _unit_state(run: Runner, unit: str, timer: bool = False, max_age_seconds: int = 0) -> str:
    if timer:
        if run(["systemctl", "is-active", unit]).returncode != 0:
            return "failed"
        if run(["systemctl", "is-enabled", unit]).returncode != 0:
            return "stale"
        if max_age_seconds:
            last = run(["systemctl", "show", unit, "--property=LastTriggerUSecMonotonic", "--value"])
            triggered = _timespan_seconds(last.stdout.strip())
            now = time.clock_gettime(time.CLOCK_BOOTTIME)
            if last.returncode != 0 or triggered is None or now - triggered > max_age_seconds:
                return "stale"
    result = run(["systemctl", "show", unit, "--property=Result", "--value"])
    return "failed" if result.returncode != 0 or result.stdout.strip() not in {"", "success"} else "ok"


def _container(run: Runner, name: str) -> tuple[dict[str, Any], dict[str, int]]:
    state = run(["docker", "inspect", "--format", "{{.State.Status}}", name])
    if state.returncode != 0:
        return {"state": "missing", "restart_count": 0, "oom_killed": False}, {"min": 0, "max": 0}
    oom = run(["docker", "inspect", "--format", "{{.State.OOMKilled}}", name])
    restarts = run(["docker", "inspect", "--format", "{{.RestartCount}}", name])
    environment = run(["docker", "inspect", "--format", "{{range .Config.Env}}{{println .}}{{end}}", name])
    env = dict(value.split("=", 1) for value in environment.stdout.splitlines() if "=" in value)
    try:
        capacity = {"min": int(env.get("CI_FLEET_MIN_RUNNERS", 0)), "max": int(env.get("CI_FLEET_MAX_RUNNERS", 0))}
    except ValueError:
        return {"state": "invalid", "restart_count": 0, "oom_killed": False}, {"min": 0, "max": 0}
    try:
        restart_count = int(restarts.stdout.strip() or 0)
    except ValueError:
        restart_count = 0
    controller_state = state.stdout.strip() or "missing"
    if controller_state != "running":
        capacity = {"min": 0, "max": 0}
    return {
        "state": controller_state,
        "restart_count": restart_count,
        "oom_killed": oom.stdout.strip().lower() == "true",
    }, capacity


def _memory_details(root: Path) -> tuple[dict[str, int], dict[str, int]]:
    values: dict[str, int] = {}
    try:
        for line in (root / "proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            values[key] = int(value.split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        return {"total_bytes": 0, "available_bytes": 0}, {"total_bytes": 0, "used_bytes": 0}
    swap_total = values.get("SwapTotal", 0)
    return (
        {"total_bytes": values.get("MemTotal", 0), "available_bytes": values.get("MemAvailable", 0)},
        {"total_bytes": swap_total, "used_bytes": max(0, swap_total - values.get("SwapFree", 0))},
    )


def _memory(root: Path) -> tuple[int, int]:
    memory, swap = _memory_details(root)
    available = round(100 * memory["available_bytes"] / max(memory["total_bytes"], 1))
    swap_used = round(100 * swap["used_bytes"] / max(swap["total_bytes"], 1)) if swap["total_bytes"] else 0
    return available, swap_used


def _cpu(root: Path) -> dict[str, float | int]:
    # ponytail: cumulative boot-average CPU; persist the prior sample if interval utilization becomes necessary.
    try:
        fields = next(line for line in (root / "proc/stat").read_text().splitlines() if line.startswith("cpu ")).split()[1:]
        counters = [int(value) for value in fields]
        total = sum(counters[:8])
        used = total - counters[3]
        percent = round(100 * used / max(total, 1), 1)
    except (OSError, ValueError, IndexError, StopIteration):
        percent = 0.0
    return {"logical": max(os.cpu_count() or 1, 1), "used_percent": percent}


def _boot_time(root: Path) -> int:
    try:
        line = next(line for line in (root / "proc/stat").read_text().splitlines() if line.startswith("btime "))
        return max(int(line.split()[1]), 0)
    except (OSError, ValueError, IndexError, StopIteration):
        return 0


def _controller_status(run: Runner, name: str, controller: str, maximum: int) -> tuple[dict[str, int], str, bool]:
    result = run(["docker", "exec", name, "cat", "/run/ci-fleet/status.json"])
    try:
        value = json.loads(result.stdout) if result.returncode == 0 else {}
        current, busy, reported_max = (value[key] for key in ("current", "busy", "maximum"))
        version = value["software_version"]
        generated_at = value["generated_at"]
        valid = (
            value.get("controller") == controller
            and all(isinstance(count, int) and not isinstance(count, bool) and count >= 0 for count in (current, busy, reported_max))
            and busy <= current <= reported_max == maximum
            and isinstance(version, str)
            and bool(re.fullmatch(r"[A-Za-z0-9_.+-]{1,64}", version))
            and isinstance(generated_at, int) and not isinstance(generated_at, bool)
            and abs(int(time.time()) - generated_at) <= 120
        )
        if valid:
            return {"current": current, "busy": busy, "maximum": reported_max}, version, True
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        pass
    return {"current": 0, "busy": 0, "maximum": maximum}, "unknown", False


def _memory_pressure(root: Path) -> float | None:
    try:
        for line in (root / "proc/pressure/memory").read_text().splitlines():
            if line.startswith("some "):
                match = re.search(r"avg300=([0-9.]+)", line)
                return float(match.group(1)) if match else None
    except OSError:
        pass
    return None


def _backup_state(values: dict[str, str], run: Runner) -> str:
    command = values.get("CI_FLEET_HEALTH_BACKUP_CHECK")
    if not command:
        return "not_configured"
    path = Path(command)
    try:
        mode = path.stat()
    except OSError:
        return "failed"
    if not path.is_absolute() or mode.st_uid != 0 or mode.st_mode & (stat.S_IWGRP | stat.S_IWOTH) or not os.access(path, os.X_OK):
        return "failed"
    return "ok" if run([str(path)]).returncode == 0 else "failed"


def _reconcile_state(path: Path) -> dict[str, Any]:
    empty = {"status": "missing", "desired_commit": "", "applied_commit": "", "health": "", "last_success_at": None}
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return empty
    if not isinstance(value, dict):
        return {**empty, "status": "invalid"}
    status = value.get("status", "")
    if not isinstance(status, str) or status not in {"converged", "drift", "invalid", "pending", "reconciling", "rolled_back", "failed"}:
        status = "invalid"
    commits = [value.get(name, "") for name in ("desired_commit", "applied_commit")]
    commits = [commit if isinstance(commit, str) and (not commit or re.fullmatch(r"[0-9a-f]{40}", commit)) else "invalid" for commit in commits]
    reported_health = value.get("health", "")
    if not isinstance(reported_health, str) or reported_health not in {"", "healthy", "warning", "unhealthy", "maintenance", "drift", "unknown"}:
        reported_health = "invalid"
    last_success = value.get("last_success_at")
    if not isinstance(last_success, int) or isinstance(last_success, bool) or last_success < 0:
        last_success = None
    return {"status": status, "desired_commit": commits[0], "applied_commit": commits[1], "health": reported_health, "last_success_at": last_success}


def collect_snapshot(values: dict[str, str], *, root: Path = Path("/"), run: Runner = _run) -> dict[str, Any]:
    docker_root = values.get("CI_FLEET_DOCKER_ROOT", "/var/lib/docker")
    available, swap = _memory(root)
    memory, swap_metrics = _memory_details(root)
    loads = os.getloadavg()
    docker_ok = run(["docker", "info"]).returncode == 0
    controller_name = values.get("CI_FLEET_CONTROLLER_CONTAINER", "ci-fleet-controller-1")
    instance = values.get("CI_FLEET_INSTANCE", "unknown")
    configured = {"min": int(values.get("CI_FLEET_MIN_RUNNERS", 0)), "max": int(values.get("CI_FLEET_MAX_RUNNERS", 0))}
    controller, effective = _container(run, controller_name) if docker_ok else ({"state": "missing", "restart_count": 0, "oom_killed": False}, {"min": 0, "max": 0})
    runners, software_version, controller_status_valid = _controller_status(run, controller_name, instance, configured["max"]) if docker_ok else ({"current": 0, "busy": 0, "maximum": configured["max"]}, "unknown", False)
    managed = {"running": 0, "inactive": 0, "unhealthy": 0, "restarting": 0}
    if docker_ok:
        result = run(["docker", "ps", "-a", "--filter", "label=io.randomdevelopment.ci-fleet.managed=true", "--format", "{{json .}}"])
        for line in result.stdout.splitlines() if result.returncode == 0 else []:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            state, status = item.get("State", ""), item.get("Status", "").lower()
            managed["running" if state == "running" else "inactive"] += 1
            managed["unhealthy"] += int("unhealthy" in status)
            managed["restarting"] += int(state == "restarting" or "restarting" in status)
    oom = run(["journalctl", "--dmesg", "--since=-24h", "--grep=Out of memory|Killed process", "--quiet"])
    timer_ages = {"health": 900, "cleanup": 172800, "drift": 3600}
    remote_config = bool(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", values.get("CI_FLEET_CONFIG_REPOSITORY", "")))
    reconciliation = _reconcile_state(root / "var/lib/ci-fleet/reconcile/state.json") if remote_config else None
    if reconciliation and values.get("CI_FLEET_HEALTH_BOOTSTRAP") == "1":
        reconciliation["status"] = "bootstrap"
    if remote_config:
        timer_ages["reconcile"] = 900
    timers = {name: _unit_state(run, f"ci-fleet-{name}.timer", timer=True, max_age_seconds=age) for name, age in timer_ages.items()}
    service_units = {
        "cleanup": "ci-fleet-cleanup.service",
        "drift": "ci-fleet-drift.service",
    }
    if remote_config:
        service_units["reconcile"] = "ci-fleet-reconcile.service"
    services = {name: _unit_state(run, unit) for name, unit in service_units.items()}
    debian = (root / "etc/debian_version").exists()
    if debian:
        timers["updates"] = _unit_state(run, "apt-daily-upgrade.timer", timer=True, max_age_seconds=172800)
        services["updates"] = _unit_state(run, "apt-daily-upgrade.service")
    if values.get("CI_FLEET_HEALTH_BOOTSTRAP") == "1":
        # ponytail: activation validates unit installation separately; scheduled runs verify maintenance state after commit.
        timers = {name: "ok" for name in timers}
        services = {name: "ok" for name in services}
    stale = _stale_resources(run, instance) if docker_ok else {"containers": 0, "networks": 0, "volumes": 0}
    stale["images"] = _count(run, ["docker", "images", "-q", "--filter", "dangling=true", "--filter", "label=io.randomdevelopment.ci-fleet.managed=true"]) if docker_ok else 0
    stale["build_cache"] = _count(run, ["docker", "buildx", "du", "--filter", "until=168h", "--format", "json"]) if docker_ok else 0
    docker_network_headroom = _docker_network_headroom(run, values, docker_ok=docker_ok)
    return {
        "controller_id": instance,
        "desired_state": values.get("CI_FLEET_CONTROLLER_STATE", "active"),
        "disks": {"root": _disk(str(root)), "docker": _disk(str(root / docker_root.lstrip("/")))},
        "cpu": _cpu(root),
        "memory": memory,
        "swap": swap_metrics,
        "load": {"one": loads[0], "five": loads[1], "fifteen": loads[2]},
        "boot_time": _boot_time(root),
        "ssh": _ssh_state(run),
        "software_version": software_version if software_version != "unknown" else values.get("CI_FLEET_ENGINE_REF", "unknown"),
        "runners": runners,
        "controller_status_valid": controller_status_valid,
        "memory_available_percent": available,
        "load_per_cpu": loads[2] / max(os.cpu_count() or 1, 1),
        "swap_used_percent": swap if (pressure := _memory_pressure(root)) is None or pressure >= 0.1 else 0,
        "recent_oom": oom.returncode == 0 and bool(oom.stdout.strip()),
        "docker_available": docker_ok,
        "controller": controller,
        "configured_capacity": configured,
        "effective_capacity": effective,
        "managed": managed,
        "stale": stale,
        "services": services,
        "timers": timers,
        "pending_reboot": (root / "var/run/reboot-required").exists(),
        "failed_packages": debian and bool(run(["dpkg", "--audit"]).stdout.strip()),
        "clock_synchronized": run(["timedatectl", "show", "--property=NTPSynchronized", "--value"]).stdout.strip() == "yes",
        "backup": _backup_state(values, run),
        "reconciliation": reconciliation,
        "docker_network_headroom": docker_network_headroom,
    }


def load_monitoring_config(path: Path) -> dict[str, str]:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {}
    expected_owner = os.getuid() if os.environ.get("CI_FLEET_TESTING") == "1" else 0
    if not stat.S_ISREG(info.st_mode) or info.st_uid != expected_owner or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError(f"monitoring configuration must be root-owned mode 0600: {path}")
    values: dict[str, str] = {}
    for number, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not re.fullmatch(r"CI_FLEET_HEALTH_[A-Z0-9_]+=[^\n]*", line):
            raise ValueError(f"invalid monitoring configuration at line {number}")
        key, value = line.split("=", 1)
        values[key] = value
    return values


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, sort_keys=True) + "\n")
    os.chmod(temporary, 0o644)
    temporary.replace(path)


def _send_status(
    values: dict[str, str],
    report: dict[str, Any],
    *,
    now: int | None = None,
    nonce: str | None = None,
    opener: Callable[..., Any] | None = None,
) -> int:
    url = values.get("CI_FLEET_HEALTH_STATUS_URL")
    if not url:
        return 0
    try:
        parsed = urllib.parse.urlsplit(url)
        parsed.port
    except ValueError:
        return 1
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.path != "/v1/status" or parsed.query or parsed.fragment:
        return 1
    key_file = values.get("CI_FLEET_HEALTH_STATUS_KEY_FILE")
    if not key_file:
        return 1
    path = Path(key_file)
    try:
        info = path.stat()
        expected_owner = os.getuid() if os.environ.get("CI_FLEET_TESTING") == "1" else 0
        if info.st_uid != expected_owner or stat.S_IMODE(info.st_mode) & 0o077:
            return 1
        key = path.read_bytes()
    except OSError:
        return 1
    if not 32 <= len(key) <= 128:
        return 1
    body = json.dumps(report, separators=(",", ":"), sort_keys=True).encode()
    if len(body) > 32_768:
        return 1
    generated_at = int(now if now is not None else time.time())
    request = urllib.request.Request(
        url,
        data=body,
        headers=sign_headers(report["controller"]["id"], body, key, timestamp=generated_at, nonce=nonce or secrets.token_hex(16)),
        method="POST",
    )
    try:
        transport = opener or urllib.request.build_opener(_NoRedirect).open
        with transport(request, timeout=10) as response:
            return 0 if 200 <= response.status < 300 else 1
    except (OSError, ValueError, http.client.HTTPException):
        return 1


def _send_heartbeat(
    values: dict[str, str],
    report: dict[str, Any],
    *,
    opener: Callable[..., Any] | None = None,
) -> int:
    url = values.get("CI_FLEET_HEALTH_HEARTBEAT_URL")
    if not url:
        return 0
    try:
        parsed = urllib.parse.urlsplit(url)
        parsed.port
    except ValueError:
        return 2
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return 2
    headers = {"Content-Type": "application/json"}
    token_file = values.get("CI_FLEET_HEALTH_HEARTBEAT_TOKEN_FILE")
    if token_file:
        path = Path(token_file)
        try:
            info = path.stat()
            expected_owner = os.getuid() if os.environ.get("CI_FLEET_TESTING") == "1" else 0
            if info.st_uid != expected_owner or stat.S_IMODE(info.st_mode) & 0o077:
                return 2
            headers["Authorization"] = f"Bearer {path.read_text().strip()}"
        except OSError:
            return 2
    request = urllib.request.Request(url, data=json.dumps(report).encode(), headers=headers, method="POST")
    try:
        transport = opener or urllib.request.build_opener(_NoRedirect).open
        with transport(request, timeout=10) as response:
            return 0 if 200 <= response.status < 300 else 1
    except (OSError, ValueError, http.client.HTTPException):
        return 1


def _local(args: argparse.Namespace) -> int:
    environment = dict(os.environ)
    values = dict(environment)
    config_invalid = False
    try:
        values.update(load_monitoring_config(args.monitoring_config))
        thresholds = thresholds_from(values)
    except (OSError, UnicodeError, ValueError):
        if environment.get("CI_FLEET_STATUS_REPORTING_REQUIRED") != "1":
            raise
        config_invalid = True
        values = environment
        thresholds = thresholds_from(values)
    snapshot = collect_snapshot(values)
    report = evaluate(snapshot, thresholds)
    now = int(time.time())
    report["timestamp"] = now
    delivery = 0
    if environment.get("CI_FLEET_HEALTH_SUPPRESS_DELIVERY") != "1":
        if config_invalid:
            delivery = 1
        elif values.get("CI_FLEET_HEALTH_STATUS_URL") or values.get("CI_FLEET_STATUS_REPORTING_REQUIRED") == "1":
            delivery = (
                _send_status(values, build_status_report(snapshot, report, generated_at=now), now=now)
                if values.get("CI_FLEET_HEALTH_STATUS_URL") else 1
            )
        else:
            delivery = _send_heartbeat(values, report)
    if delivery:
        severity = "critical" if delivery == 2 else "warning"
        report["checks"].append({"id": "status_delivery", "status": severity})
        if delivery > report["exit_code"]:
            report["status"], report["exit_code"] = ("unhealthy", 2) if delivery == 2 else ("warning", 1)
    _write_report(args.output, report)
    print(json.dumps(report, sort_keys=True) if args.json else render_human(report))
    return int(report["exit_code"])


def _heartbeats(args: argparse.Namespace) -> int:
    config = json.loads(args.config.read_text())
    records = {}
    for controller in config["controllers"]:
        path = args.input_dir / f"{controller}.json"
        if path.exists():
            records[controller] = json.loads(path.read_text())
    report = evaluate_heartbeats(config["controllers"], records, now=int(time.time()), grace_seconds=args.grace_seconds)
    print(json.dumps(report, sort_keys=True) if args.json else "\n".join(f'{host["status"].upper()} controller={host["controller"]}' for host in report["hosts"]))
    return int(report["exit_code"])


def main() -> int:
    parser = argparse.ArgumentParser(description="Redacted ci-fleet host health")
    commands = parser.add_subparsers(dest="command", required=True)
    local = commands.add_parser("local")
    local.add_argument("--json", action="store_true")
    local.add_argument("--monitoring-config", type=Path, default=Path("/etc/ci-fleet/monitoring.env"))
    local.add_argument("--output", type=Path, default=Path("/var/lib/ci-fleet/health/latest.json"))
    local.set_defaults(handler=_local)
    heartbeats = commands.add_parser("heartbeats")
    heartbeats.add_argument("--config", type=Path, required=True)
    heartbeats.add_argument("--input-dir", type=Path, required=True)
    heartbeats.add_argument("--grace-seconds", type=int, default=900)
    heartbeats.add_argument("--json", action="store_true")
    heartbeats.set_defaults(handler=_heartbeats)
    args = parser.parse_args()
    try:
        return args.handler(args)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"CRITICAL health_configuration_invalid: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
