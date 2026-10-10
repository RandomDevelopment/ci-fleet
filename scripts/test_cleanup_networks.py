#!/usr/bin/env python3
"""Regression coverage for scoped network cleanup."""

import copy
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import shlex
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import cleanup_networks as cleanup


POLICY = {
    "CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_COUNT": "1",
    "CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_0_BASE": "198.51.100.0/24",
    "CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_0_SIZE": "29",
}


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
    def run_cleanup(self, inventory, *, apply=True, policy=None, instance="example", attach_on_recheck=False, container_references=None, disappeared_on_recheck=None, attach_on_remove=None, remove_failures=None):
        remaining = copy.deepcopy(inventory)
        calls = []
        inspections = {}
        failures = copy.deepcopy(remove_failures or {})

        def docker(*args):
            calls.append(args)
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
            if failures.get(args[2]):
                raise cleanup.subprocess.CalledProcessError(1, ["docker", *args], stderr=failures[args[2]].pop(0))
            if args[2] == attach_on_remove:
                remaining[args[2]]["Containers"] = {"new-container": {}}
                raise cleanup.subprocess.CalledProcessError(1, ["docker", *args])
            self.assertFalse(remaining[args[2]]["Containers"])
            del remaining[args[2]]
            return ""

        output = io.StringIO()
        with patch.dict(os.environ, POLICY if policy is None else policy, clear=True), patch.object(cleanup, "docker", docker), redirect_stdout(output):
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

    def test_expired_labels_remain_instance_scoped_and_protect_infrastructure(self):
        expired = {f"{cleanup.LABEL_PREFIX}managed": "true", f"{cleanup.LABEL_PREFIX}expires-at": "1", f"{cleanup.LABEL_PREFIX}instance": "example"}
        inventory = {
            "expired": network("old", "192.0.2.0/24", labels=expired),
            "other-instance": network("other", "192.0.2.0/24", labels={**expired, f"{cleanup.LABEL_PREFIX}instance": "other"}),
            "unexpired": network("fresh", "192.0.2.0/24", labels={**expired, f"{cleanup.LABEL_PREFIX}expires-at": "99999999999"}),
            "active-expired": network("active", labels=expired, containers={"running": {}}),
            "controller": network("ci-fleet_default", labels=expired),
            "bridge": network("bridge", labels=expired),
        }
        remaining, _, output = self.run_cleanup(inventory, policy={})
        self.assertEqual(set(remaining), set(inventory) - {"expired"})
        self.assertIn("REMOVE network old expired=1", output)

    def test_mixed_allocations_and_supernets_are_outside_pool_cleanup(self):
        mixed = network()
        mixed["IPAM"]["Config"].append({"Subnet": "192.0.2.0/24"})
        ipv6 = network()
        ipv6["IPAM"]["Config"].append({"Subnet": "2001:db8::/64"})
        inventory = {"mixed": mixed, "ipv6": ipv6, "supernet": network("supernet", "198.51.100.0/23")}
        remaining, _, _ = self.run_cleanup(inventory)
        self.assertEqual(remaining, inventory)

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
        remaining, calls, _ = self.run_cleanup(inventory, remove_failures={"race": ["network has active endpoints"]})
        self.assertEqual(remaining, {})
        self.assertEqual(calls.count(("network", "rm", "race")), 2)
        self.assertIn(("network", "rm", "abandoned"), calls)

    def test_repeated_cleared_endpoint_races_defer_after_two_attempts(self):
        inventory = {"race": network("race"), "abandoned": network("abandoned", "198.51.100.8/29")}
        remaining, calls, output = self.run_cleanup(inventory, remove_failures={"race": ["has active endpoints"] * 2})
        self.assertEqual(set(remaining), {"race"})
        self.assertEqual(calls.count(("network", "rm", "race")), 2)
        self.assertIn("DEFER network race active-endpoint-race", output)

    def test_persistent_genuine_removal_error_remains_fatal(self):
        with self.assertRaises(cleanup.subprocess.CalledProcessError) as raised:
            self.run_cleanup({"denied": network()}, remove_failures={"denied": ["permission denied"] * 2})
        self.assertEqual(raised.exception.stderr, "permission denied")

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
