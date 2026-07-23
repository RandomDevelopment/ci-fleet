#!/usr/bin/env python3
import copy
import importlib.util
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
                output = "yes\n" if args[0] == "timedatectl" else "success\n" if args[0] == "systemctl" else ""
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
        self.assertEqual(health._send_heartbeat({"CI_FLEET_HEALTH_HEARTBEAT_URL": "http://unsafe.invalid"}, report), 2)

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
