#!/usr/bin/env python3
"""Regression coverage for scoped network cleanup."""

import copy
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import shlex
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import cleanup_networks as cleanup


POLICY = {
    "CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_COUNT": "1",
    "CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_0_BASE": "198.51.100.0/24",
    "CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_0_SIZE": "29",
}
BOOT_ID = "11111111-1111-4111-8111-111111111111"


def network(name="job", subnet="198.51.100.0/29", *, labels=None, containers=None, options=None):
    return {
        "Name": name,
        "Created": "2000-01-01T00:00:00.000000001Z",
        "Labels": labels or {},
        "Containers": containers or {},
        "Options": options or {},
        "IPAM": {"Config": [{"Subnet": subnet}]},
    }


class CleanupNetworksTests(unittest.TestCase):
    def run_cleanup(self, inventory, *, apply=True, policy=None, instance="example", attach_on_recheck=False, container_references=None, disappeared_on_recheck=None, attach_on_remove=None, remove_failures=None, root=None, active_containers=None, attempt_create_on_remove=False, check_wait_unlock=False, swarm_state="inactive"):
        remaining = copy.deepcopy(inventory)
        calls = []
        inspections = {}
        failures = copy.deepcopy(remove_failures or {})
        swarm_states = copy.deepcopy(swarm_state)

        def docker(*args):
            calls.append(args)
            if args[0] == "info":
                if isinstance(swarm_states, list):
                    return swarm_states.pop(0) if len(swarm_states) > 1 else swarm_states[0]
                return swarm_states
            if args[:3] == ("ps", "-aq", "--no-trunc"):
                return "\n".join(active_containers or {})
            if args[0] == "inspect":
                return json.dumps([active_containers[args[1]]])
            if args[:2] == ("network", "ls"):
                return "\n".join(remaining)
            if args[:3] == ("ps", "-aq", "--filter"):
                self.assertTrue(args[3].startswith("network="))
                network_id = args[3].removeprefix("network=")
                self.assertIn(network_id, remaining)
                self.assertEqual(args[4:], ("--filter", f"network={remaining[network_id]['Name']}"))
                references = {network_id, remaining[network_id]["Name"]}
                return "referencing-container\n" if references.intersection(container_references or set()) else ""
            if args[:2] == ("network", "inspect"):
                network_id = args[2]
                inspections[network_id] = inspections.get(network_id, 0) + 1
                if network_id == disappeared_on_recheck and inspections[network_id] == 2:
                    del remaining[network_id]
                    raise cleanup.subprocess.CalledProcessError(1, ["docker", *args])
                if attach_on_recheck and inspections[network_id] == 2:
                    remaining[network_id]["Containers"] = {"new-container": {}}
                return json.dumps([remaining[network_id]])
            self.assertEqual(args[:2], ("network", "rm"))
            if attempt_create_on_remove:
                with (Path(cleanup.host_path("/run/lock/ci-fleet/docker-maintenance.lock"))).open("r+") as contender:
                    with self.assertRaises(BlockingIOError):
                        cleanup.fcntl.flock(contender, cleanup.fcntl.LOCK_SH | cleanup.fcntl.LOCK_NB)
                self.assertTrue(cleanup.pending_requests("removals"))
            if failures.get(args[2]):
                raise cleanup.subprocess.CalledProcessError(1, ["docker", *args], stderr=failures[args[2]].pop(0))
            if args[2] == attach_on_remove:
                remaining[args[2]]["Containers"] = {"new-container": {}}
                raise cleanup.subprocess.CalledProcessError(1, ["docker", *args], stderr="Error response from daemon: network has active endpoints")
            self.assertFalse(remaining[args[2]]["Containers"])
            del remaining[args[2]]
            return ""

        output = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(root or temporary)
            boot = root / "proc/sys/kernel/random/boot_id"
            boot.parent.mkdir(parents=True, exist_ok=True)
            boot.write_text(BOOT_ID)
            environment = {**(POLICY if policy is None else policy), "CI_FLEET_ROOT_PREFIX": str(root)}
            def sleep(_seconds):
                if check_wait_unlock:
                    with (root / "run/lock/ci-fleet/docker-maintenance.lock").open("r+") as lock:
                        cleanup.fcntl.flock(lock, cleanup.fcntl.LOCK_SH | cleanup.fcntl.LOCK_NB)

            with patch.dict(os.environ, environment, clear=True), patch.object(cleanup, "docker", docker), patch.object(cleanup.time, "sleep", sleep), redirect_stdout(output):
                cleanup.cleanup_networks(apply=apply, instance=instance)
        return remaining, calls, output.getvalue()

    def test_empty_unlabeled_pool_networks_are_removed_and_active_and_legacy_remain(self):
        inventory = {
            "empty": network(),
            "active": network("active", "198.51.100.8/29", containers={"running": {}}),
            "legacy": network("legacy", "192.0.2.0/24"),
            "controller": network("ci-fleet_default"),
            "custom-controller": network("custom-controller", labels={"com.docker.compose.project": "ci-fleet"}),
            "bridge": network("bridge"),
            "renamed-bridge": network("default-bridge", options={"com.docker.network.bridge.default_bridge": "true"}),
        }
        remaining, calls, output = self.run_cleanup(inventory)
        self.assertEqual(set(remaining), set(inventory) - {"empty"})
        self.assertIn("REMOVE network job empty-default-address-pool", output)
        self.assertIn("REPORT network legacy outside-cleanup-scope", output)
        self.assertNotIn(("network", "prune"), calls)

    def test_dry_run_reports_candidates_without_mutation(self):
        inventory = {"empty": network()}
        remaining, calls, output = self.run_cleanup(inventory, apply=False)
        self.assertEqual(remaining, inventory)
        self.assertFalse(any(call[:2] == ("network", "rm") for call in calls))
        self.assertIn("WOULD_REMOVE network job empty-default-address-pool", output)

    def test_final_removal_blocks_a_new_created_container_reference(self):
        remaining, _, _ = self.run_cleanup({"old": network()}, attempt_create_on_remove=True)
        self.assertEqual(remaining, {})

    def test_active_work_defers_cleanup_and_releases_maintenance_lock(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(cleanup, "IDLE_WAIT_SECONDS", 0):
            root = Path(temporary)
            active = {"job": {"State": {"Status": "running"}, "Config": {"Labels": {}}, "Mounts": []}}
            remaining, calls, output = self.run_cleanup({"old": network()}, root=root, active_containers=active)
            self.assertEqual(set(remaining), {"old"})
            self.assertFalse(any(call[:2] == ("network", "rm") for call in calls))
            self.assertIn("DEFER network cleanup active-work-or-maintenance-timeout", output)
            with (root / "run/lock/ci-fleet/docker-maintenance.lock").open("r+") as lock:
                cleanup.fcntl.flock(lock, cleanup.fcntl.LOCK_SH | cleanup.fcntl.LOCK_NB)

    def test_cleanup_waits_without_holding_exclusive_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checks = []

            def active_work():
                checks.append(True)
                return len(checks) == 1

            with patch.object(cleanup, "active_work", active_work):
                remaining, _, _ = self.run_cleanup({"old": network()}, root=root, check_wait_unlock=True)
            self.assertEqual(remaining, {})
            self.assertEqual(len(checks), 3)

    def test_installer_lock_defers_network_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "run").mkdir()
            with (root / "run/ci-fleet-installer.lock").open("w+") as lock:
                cleanup.fcntl.flock(lock, cleanup.fcntl.LOCK_EX)
                remaining, calls, output = self.run_cleanup({"old": network()}, root=root)
            self.assertEqual(set(remaining), {"old"})
            self.assertFalse(any(call[:2] == ("network", "rm") for call in calls))
            self.assertIn("DEFER network cleanup installer-active", output)

    def test_swarm_or_unknown_daemon_state_defers_network_cleanup(self):
        for state in ("active", "pending", "error", ""):
            with self.subTest(state=state):
                remaining, calls, output = self.run_cleanup({"old": network()}, swarm_state=state)
                self.assertEqual(set(remaining), {"old"})
                self.assertFalse(any(call[:2] == ("network", "rm") for call in calls))
                self.assertIn("unsupported-or-unknown-swarm-state", output)

    def test_swarm_activation_between_idle_checks_defers_removal(self):
        remaining, calls, output = self.run_cleanup({"old": network()}, swarm_state=["inactive", "active"])
        self.assertEqual(set(remaining), {"old"})
        self.assertEqual(sum(call[0] == "info" for call in calls), 2)
        self.assertFalse(any(call[:2] == ("network", "rm") for call in calls))
        self.assertIn("unsupported-or-unknown-swarm-state", output)

    def test_current_boot_and_unknown_request_markers_defer_cleanup(self):
        for kind in ("inflight", "removals"):
            for entry in (f"{BOOT_ID}/interrupted.json", "unknown/marker", "unexpected-file"):
                with self.subTest(kind=kind, entry=entry), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    marker = root / "run/lock/ci-fleet" / kind / entry
                    marker.parent.mkdir(mode=0o700, parents=True)
                    marker.touch(mode=0o600)
                    remaining, calls, output = self.run_cleanup({"old": network()}, root=root)
                    self.assertEqual(set(remaining), {"old"})
                    self.assertFalse(any(call[:2] == ("network", "rm") for call in calls))
                    self.assertIn("DEFER network cleanup unresolved-Docker-request", output)
                    self.assertTrue(marker.exists())

    def test_old_boot_markers_are_cleared_before_reclaiming_networks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old_boot = "22222222-2222-4222-8222-222222222222"
            for kind in ("inflight", "removals"):
                directory = root / "run/lock/ci-fleet" / kind / old_boot
                directory.mkdir(mode=0o700, parents=True)
                (directory / "interrupted.json").touch(mode=0o600)
            remaining, _, _ = self.run_cleanup({"old": network()}, root=root)
            self.assertEqual(remaining, {})
            for kind in ("inflight", "removals"):
                self.assertFalse((root / "run/lock/ci-fleet" / kind / old_boot).exists())

    def test_uncertain_removal_keeps_marker_and_blocks_later_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(cleanup.subprocess.CalledProcessError):
                self.run_cleanup({"old": network()}, root=root, remove_failures={"old": ["connection reset by peer"]})
            self.assertTrue(any((root / "run/lock/ci-fleet/removals" / BOOT_ID).iterdir()))
            remaining, calls, output = self.run_cleanup({"old": network()}, root=root)
            self.assertEqual(set(remaining), {"old"})
            self.assertFalse(any(call[:2] == ("network", "rm") for call in calls))
            self.assertIn("unresolved-Docker-request", output)

    def test_only_current_coordinated_services_are_exempt_from_active_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mounts = [{"Type": "bind", "RW": True, "Source": str(root / "run/lock/ci-fleet"), "Destination": "/run/ci-fleet/locks"}]
            containers = {}
            for service in ("controller", "docker-socket-proxy"):
                containers[service] = {"State": {"Status": "running"}, "Mounts": mounts, "Config": {
                    "Labels": {"com.docker.compose.project": "ci-fleet", "com.docker.compose.service": service, cleanup.PROXY_PROTOCOL_LABEL: "v1"},
                    "Env": ["DOCKER_HOST=unix:///run/ci-fleet/locks/docker.sock"] if service == "controller" else [],
                    "Cmd": ["--docker-socket-proxy"] if service == "docker-socket-proxy" else None,
                }}
            remaining, _, _ = self.run_cleanup({"old": network()}, root=root, active_containers=containers)
            self.assertEqual(remaining, {})
            for service in containers:
                with self.subTest(service=service):
                    legacy = copy.deepcopy(containers)
                    del legacy[service]["Config"]["Labels"][cleanup.PROXY_PROTOCOL_LABEL]
                    with patch.object(cleanup, "IDLE_WAIT_SECONDS", 0):
                        remaining, calls, _ = self.run_cleanup({"old": network()}, root=root, active_containers=legacy)
                    self.assertEqual(set(remaining), {"old"})
                    self.assertFalse(any(call[:2] == ("network", "rm") for call in calls))

    def test_pool_cleanup_preserves_fresh_and_unknown_creation_times(self):
        now = 2_000_000_000
        cases = {
            "fresh": datetime.fromtimestamp(now - 599, timezone.utc).isoformat(),
            "future": datetime.fromtimestamp(now + 1, timezone.utc).isoformat(),
            "invalid": "not-a-timestamp",
            "timezone-less": "2000-01-01T00:00:00",
            "empty": "",
            "null": None,
            "numeric": now - 1000,
        }
        for name, created in cases.items():
            with self.subTest(name=name), patch.object(cleanup.time, "time", return_value=now):
                item = {**network(), "Created": created}
                remaining, calls, _ = self.run_cleanup({name: item})
                self.assertEqual(remaining, {name: item})
                self.assertFalse(any(call[:2] == ("network", "rm") for call in calls))
        item = network()
        del item["Created"]
        remaining, _, _ = self.run_cleanup({"missing": item})
        self.assertEqual(remaining, {"missing": item})

    def test_pool_cleanup_removes_network_after_full_creation_grace(self):
        now = 2_000_000_000
        item = {**network(), "Created": datetime.fromtimestamp(now - 600, timezone.utc).isoformat()}
        with patch.object(cleanup.time, "time", return_value=now):
            remaining, _, _ = self.run_cleanup({"old": item})
        self.assertEqual(remaining, {})

    def test_documented_dry_run_uses_the_same_environment_as_applying_service(self):
        root = Path(__file__).resolve().parents[1]
        service = dict(
            line.split("=", 1) for line in (root / "host/systemd/ci-fleet-cleanup.service").read_text().splitlines()
            if "=" in line
        )
        docs = (root / "docs/HOST-MAINTENANCE.md").read_text()
        command = docs[docs.index("sudo systemd-run "):].split("\n```", 1)[0]
        args = shlex.split(command.replace("\\\n", " "))
        properties = dict(arg.removeprefix("--property=").split("=", 1) for arg in args if arg.startswith("--property="))
        self.assertEqual(properties["EnvironmentFile"], service["EnvironmentFile"])
        self.assertEqual(properties["WorkingDirectory"], service["WorkingDirectory"])
        self.assertEqual(properties["User"], service["User"])
        self.assertEqual(args[-1], shlex.split(service["ExecStart"])[0])
        self.assertNotIn("--apply", args)
        self.assertEqual(service["TimeoutStartSec"], "15min")

    def test_expired_labels_remain_instance_scoped_and_protect_infrastructure(self):
        expired = {f"{cleanup.LABEL_PREFIX}managed": "true", f"{cleanup.LABEL_PREFIX}expires-at": "1", f"{cleanup.LABEL_PREFIX}instance": "example"}
        inventory = {
            "expired": network("old", "192.0.2.0/24", labels=expired),
            "expired-unknown": {**network("old-unknown", labels=expired), "IPAM": None},
            "other-instance": network("other", "192.0.2.0/24", labels={**expired, f"{cleanup.LABEL_PREFIX}instance": "other"}),
            "unexpired": network("fresh", "192.0.2.0/24", labels={**expired, f"{cleanup.LABEL_PREFIX}expires-at": "99999999999"}),
            "active-expired": network("active", labels=expired, containers={"running": {}}),
            "controller": network("ci-fleet_default", labels=expired),
            "bridge": network("bridge", labels=expired),
        }
        for policy in ({}, POLICY):
            with self.subTest(policy=policy):
                remaining, _, output = self.run_cleanup(inventory, policy=policy)
                self.assertEqual(set(remaining), set(inventory) - {"expired", "expired-unknown"})
                self.assertIn("REMOVE network old expired=1", output)
                self.assertIn("REMOVE network old-unknown expired=1", output)

    def test_mixed_allocations_and_supernets_are_outside_pool_cleanup(self):
        mixed = network()
        mixed["IPAM"]["Config"].append({"Subnet": "192.0.2.0/24"})
        ipv6 = network()
        ipv6["IPAM"]["Config"].append({"Subnet": "2001:db8::/64"})
        inventory = {"mixed": mixed, "ipv6": ipv6, "supernet": network("supernet", "198.51.100.0/23")}
        remaining, _, _ = self.run_cleanup(inventory)
        self.assertEqual(remaining, inventory)

    def test_unknown_allocations_are_reported_without_blocking_later_pool_cleanup(self):
        allocations = {
            "missing-ipam": {},
            **{f"ipam-{name}": {"IPAM": value} for name, value in {
                "null": None, "string": "unknown", "number": 1, "boolean": True, "list": [],
            }.items()},
            "missing-config": {"IPAM": {}},
            **{f"config-{name}": {"IPAM": {"Config": value}} for name, value in {
                "null": None, "string": "unknown", "number": 1, "boolean": True, "mapping": {}, "empty": [],
            }.items()},
            **{f"entry-{name}": {"IPAM": {"Config": [value]}} for name, value in {
                "null": None, "string": "unknown", "number": 1, "boolean": True, "list": [], "missing-subnet": {},
            }.items()},
            **{f"subnet-{name}": {"IPAM": {"Config": [{"Subnet": value}]}} for name, value in {
                "null": None, "empty": "", "number": 1, "boolean": True, "list": [], "mapping": {},
                "invalid": "unknown", "host-bits": "198.51.100.1/29", "bare-address": "198.51.100.0",
                "netmask": "198.51.100.0/255.255.255.248", "whitespace": " 198.51.100.0/29",
            }.items()},
            "mixed-unknown": {"IPAM": {"Config": [{"Subnet": "198.51.100.0/29"}, {}]}},
        }
        inventory = {}
        for name, allocation in allocations.items():
            item = network(name)
            del item["IPAM"]
            inventory[name] = {**item, **allocation}
        inventory["valid"] = network("valid", "198.51.100.8/29")
        remaining, calls, output = self.run_cleanup(inventory)
        self.assertEqual(remaining, {name: item for name, item in inventory.items() if name != "valid"})
        for name in allocations:
            with self.subTest(name=name):
                self.assertIn(f"REPORT network {name} outside-cleanup-scope", output)
                self.assertNotIn(("network", "rm", name), calls)
        self.assertIn(("network", "rm", "valid"), calls)
        self.assertIn("REMOVE network valid empty-default-address-pool", output)

    def test_concurrent_container_attachment_is_preserved(self):
        remaining, calls, _ = self.run_cleanup({"empty": network()}, attach_on_recheck=True)
        self.assertEqual(remaining["empty"]["Containers"], {"new-container": {}})
        self.assertFalse(any(call[:2] == ("network", "rm") for call in calls))

    def test_stopped_container_reference_without_an_endpoint_is_preserved(self):
        inventory = {"stopped": network("stopped-job")}
        remaining, calls, _ = self.run_cleanup(inventory, container_references={"stopped"})
        self.assertEqual(remaining, inventory)
        self.assertIn(("ps", "-aq", "--filter", "network=stopped", "--filter", "network=stopped-job"), calls)
        self.assertFalse(any(call[:2] == ("network", "rm") for call in calls))

    def test_created_container_reference_by_name_without_a_network_id_is_preserved(self):
        inventory = {"created": network("created-job")}
        remaining, calls, _ = self.run_cleanup(inventory, container_references={"created-job"})
        self.assertEqual(remaining, inventory)
        self.assertFalse(any(call[:2] == ("network", "rm") for call in calls))

    def test_cleanup_continues_after_expected_removal_races(self):
        inventory = {"race": network("race"), "abandoned": network("abandoned", "198.51.100.8/29")}
        remaining, _, _ = self.run_cleanup(inventory, disappeared_on_recheck="race")
        self.assertEqual(remaining, {})
        remaining, _, _ = self.run_cleanup(inventory, attach_on_remove="race")
        self.assertEqual(set(remaining), {"race"})
        self.assertEqual(remaining["race"]["Containers"], {"new-container": {}})

    def test_cleared_attachment_race_retries_once_and_reclaims_later_networks(self):
        inventory = {"race": network("race"), "abandoned": network("abandoned", "198.51.100.8/29")}
        remaining, calls, _ = self.run_cleanup(inventory, remove_failures={"race": ["Error response from daemon: network has active endpoints"]})
        self.assertEqual(remaining, {})
        self.assertEqual(calls.count(("network", "rm", "race")), 2)
        self.assertIn(("network", "rm", "abandoned"), calls)

    def test_repeated_cleared_endpoint_races_defer_after_two_attempts(self):
        inventory = {"race": network("race"), "abandoned": network("abandoned", "198.51.100.8/29")}
        remaining, calls, output = self.run_cleanup(inventory, remove_failures={"race": ["Error response from daemon: has active endpoints"] * 2})
        self.assertEqual(set(remaining), {"race"})
        self.assertEqual(calls.count(("network", "rm", "race")), 2)
        self.assertIn("DEFER network race active-endpoint-race", output)

    def test_persistent_genuine_removal_error_remains_fatal(self):
        with self.assertRaises(cleanup.subprocess.CalledProcessError) as raised:
            self.run_cleanup({"denied": network()}, remove_failures={"denied": ["Error response from daemon: permission denied"] * 2})
        self.assertEqual(raised.exception.stderr, "Error response from daemon: permission denied")

    def test_incomplete_inspection_and_invalid_policy_fail_before_removal(self):
        missing = network()
        del missing["Containers"]
        with self.assertRaisesRegex(ValueError, "omitted container attachments"):
            self.run_cleanup({"missing": missing})
        with self.assertRaisesRegex(ValueError, "invalid Docker default address pool count"):
            self.run_cleanup({"empty": network()}, policy={"CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_COUNT": "-1"})
        with self.assertRaisesRegex(ValueError, "do not match configured count"):
            self.run_cleanup({"empty": network()}, policy={**POLICY, "CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_1_BASE": "192.0.2.0/24"})


if __name__ == "__main__":
    unittest.main()
