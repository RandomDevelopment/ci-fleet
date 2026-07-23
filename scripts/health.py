#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


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

    desired = snapshot["desired_state"]
    controller_state = snapshot["controller"]["state"]
    controller_ok = controller_state == "running" if desired == "active" else controller_state in {"missing", "exited", "created"}
    add("controller", "ok" if controller_ok else "critical", state=controller_state, desired_state=desired)
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


def _timespan_seconds(value: str) -> float | None:
    units = {"y": 365.25 * 86400, "month": 365.25 * 86400 / 12, "w": 7 * 86400, "d": 86400, "h": 3600, "min": 60, "s": 1, "ms": 0.001, "us": 0.000001, "µs": 0.000001, "ns": 0.000000001}
    matches = list(re.finditer(r"([0-9]+(?:\.[0-9]+)?)(month|min|ms|us|µs|ns|y|w|d|h|s)", value))
    if not matches or "".join(match.group(0) for match in matches) != re.sub(r"\s+", "", value):
        return None
    return sum(float(match.group(1)) * units[match.group(2)] for match in matches)


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


def _memory(root: Path) -> tuple[int, int]:
    values: dict[str, int] = {}
    try:
        for line in (root / "proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            values[key] = int(value.split()[0])
    except (OSError, ValueError, IndexError):
        return 0, 0
    available = round(100 * values.get("MemAvailable", 0) / max(values.get("MemTotal", 1), 1))
    swap_total = values.get("SwapTotal", 0)
    swap = round(100 * (swap_total - values.get("SwapFree", 0)) / max(swap_total, 1)) if swap_total else 0
    return available, swap


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


def collect_snapshot(values: dict[str, str], *, root: Path = Path("/"), run: Runner = _run) -> dict[str, Any]:
    docker_root = values.get("CI_FLEET_DOCKER_ROOT", "/var/lib/docker")
    available, swap = _memory(root)
    docker_ok = run(["docker", "info"]).returncode == 0
    controller_name = values.get("CI_FLEET_CONTROLLER_CONTAINER", "ci-fleet-controller-1")
    controller, effective = _container(run, controller_name) if docker_ok else ({"state": "missing", "restart_count": 0, "oom_killed": False}, {"min": 0, "max": 0})
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
    configured = {"min": int(values.get("CI_FLEET_MIN_RUNNERS", 0)), "max": int(values.get("CI_FLEET_MAX_RUNNERS", 0))}
    timer_ages = {"health": 900, "cleanup": 172800, "drift": 3600}
    timers = {name: _unit_state(run, f"ci-fleet-{name}.timer", timer=True, max_age_seconds=age) for name, age in timer_ages.items()}
    services = {name: _unit_state(run, unit) for name, unit in {
        "cleanup": "ci-fleet-cleanup.service",
        "drift": "ci-fleet-drift.service",
    }.items()}
    debian = (root / "etc/debian_version").exists()
    if debian:
        timers["updates"] = _unit_state(run, "apt-daily-upgrade.timer", timer=True, max_age_seconds=172800)
        services["updates"] = _unit_state(run, "apt-daily-upgrade.service")
    if values.get("CI_FLEET_HEALTH_BOOTSTRAP") == "1":
        # ponytail: activation validates unit installation separately; scheduled runs verify live timers after enablement.
        timers = {name: "ok" for name in timers}
    instance = values.get("CI_FLEET_INSTANCE", "unknown")
    stale = _stale_resources(run, instance) if docker_ok else {"containers": 0, "networks": 0, "volumes": 0}
    stale["images"] = _count(run, ["docker", "images", "-q", "--filter", "dangling=true", "--filter", "label=io.randomdevelopment.ci-fleet.managed=true"]) if docker_ok else 0
    stale["build_cache"] = _count(run, ["docker", "buildx", "du", "--filter", "until=168h", "--format", "json"]) if docker_ok else 0
    return {
        "controller_id": instance,
        "desired_state": values.get("CI_FLEET_CONTROLLER_STATE", "active"),
        "disks": {"root": _disk(str(root)), "docker": _disk(str(root / docker_root.lstrip("/")))},
        "memory_available_percent": available,
        "load_per_cpu": os.getloadavg()[2] / max(os.cpu_count() or 1, 1),
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
    }


def load_monitoring_config(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    info = path.stat()
    expected_owner = os.getuid() if os.environ.get("CI_FLEET_TESTING") == "1" else 0
    if info.st_uid != expected_owner or stat.S_IMODE(info.st_mode) & 0o077:
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


def _send_heartbeat(values: dict[str, str], report: dict[str, Any]) -> int:
    url = values.get("CI_FLEET_HEALTH_HEARTBEAT_URL")
    if not url:
        return 0
    if not url.startswith("https://"):
        return 2
    headers = {"Content-Type": "application/json"}
    token_file = values.get("CI_FLEET_HEALTH_HEARTBEAT_TOKEN_FILE")
    if token_file:
        path = Path(token_file)
        try:
            info = path.stat()
            if info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o077:
                return 2
            headers["Authorization"] = f"Bearer {path.read_text().strip()}"
        except OSError:
            return 2
    request = urllib.request.Request(url, data=json.dumps(report).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return 0 if 200 <= response.status < 300 else 1
    except OSError:
        return 1


def _local(args: argparse.Namespace) -> int:
    values = dict(os.environ)
    values.update(load_monitoring_config(args.monitoring_config))
    report = evaluate(collect_snapshot(values), thresholds_from(values))
    report["timestamp"] = int(time.time())
    heartbeat = _send_heartbeat(values, report)
    if heartbeat:
        severity = "critical" if heartbeat == 2 else "warning"
        report["checks"].append({"id": "heartbeat_delivery", "status": severity})
        if heartbeat > report["exit_code"]:
            report["status"], report["exit_code"] = ("warning", 1) if heartbeat == 1 else ("unhealthy", 2)
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
