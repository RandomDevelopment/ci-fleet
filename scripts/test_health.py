#!/usr/bin/env python3
import copy
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("health", ROOT / "scripts" / "health.py")
health = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = health
SPEC.loader.exec_module(health)


def healthy_snapshot():
    return {
        "controller_id": "example-ci-01",
        "desired_state": "active",
        "disks": {"root": {"used_percent": 20, "inode_used_percent": 10}, "docker": {"used_percent": 30, "inode_used_percent": 15}},
        "memory_available_percent": 75,
        "swap_used_percent": 0,
        "recent_oom": False,
        "docker_available": True,
        "controller": {"state": "running", "restart_count": 0, "oom_killed": False},
        "configured_capacity": {"min": 0, "max": 1},
        "effective_capacity": {"min": 0, "max": 1},
        "managed": {"running": 0, "inactive": 0, "unhealthy": 0, "restarting": 0},
        "stale": {"images": 0, "networks": 0, "volumes": 0, "build_cache_bytes": 0},
        "services": {"cleanup": "ok", "drift": "ok"},
        "timers": {"health": "ok", "cleanup": "ok", "drift": "ok", "updates": "ok"},
        "pending_reboot": False,
        "failed_packages": False,
        "clock_synchronized": True,
        "backup": "not_configured",
    }


def network_policy_values() -> dict[str, str]:
    return {
        "CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_COUNT": "1",
        "CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_0_BASE": "198.51.100.0/24",
        "CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_0_SIZE": "28",
        "CI_FLEET_DOCKER_NETWORKS_PER_RUNNER": "1",
        "CI_FLEET_DOCKER_NETWORK_RESERVE_SUBNETS": "1",
    }


class HealthTests(unittest.TestCase):
    def test_healthy_active_host(self) -> None:
        report = health.evaluate(healthy_snapshot(), health.Thresholds())
        self.assertEqual(report["status"], "healthy")
        self.assertEqual(report["exit_code"], 0)
        self.assertEqual(report["controller"], "example-ci-01")

    def test_disk_warning_and_critical_thresholds(self) -> None:
        warning = healthy_snapshot()
        warning["disks"]["docker"]["used_percent"] = 80
        report = health.evaluate(warning, health.Thresholds())
        self.assertEqual((report["status"], report["exit_code"]), ("warning", 1))
        self.assertIn("disk_docker", {check["id"] for check in report["checks"] if check["status"] == "warning"})

        critical = copy.deepcopy(warning)
        critical["disks"]["docker"]["used_percent"] = 90
        report = health.evaluate(critical, health.Thresholds())
        self.assertEqual((report["status"], report["exit_code"]), ("unhealthy", 2))
        self.assertIn("disk_docker", {check["id"] for check in report["checks"] if check["status"] == "critical"})

    def test_docker_network_headroom_reports_low_water_exhaustion_and_legacy_networks(self) -> None:
        healthy = health.evaluate({**healthy_snapshot(), "docker_network_headroom": {"configured": 16, "used": 2, "free": 14, "reserve": 1, "legacy": 0, "state": "healthy"}}, health.Thresholds())
        self.assertEqual(healthy["status"], "healthy")
        self.assertIn("docker_network_headroom", {check["id"] for check in healthy["checks"] if check["status"] == "ok"})

        warning = health.evaluate({**healthy_snapshot(), "docker_network_headroom": {"configured": 2, "used": 1, "free": 1, "reserve": 1, "legacy": 0, "state": "warning"}}, health.Thresholds())
        self.assertEqual((warning["status"], warning["exit_code"]), ("warning", 1))
        self.assertIn("docker_network_headroom", {check["id"] for check in warning["checks"] if check["status"] == "warning"})

        critical = health.evaluate({**healthy_snapshot(), "docker_network_headroom": {"configured": 1, "used": 1, "free": 0, "reserve": 1, "legacy": 0, "state": "critical"}}, health.Thresholds())
        self.assertEqual((critical["status"], critical["exit_code"]), ("unhealthy", 2))
        self.assertIn("docker_network_headroom", {check["id"] for check in critical["checks"] if check["status"] == "critical"})

        legacy = health.evaluate({**healthy_snapshot(), "docker_network_headroom": {"configured": 2, "used": 0, "free": 2, "reserve": 1, "legacy": 1, "state": "warning"}}, health.Thresholds())
        self.assertEqual(legacy["status"], "warning")
        self.assertIn("docker_network_legacy", {check["id"] for check in legacy["checks"] if check["status"] == "warning"})

    def test_docker_network_headroom_collection_handles_inspection_failure(self) -> None:
        def run(args):
            if args[:2] == ["docker", "info"]:
                return health.subprocess.CompletedProcess(args, 0, "", "")
            return health.subprocess.CompletedProcess(args, 1, "", "")

        snapshot = health.collect_snapshot({**network_policy_values(), "CI_FLEET_CONTROLLER_STATE": "active", "CI_FLEET_INSTANCE": "example-ci-01"}, run=run)
        self.assertEqual(snapshot["docker_network_headroom"]["state"], "unavailable")
        report = health.evaluate({**healthy_snapshot(), "docker_network_headroom": snapshot["docker_network_headroom"]}, health.Thresholds())
        self.assertEqual((report["status"], report["exit_code"]), ("unhealthy", 2))
        self.assertIn("docker_network_inspection", {check["id"] for check in report["checks"] if check["status"] == "critical"})

    def test_legacy_rendered_state_without_network_policy_remains_compatible(self) -> None:
        network = health._docker_network_headroom(lambda args: health.subprocess.CompletedProcess(args, 0, "", ""), {}, docker_ok=True)
        report = health.evaluate({**healthy_snapshot(), "docker_network_headroom": network}, health.Thresholds())
        self.assertEqual(network["state"], "not_configured")
        self.assertNotIn("docker_network_inspection", {check["id"] for check in report["checks"]})

    def test_legacy_status_report_omits_unconfigured_docker_network(self) -> None:
        for docker_ok in (True, False):
            with self.subTest(docker_ok=docker_ok):
                network = health._docker_network_headroom(
                    lambda args: health.subprocess.CompletedProcess(args, 0, "", ""), {}, docker_ok=docker_ok
                )
                snapshot = {**healthy_snapshot(), "docker_network_headroom": network}
                report = health.build_status_report(snapshot, health.evaluate(snapshot, health.Thresholds()), generated_at=1_000)
                self.assertEqual(network["state"], "not_configured")
                self.assertNotIn("network", report["docker"])

    def test_network_pool_parser_accepts_29_and_rejects_smaller_allocations(self) -> None:
        values = {
            "CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_0_BASE": "198.51.100.0/24",
        }
        values["CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_0_SIZE"] = "29"
        self.assertIsNotNone(health._parse_network_pool(values, 0))
        for size in (30, 31, 32):
            with self.subTest(size=size):
                values["CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_0_SIZE"] = str(size)
                self.assertIsNone(health._parse_network_pool(values, 0))

    def test_docker_network_headroom_collection_counts_legacy_networks(self) -> None:
        networks = {
            "managed": [{"IPAM": {"Config": [{"Subnet": "198.51.100.0/28"}]}}],
            "legacy": [{"IPAM": {"Config": [{"Subnet": "10.0.0.0/24"}]}}],
        }

        def run(args):
            if args[:2] == ["docker", "info"]:
                return health.subprocess.CompletedProcess(args, 0, "", "")
            if args[:3] == ["docker", "network", "ls"]:
                return health.subprocess.CompletedProcess(args, 0, "managed\nlegacy\n", "")
            if args[:3] == ["docker", "network", "inspect"]:
                return health.subprocess.CompletedProcess(args, 0, json.dumps(networks[args[-1]]), "")
            return health.subprocess.CompletedProcess(args, 0, "", "")

        snapshot = health.collect_snapshot({**network_policy_values(), "CI_FLEET_CONTROLLER_STATE": "active", "CI_FLEET_INSTANCE": "example-ci-01"}, run=run)
        self.assertEqual(snapshot["docker_network_headroom"], {"configured": 16, "used": 1, "free": 15, "reserve": 1, "legacy": 1, "state": "warning"})
        report = health.evaluate({**healthy_snapshot(), "docker_network_headroom": snapshot["docker_network_headroom"]}, health.Thresholds())
        self.assertEqual(report["status"], "warning")

    def test_builtin_addressless_networks_are_ignored_but_custom_ones_are_legacy(self) -> None:
        networks = {name: [{"IPAM": {"Config": []}}] for name in ("host", "none", "custom")}
        def run(args):
            if args[:3] == ["docker", "network", "ls"]:
                return health.subprocess.CompletedProcess(args, 0, "host\nnone\ncustom\n", "")
            return health.subprocess.CompletedProcess(args, 0, json.dumps(networks[args[-1]]), "")
        result = health._docker_network_headroom(run, network_policy_values(), docker_ok=True)
        self.assertEqual(result["legacy"], 1)

    def test_overlapping_builtin_bridge_consumes_allocation_without_legacy_warning(self) -> None:
        def run(args):
            if args[:3] == ["docker", "network", "ls"]:
                return health.subprocess.CompletedProcess(args, 0, "bridge\n", "")
            return health.subprocess.CompletedProcess(args, 0, json.dumps([{"IPAM": {"Config": [{"Subnet": "198.51.100.0/28"}]}}]), "")
        result = health._docker_network_headroom(run, network_policy_values(), docker_ok=True)
        self.assertEqual((result["used"], result["free"], result["legacy"], result["state"]), (1, 15, 0, "healthy"))

    def test_overlapping_builtin_bridge_warns_when_it_breaks_reviewed_capacity(self) -> None:
        values = network_policy_values()
        values.update({
            "CI_FLEET_CONFIGURED_MAX_RUNNERS": "1",
            "CI_FLEET_CONTROLLER_STATE": "active",
            "CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_0_BASE": "198.51.100.0/27",
            "CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_0_SIZE": "29",
            "CI_FLEET_DOCKER_NETWORKS_PER_RUNNER": "2",
        })
        def run(args):
            if args[:3] == ["docker", "network", "ls"]:
                return health.subprocess.CompletedProcess(args, 0, "bridge\n", "")
            return health.subprocess.CompletedProcess(args, 0, json.dumps([{"IPAM": {"Config": [{"Subnet": "198.51.100.0/29"}]}}]), "")
        result = health._docker_network_headroom(run, values, docker_ok=True)
        self.assertEqual((result["configured"], result["used"], result["free"], result["legacy"], result["state"]), (4, 1, 3, 0, "warning"))

    def test_non_overlapping_builtin_bridge_is_not_legacy(self) -> None:
        inspected = []
        def run(args):
            if args[:3] == ["docker", "network", "ls"]:
                return health.subprocess.CompletedProcess(args, 0, "bridge\n", "")
            inspected.append(args[-1])
            return health.subprocess.CompletedProcess(args, 0, json.dumps([{"IPAM": {"Config": [{"Subnet": "172.17.0.0/16"}]}}]), "")
        result = health._docker_network_headroom(run, network_policy_values(), docker_ok=True)
        self.assertEqual((result["used"], result["legacy"], result["state"]), (0, 0, "healthy"))
        self.assertEqual(inspected, ["bridge"])

    def test_similar_user_network_is_legacy(self) -> None:
        def run(args):
            if args[:3] == ["docker", "network", "ls"]:
                return health.subprocess.CompletedProcess(args, 0, "bridge-copy\n", "")
            return health.subprocess.CompletedProcess(args, 0, json.dumps([{"IPAM": {"Config": [{"Subnet": "172.17.0.0/16"}]}}]), "")
        result = health._docker_network_headroom(run, network_policy_values(), docker_ok=True)
        self.assertEqual((result["used"], result["legacy"], result["state"]), (0, 1, "warning"))

    def test_broader_network_consumes_all_allocation_slots(self) -> None:
        def run(args):
            if args[:3] == ["docker", "network", "ls"]:
                return health.subprocess.CompletedProcess(args, 0, "broad\n", "")
            return health.subprocess.CompletedProcess(args, 0, json.dumps([{"IPAM": {"Config": [{"Subnet": "198.51.100.0/24"}]}}]), "")
        result = health._docker_network_headroom(run, network_policy_values(), docker_ok=True)
        self.assertEqual((result["used"], result["free"], result["state"]), (16, 0, "critical"))

    def test_network_containing_pool_consumes_all_intersecting_slots(self) -> None:
        def run(args):
            if args[:3] == ["docker", "network", "ls"]:
                return health.subprocess.CompletedProcess(args, 0, "inverse\n", "")
            return health.subprocess.CompletedProcess(args, 0, json.dumps([{"IPAM": {"Config": [{"Subnet": "198.51.100.0/23"}]}}]), "")
        result = health._docker_network_headroom(run, network_policy_values(), docker_ok=True)
        self.assertEqual((result["used"], result["free"], result["state"]), (16, 0, "critical"))

    def test_large_pool_gap_counts_overlaps_without_materializing_slots(self) -> None:
        values = {
            "CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_COUNT": "1",
            "CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_0_BASE": "240.0.0.0/8",
            "CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_0_SIZE": "29",
            "CI_FLEET_DOCKER_NETWORK_RESERVE_SUBNETS": "1",
        }
        networks = {
            "broad": [{"IPAM": {"Config": [{"Subnet": "240.0.0.0/8"}]}}],
            "duplicate": [{"IPAM": {"Config": [{"Subnet": "240.0.0.0/9"}]}}],
        }
        def run(args):
            if args[:3] == ["docker", "network", "ls"]:
                return health.subprocess.CompletedProcess(args, 0, "broad\nduplicate\n", "")
            return health.subprocess.CompletedProcess(args, 0, json.dumps(networks[args[-1]]), "")
        result = health._docker_network_headroom(run, values, docker_ok=True)
        self.assertEqual((result["configured"], result["used"], result["free"]), (1 << 21, 1 << 21, 0))

    def test_network_removed_during_inspection_is_skipped_after_refresh(self) -> None:
        listings = iter(("vanished\nmanaged\n", "managed\n"))
        def run(args):
            if args[:3] == ["docker", "network", "ls"]:
                return health.subprocess.CompletedProcess(args, 0, next(listings), "")
            if args[-1] == "vanished":
                return health.subprocess.CompletedProcess(args, 1, "", "not found")
            return health.subprocess.CompletedProcess(args, 0, json.dumps([{"IPAM": {"Config": [{"Subnet": "198.51.100.0/28"}]}}]), "")
        result = health._docker_network_headroom(run, network_policy_values(), docker_ok=True)
        self.assertEqual((result["used"], result["state"]), (1, "healthy"))

    def test_network_inspection_error_fails_closed_when_network_still_exists(self) -> None:
        def run(args):
            if args[:3] == ["docker", "network", "ls"]:
                return health.subprocess.CompletedProcess(args, 0, "broken\n", "")
            return health.subprocess.CompletedProcess(args, 1, "", "permission denied")
        self.assertEqual(health._docker_network_headroom(run, network_policy_values(), docker_ok=True)["state"], "unavailable")

    def test_ipv6_ipam_is_ignored_without_hiding_ipv4(self) -> None:
        networks = {
            "dual": [{"IPAM": {"Config": [{"Subnet": "2001:db8::/64"}, {"Subnet": "198.51.100.0/28"}]}}],
            "v6-only": [{"IPAM": {"Config": [{"Subnet": "2001:db8:1::/64"}]}}],
        }
        def run(args):
            if args[:3] == ["docker", "network", "ls"]:
                return health.subprocess.CompletedProcess(args, 0, "dual\nv6-only\n", "")
            return health.subprocess.CompletedProcess(args, 0, json.dumps(networks[args[-1]]), "")
        result = health._docker_network_headroom(run, network_policy_values(), docker_ok=True)
        self.assertEqual((result["used"], result["legacy"], result["state"]), (1, 0, "healthy"))

    def test_malformed_ipv4_ipam_fails_inspection(self) -> None:
        def run(args):
            if args[:3] == ["docker", "network", "ls"]:
                return health.subprocess.CompletedProcess(args, 0, "broken\n", "")
            return health.subprocess.CompletedProcess(args, 0, json.dumps([{"IPAM": {"Config": [{"Subnet": "198.51.100.999/28"}]}}]), "")
        self.assertEqual(health._docker_network_headroom(run, network_policy_values(), docker_ok=True)["state"], "unavailable")

    def test_status_report_redacts_network_addresses(self) -> None:
        snapshot = {**healthy_snapshot(), "docker_network_headroom": {"configured": 16, "used": 2, "free": 14, "reserve": 1, "legacy": 1, "state": "warning"}}
        report = health.build_status_report(snapshot, health.evaluate(snapshot, health.Thresholds()), generated_at=1_000)
        self.assertEqual(report["docker"]["network"], {"configured": 16, "used": 2, "free": 14, "legacy": 1})
        self.assertNotIn("198.51.100", json.dumps(report))

    def test_health_contract_classifies_host_failures(self) -> None:
        cases = {
            "inode_root": (lambda s: s["disks"]["root"].update(inode_used_percent=90), "unhealthy"),
            "memory": (lambda s: s.update(memory_available_percent=8), "unhealthy"),
            "load": (lambda s: s.update(load_per_cpu=1.5), "unhealthy"),
            "swap": (lambda s: s.update(swap_used_percent=25), "warning"),
            "oom": (lambda s: s.update(recent_oom=True), "unhealthy"),
            "docker": (lambda s: s.update(docker_available=False), "unhealthy"),
            "controller": (lambda s: s["controller"].update(state="exited"), "unhealthy"),
            "restarts": (lambda s: s["controller"].update(restart_count=3), "warning"),
            "capacity": (lambda s: s.update(effective_capacity={"min": 0, "max": 0}), "unhealthy"),
            "managed_unhealthy": (lambda s: s["managed"].update(unhealthy=1), "unhealthy"),
            "stale_volume": (lambda s: s["stale"].update(volumes=1), "warning"),
            "drift": (lambda s: s["services"].update(drift="failed"), "unhealthy"),
            "timer_cleanup": (lambda s: s["timers"].update(cleanup="stale"), "warning"),
            "updates": (lambda s: s.update(failed_packages=True), "unhealthy"),
            "reboot": (lambda s: s.update(pending_reboot=True), "warning"),
            "clock": (lambda s: s.update(clock_synchronized=False), "warning"),
            "backup": (lambda s: s.update(backup="failed"), "warning"),
        }
        for check_id, (mutate, expected) in cases.items():
            with self.subTest(check_id=check_id):
                snapshot = healthy_snapshot()
                mutate(snapshot)
                report = health.evaluate(snapshot, health.Thresholds())
                self.assertEqual(report["status"], expected)
                self.assertIn(check_id, {check["id"] for check in report["checks"] if check["status"] != "ok"})

    def test_drained_host_is_maintenance_not_unhealthy(self) -> None:
        snapshot = healthy_snapshot()
        snapshot["desired_state"] = "drained"
        snapshot["controller"]["state"] = "missing"
        snapshot["effective_capacity"] = {"min": 0, "max": 0}
        report = health.evaluate(snapshot, health.Thresholds())
        self.assertEqual((report["status"], report["exit_code"]), ("maintenance", 0))

    def test_remote_reconciliation_failure_is_unhealthy(self) -> None:
        snapshot = healthy_snapshot()
        snapshot["reconciliation"] = {
            "status": "rolled_back",
            "desired_commit": "1" * 40,
            "applied_commit": "2" * 40,
            "health": "healthy",
        }
        report = health.evaluate(snapshot, health.Thresholds())
        self.assertEqual((report["status"], report["exit_code"]), ("unhealthy", 2))
        self.assertEqual(next(check for check in report["checks"] if check["id"] == "reconciliation")["status"], "critical")

        snapshot["reconciliation"] = {"status": "missing", "desired_commit": "", "applied_commit": "", "health": ""}
        report = health.evaluate(snapshot, health.Thresholds())
        self.assertEqual((report["status"], report["exit_code"]), ("warning", 1))

    def test_malformed_reconciliation_state_is_observable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text('{"status":[],"health":{},"desired_commit":[],"applied_commit":null}\n')
            reconciliation = health._reconcile_state(path)
            self.assertEqual(
                reconciliation,
                {"status": "invalid", "desired_commit": "invalid", "applied_commit": "invalid", "health": "invalid", "last_success_at": None},
            )
            snapshot = healthy_snapshot()
            snapshot["controller_id"] = "example-ci-01"
            snapshot["reconciliation"] = reconciliation
            report = health.build_status_report(snapshot, health.evaluate(snapshot, health.Thresholds()), generated_at=1_000)
            self.assertEqual(report["configuration"], {"desired_commit": "", "applied_commit": ""})
            self.assertEqual(report["error"]["code"], "reconciliation_invalid")

    def test_external_heartbeats_detect_missing_and_stale_active_hosts(self) -> None:
        controllers = {
            "fresh": {"state": "active", "lifecycle": "stable"},
            "stale": {"state": "active", "lifecycle": "stable"},
            "missing": {"state": "active", "lifecycle": "stable"},
            "drained": {"state": "drained", "lifecycle": "stable"},
            "retired": {"state": "disabled", "lifecycle": "retiring"},
        }
        records = {
            "fresh": {"controller": "fresh", "timestamp": 980, "status": "warning"},
            "stale": {"controller": "stale", "timestamp": 800, "status": "healthy"},
        }
        report = health.evaluate_heartbeats(controllers, records, now=1000, grace_seconds=60)
        states = {host["controller"]: host["status"] for host in report["hosts"]}
        self.assertEqual(states, {
            "fresh": "warning",
            "stale": "missing",
            "missing": "missing",
            "drained": "maintenance",
            "retired": "retired",
        })
        self.assertEqual((report["status"], report["exit_code"]), ("unhealthy", 2))
        warning = health.evaluate_heartbeats({"fresh": controllers["fresh"]}, {"fresh": records["fresh"]}, now=1000, grace_seconds=60)
        self.assertEqual((warning["status"], warning["exit_code"]), ("warning", 1))
        future = health.evaluate_heartbeats({"fresh": controllers["fresh"]}, {"fresh": {"controller": "fresh", "timestamp": 2000, "status": "healthy"}}, now=1000, grace_seconds=60)
        self.assertEqual((future["status"], future["hosts"][0]["status"]), ("unhealthy", "missing"))
        wrong = health.evaluate_heartbeats({"fresh": controllers["fresh"]}, {"fresh": {"controller": "other", "timestamp": 980, "status": "healthy"}}, now=1000, grace_seconds=60)
        self.assertEqual((wrong["status"], wrong["hosts"][0]["status"]), ("unhealthy", "missing"))

    def test_collector_uses_sustained_metrics_and_all_service_units(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "proc/pressure").mkdir(parents=True)
            (root / "proc/meminfo").write_text(
                "MemTotal: 100 kB\nMemAvailable: 75 kB\nSwapTotal: 100 kB\nSwapFree: 50 kB\n"
            )
            pressure = root / "proc/pressure/memory"
            pressure.write_text("some avg10=0.00 avg60=0.05 avg300=0.20 total=1\n")
            def run(args):
                output = "yes\n" if args[0] == "timedatectl" else "failed\n" if args[:2] == ["systemctl", "show"] and args[2].endswith(".service") else "success\n" if args[0] == "systemctl" else ""
                return health.subprocess.CompletedProcess(args, 1 if args[:2] == ["docker", "info"] else 0, output, "")

            original_load, original_cpus = health.os.getloadavg, health.os.cpu_count
            health.os.getloadavg, health.os.cpu_count = lambda: (1.0, 2.0, 6.0), lambda: 2
            try:
                snapshot = health.collect_snapshot(
                    {"CI_FLEET_INSTANCE": "example", "CI_FLEET_CONTROLLER_STATE": "disabled", "CI_FLEET_HEALTH_BOOTSTRAP": "1"},
                    root=root,
                    run=run,
                )
                self.assertEqual((snapshot["load_per_cpu"], snapshot["swap_used_percent"]), (3.0, 50))
                self.assertEqual(set(snapshot["services"]), {"cleanup", "drift"})
                self.assertEqual(set(snapshot["timers"]), {"health", "cleanup", "drift"})
                (root / "var/lib/ci-fleet/reconcile").mkdir(parents=True)
                (root / "var/lib/ci-fleet/reconcile/state.json").write_text('{"status":"rolled_back","desired_commit":"","applied_commit":"","health":"healthy"}\n')
                remote = health.collect_snapshot(
                    {
                        "CI_FLEET_CONTROLLER_STATE": "disabled",
                        "CI_FLEET_CONFIG_REPOSITORY": "example/config",
                        "CI_FLEET_HEALTH_BOOTSTRAP": "1",
                    },
                    root=root,
                    run=run,
                )
                self.assertEqual(set(remote["services"]), {"cleanup", "drift", "reconcile"})
                self.assertEqual(set(remote["services"].values()), {"ok"})
                self.assertEqual(set(remote["timers"]), {"health", "cleanup", "drift", "reconcile"})
                self.assertEqual(remote["reconciliation"]["status"], "bootstrap")
                (root / "etc").mkdir()
                (root / "etc/debian_version").write_text("13\n")
                debian = health.collect_snapshot({"CI_FLEET_CONTROLLER_STATE": "disabled", "CI_FLEET_HEALTH_BOOTSTRAP": "1"}, root=root, run=run)
                self.assertIn("updates", debian["services"])
                self.assertIn("updates", debian["timers"])
                pressure.write_text("some avg10=0.00 avg60=0.00 avg300=0.00 total=1\n")
                self.assertEqual(health.collect_snapshot({"CI_FLEET_CONTROLLER_STATE": "disabled", "CI_FLEET_HEALTH_BOOTSTRAP": "1"}, root=root, run=run)["swap_used_percent"], 0)
                pressure.unlink()
                self.assertEqual(health.collect_snapshot({"CI_FLEET_CONTROLLER_STATE": "disabled", "CI_FLEET_HEALTH_BOOTSTRAP": "1"}, root=root, run=run)["swap_used_percent"], 50)
            finally:
                health.os.getloadavg, health.os.cpu_count = original_load, original_cpus

    def test_collector_builds_status_metrics_and_uses_controller_runner_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "proc").mkdir()
            (root / "proc/stat").write_text("cpu  100 0 50 800 50 0 0 0 40 10\nbtime 900\n")
            (root / "proc/meminfo").write_text("MemTotal: 1024 kB\nMemAvailable: 768 kB\nSwapTotal: 512 kB\nSwapFree: 384 kB\n")
            generated_at = [int(health.time.time())]

            def run(args):
                if args[:3] == ["docker", "exec", "controller"]:
                    return health.subprocess.CompletedProcess(args, 0, json.dumps({
                        "controller": "example-ci-01", "software_version": "1" * 40,
                        "current": 2, "busy": 1, "maximum": 6, "generated_at": generated_at[0],
                    }), "")
                if args[:2] == ["docker", "info"]:
                    return health.subprocess.CompletedProcess(args, 0, "", "")
                if args[:2] == ["docker", "inspect"]:
                    outputs = {"{{.State.Status}}": "running\n", "{{.State.OOMKilled}}": "false\n", "{{.RestartCount}}": "0\n", "{{range .Config.Env}}{{println .}}{{end}}": "CI_FLEET_MIN_RUNNERS=0\nCI_FLEET_MAX_RUNNERS=6\n"}
                    return health.subprocess.CompletedProcess(args, 0, outputs.get(args[3], ""), "")
                if args[:2] == ["systemctl", "is-enabled"] and args[-1] in {"ssh.service", "ssh.socket", "sshd.service"}:
                    return health.subprocess.CompletedProcess(args, 1, "disabled\n", "")
                if args[:2] == ["systemctl", "is-active"] and args[-1] in {"ssh.service", "ssh.socket", "sshd.service"}:
                    return health.subprocess.CompletedProcess(args, 3, "inactive\n", "")
                return health.subprocess.CompletedProcess(args, 0, "success\n", "")

            snapshot = health.collect_snapshot({
                "CI_FLEET_INSTANCE": "example-ci-01", "CI_FLEET_CONTROLLER_CONTAINER": "controller",
                "CI_FLEET_MAX_RUNNERS": "6", "CI_FLEET_HEALTH_BOOTSTRAP": "1",
            }, root=root, run=run)
            self.assertEqual(snapshot["runners"], {"current": 2, "busy": 1, "maximum": 6})
            self.assertEqual(snapshot["software_version"], "1" * 40)
            self.assertEqual(snapshot["boot_time"], 900)
            self.assertEqual(snapshot["ssh"], "disabled")
            self.assertEqual(snapshot["memory"], {"total_bytes": 1048576, "available_bytes": 786432})
            self.assertEqual(snapshot["swap"], {"total_bytes": 524288, "used_bytes": 131072})
            self.assertAlmostEqual(snapshot["cpu"]["used_percent"], 20.0)
            generated_at[0] -= 121
            self.assertEqual(health._controller_status(run, "controller", "example-ci-01", 6), ({"current": 0, "busy": 0, "maximum": 6}, "unknown", False))
            stale = health.collect_snapshot({
                "CI_FLEET_INSTANCE": "example-ci-01", "CI_FLEET_CONTROLLER_CONTAINER": "controller",
                "CI_FLEET_MAX_RUNNERS": "6", "CI_FLEET_HEALTH_BOOTSTRAP": "1",
            }, root=root, run=run)
            self.assertFalse(stale["controller_status_valid"])
            stale_report = health.evaluate(stale, health.Thresholds())
            self.assertEqual(next(check for check in stale_report["checks"] if check["id"] == "controller_status")["status"], "warning")
            outbound = health.build_status_report(stale, stale_report, generated_at=int(health.time.time()))
            self.assertEqual(outbound["error"]["code"], "health_controller_status")

    def test_threshold_overrides_validate_ordering(self) -> None:
        self.assertAlmostEqual(health._timespan_seconds("3d 1h 41min 40.5s"), 265300.5)
        self.assertAlmostEqual(health._timespan_seconds("1y 2month 3w 4d 5h 6min 7.5s"), 365.25 * 86400 + 2 * 365.25 * 86400 / 12 + 3 * 7 * 86400 + 4 * 86400 + 5 * 3600 + 6 * 60 + 7.5)
        thresholds = health.thresholds_from({"CI_FLEET_HEALTH_DISK_WARN_PERCENT": "70", "CI_FLEET_HEALTH_DISK_CRITICAL_PERCENT": "85"})
        self.assertEqual((thresholds.disk_warn_percent, thresholds.disk_critical_percent), (70, 85))
        with self.assertRaisesRegex(ValueError, "disk thresholds"):
            health.thresholds_from({"CI_FLEET_HEALTH_DISK_WARN_PERCENT": "90", "CI_FLEET_HEALTH_DISK_CRITICAL_PERCENT": "80"})

    def test_human_output_is_redacted(self) -> None:
        report = health.evaluate(healthy_snapshot(), health.Thresholds())
        report["private_token"] = "SHOULD_NOT_PRINT"
        output = health.render_human(report)
        self.assertIn("HEALTHY controller=example-ci-01", output)
        self.assertNotIn("SHOULD_NOT_PRINT", output)
        self.assertEqual(health._send_status({"CI_FLEET_HEALTH_STATUS_URL": "http://unsafe.invalid"}, {"controller": {"id": "example"}}), 1)

    def test_probe_failures_are_results_and_missing_units_fail(self) -> None:
        for error in (FileNotFoundError(), health.subprocess.TimeoutExpired(["probe"], 30)):
            original = health.subprocess.run
            health.subprocess.run = lambda *args, **kwargs: (_ for _ in ()).throw(error)
            try:
                self.assertNotEqual(health._run(["probe"]).returncode, 0)
            finally:
                health.subprocess.run = original

        def missing(args):
            return health.subprocess.CompletedProcess(args, 1, "", "")

        self.assertEqual(health._unit_state(missing, "missing.service"), "failed")

    def test_controller_runtime_directory_is_writable_by_nonroot_process(self) -> None:
        dockerfile = (ROOT / "controller/Dockerfile").read_text()
        self.assertIn("install -d -o 65532 -g 65532 /run/ci-fleet", dockerfile)
        self.assertIn("USER 65532:65532", dockerfile)

    def test_status_report_contract_redaction_and_disabled_ssh(self) -> None:
        snapshot = healthy_snapshot()
        snapshot.update({
            "software_version": "1" * 40,
            "boot_time": 900,
            "ssh": "disabled",
            "cpu": {"logical": 8, "used_percent": 25.0},
            "memory": {"total_bytes": 1024, "available_bytes": 768},
            "swap": {"total_bytes": 512, "used_bytes": 0},
            "load": {"one": 0.1, "five": 0.2, "fifteen": 0.3},
            "runners": {"current": 1, "busy": 1, "maximum": 6},
            "reconciliation": {
                "status": "failed", "desired_commit": "2" * 40, "applied_commit": "3" * 40,
                "health": "unhealthy", "last_success_at": 950,
                "message": "token=SUPER_SECRET https://private.invalid/path",
            },
        })
        snapshot["controller"]["state"] = "dead"
        for disk in snapshot["disks"].values():
            disk.update(total_bytes=4096, used_bytes=1024, inode_total=1000, inode_used=100)
        report = health.build_status_report(snapshot, health.evaluate(snapshot, health.Thresholds()), generated_at=1_000)
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["controller"]["ssh"], "disabled")
        self.assertEqual(report["configuration"], {"desired_commit": "2" * 40, "applied_commit": "3" * 40})
        self.assertEqual(report["runners"], {"current": 1, "busy": 1, "maximum": 6})
        self.assertEqual(report["process"]["state"], "unknown")
        self.assertEqual(report["error"], {"code": "reconciliation_failed", "message": "reconciliation failed"})
        priority = copy.deepcopy(snapshot)
        priority["reconciliation"]["status"] = "converged"
        priority["controller_status_valid"] = True
        prioritized = health.build_status_report(priority, {"checks": [
            {"id": "early_warning", "status": "warning"},
            {"id": "later_failure", "status": "critical"},
        ]}, generated_at=1_000)
        self.assertEqual(prioritized["error"]["code"], "health_later_failure")
        snapshot["reconciliation"]["last_success_at"] = 1_001
        future_success = health.build_status_report(snapshot, health.evaluate(snapshot, health.Thresholds()), generated_at=1_000)
        self.assertIsNone(future_success["reconciliation"]["last_success_at"])
        snapshot.update({"desired_state": "disabled", "controller_status_valid": False})
        snapshot["controller"]["state"] = "exited"
        snapshot["reconciliation"].update({"status": "converged", "last_success_at": 900})
        maintenance = health.build_status_report(snapshot, health.evaluate(snapshot, health.Thresholds()), generated_at=1_000)
        self.assertNotEqual(maintenance["error"]["code"], "health_controller_status")
        encoded = json.dumps(report)
        self.assertNotIn("SUPER_SECRET", encoded)
        self.assertNotIn("private.invalid", encoded)

    def test_ssh_state_requires_service_and_socket_to_be_disabled(self) -> None:
        def disabled(args):
            if args[:2] == ["systemctl", "is-enabled"]:
                return health.subprocess.CompletedProcess(args, 1, "disabled\n", "")
            if args[:2] == ["systemctl", "is-active"]:
                return health.subprocess.CompletedProcess(args, 3, "inactive\n", "")
            return health.subprocess.CompletedProcess(args, 1, "", "")

        self.assertEqual(health._ssh_state(disabled), "disabled")

        def socket_enabled(args):
            active = args[-1] == "ssh.socket"
            return health.subprocess.CompletedProcess(args, 0 if active else 1, "active\n" if active else "inactive\n", "")

        self.assertEqual(health._ssh_state(socket_enabled), "enabled")

        def sshd_enabled(args):
            active = args[-1] == "sshd.service"
            return health.subprocess.CompletedProcess(args, 0 if active else 1, "active\n" if active else "not-found\n", "")

        self.assertEqual(health._ssh_state(sshd_enabled), "enabled")
        self.assertEqual(health._ssh_state(lambda args: health.subprocess.CompletedProcess(args, 127, "", "")), "unknown")

    def test_legacy_configuration_failure_remains_critical(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "health.json"
            old_collect, old_send = getattr(health, "collect_snapshot"), getattr(health, "_send_heartbeat")
            old_status_url = os.environ.pop("CI_FLEET_HEALTH_STATUS_URL", None)
            setattr(health, "collect_snapshot", lambda _values: healthy_snapshot())
            setattr(health, "_send_heartbeat", lambda _values, _report: 2)
            try:
                result = health._local(health.argparse.Namespace(
                    monitoring_config=Path(directory) / "missing.env", output=output, json=True,
                ))
                report = json.loads(output.read_text())
                self.assertEqual((result, report["status"]), (2, "unhealthy"))
                self.assertEqual(report["checks"][-1], {"id": "status_delivery", "status": "critical"})
            finally:
                setattr(health, "collect_snapshot", old_collect)
                setattr(health, "_send_heartbeat", old_send)
                if old_status_url is not None:
                    os.environ["CI_FLEET_HEALTH_STATUS_URL"] = old_status_url

    def test_status_delivery_is_signed_and_outage_is_non_disruptive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / "status.key"
            key.write_text("controller-key-32-bytes-long-0001\n")
            key.chmod(0o600)
            values = {
                "CI_FLEET_HEALTH_STATUS_URL": "https://status.example.invalid/v1/status",
                "CI_FLEET_HEALTH_STATUS_KEY_FILE": str(key),
            }
            report = {"controller": {"id": "example-ci-01"}, "generated_at": 1_000}
            captured = {}

            class Response:
                status = 202
                def __enter__(self): return self
                def __exit__(self, *_): return None

            def opener(request, timeout):
                captured.update(url=request.full_url, headers=dict(request.header_items()), body=request.data, timeout=timeout)
                return Response()

            old = os.environ.get("CI_FLEET_TESTING")
            os.environ["CI_FLEET_TESTING"] = "1"
            try:
                self.assertEqual(health._send_status(values, report, now=1_000, nonce="a" * 32, opener=opener), 0)
                self.assertIn("CI-Fleet-HMAC-SHA256", captured["headers"]["Authorization"])
                expected = health.sign_headers("example-ci-01", captured["body"], key.read_bytes(), timestamp=1_000, nonce="a" * 32)
                self.assertEqual(captured["headers"]["Authorization"], expected["Authorization"])
                original = copy.deepcopy(report)
                self.assertEqual(health._send_status(values, report, now=1_000, nonce="b" * 32, opener=lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError())), 1)
                self.assertEqual(health._send_status(values, report, now=1_000, nonce="c" * 32, opener=lambda *_args, **_kwargs: (_ for _ in ()).throw(health.http.client.BadStatusLine("bad"))), 1)
                self.assertEqual(report, original)
                self.assertEqual(health._send_status({"CI_FLEET_HEALTH_STATUS_URL": "http://unsafe.invalid"}, report), 1)
                self.assertEqual(health._send_status({"CI_FLEET_HEALTH_STATUS_URL": "https://[bad/v1/status"}, report), 1)
                self.assertEqual(health._send_status({"CI_FLEET_HEALTH_STATUS_URL": "https://status.example.invalid:bad/v1/status"}, report), 1)
                legacy = {"CI_FLEET_HEALTH_HEARTBEAT_URL": "https://legacy.invalid"}
                self.assertEqual(health._send_status(legacy, report), 0)
                self.assertEqual(health._send_heartbeat(legacy, report, opener=opener), 0)
                self.assertIsNone(health._NoRedirect().redirect_request(None, None, 302, None, {}, None))
            finally:
                if old is None:
                    os.environ.pop("CI_FLEET_TESTING", None)
                else:
                    os.environ["CI_FLEET_TESTING"] = old

    def test_required_status_reporting_fails_closed_without_host_local_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "health.json"
            monitoring = Path(directory) / "monitoring.env"
            monitoring.write_text("CI_FLEET_HEALTH_SUPPRESS_DELIVERY=1\n")
            monitoring.chmod(0o600)
            old_collect = health.collect_snapshot
            old_required = os.environ.get("CI_FLEET_STATUS_REPORTING_REQUIRED")
            old_testing = os.environ.get("CI_FLEET_TESTING")
            os.environ["CI_FLEET_STATUS_REPORTING_REQUIRED"] = "1"
            os.environ["CI_FLEET_TESTING"] = "1"
            setattr(health, "collect_snapshot", lambda _values: healthy_snapshot())
            try:
                result = health._local(health.argparse.Namespace(
                    monitoring_config=monitoring, output=output, json=True,
                ))
                report = json.loads(output.read_text())
                self.assertEqual(result, 1)
                self.assertEqual(report["checks"][-1], {"id": "status_delivery", "status": "warning"})
            finally:
                setattr(health, "collect_snapshot", old_collect)
                if old_required is None:
                    os.environ.pop("CI_FLEET_STATUS_REPORTING_REQUIRED", None)
                else:
                    os.environ["CI_FLEET_STATUS_REPORTING_REQUIRED"] = old_required
                if old_testing is None:
                    os.environ.pop("CI_FLEET_TESTING", None)
                else:
                    os.environ["CI_FLEET_TESTING"] = old_testing

    def test_required_status_reporting_redacts_invalid_host_local_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "health.json"
            monitoring = Path(directory) / "monitoring.env"
            old_collect = health.collect_snapshot
            old_required = os.environ.get("CI_FLEET_STATUS_REPORTING_REQUIRED")
            old_testing = os.environ.get("CI_FLEET_TESTING")
            os.environ["CI_FLEET_STATUS_REPORTING_REQUIRED"] = "1"
            os.environ["CI_FLEET_TESTING"] = "1"
            setattr(health, "collect_snapshot", lambda _values: healthy_snapshot())
            try:
                for contents, mode in (
                    ("not-an-env-line\n", 0o600),
                    ("CI_FLEET_HEALTH_STATUS_URL=https://example.invalid/v1/status\n", 0o644),
                    ("CI_FLEET_HEALTH_DISK_WARN_PERCENT=abc\n", 0o600),
                ):
                    monitoring.write_text(contents)
                    monitoring.chmod(mode)
                    result = health._local(health.argparse.Namespace(
                        monitoring_config=monitoring, output=output, json=True,
                    ))
                    report = json.loads(output.read_text())
                    self.assertEqual(result, 1)
                    self.assertEqual(report["checks"][-1], {"id": "status_delivery", "status": "warning"})
                    self.assertNotIn("example.invalid", output.read_text())
            finally:
                setattr(health, "collect_snapshot", old_collect)
                if old_required is None:
                    os.environ.pop("CI_FLEET_STATUS_REPORTING_REQUIRED", None)
                else:
                    os.environ["CI_FLEET_STATUS_REPORTING_REQUIRED"] = old_required
                if old_testing is None:
                    os.environ.pop("CI_FLEET_TESTING", None)
                else:
                    os.environ["CI_FLEET_TESTING"] = old_testing

    def test_expired_active_resources_and_stopped_capacity_are_observable(self) -> None:
        cleanup = "KEEP container runner state=running expired=1 (routine cleanup never removes active containers)\nWOULD_REMOVE volume old expired=1\n"
        run = lambda args: health.subprocess.CompletedProcess(args, 0, cleanup, "")
        self.assertEqual(health._stale_resources(run, "example"), {"containers": 1, "networks": 0, "volumes": 1})

        def stopped(args):
            outputs = {
                "{{.State.Status}}": "exited\n",
                "{{.State.OOMKilled}}": "false\n",
                "{{.RestartCount}}": "0\n",
                "{{range .Config.Env}}{{println .}}{{end}}": "CI_FLEET_MIN_RUNNERS=1\nCI_FLEET_MAX_RUNNERS=2\n",
            }
            return health.subprocess.CompletedProcess(args, 0, outputs.get(args[3], ""), "")

        controller, capacity = health._container(stopped, "controller")
        self.assertEqual((controller["state"], capacity), ("exited", {"min": 0, "max": 0}))


if __name__ == "__main__":
    unittest.main()
