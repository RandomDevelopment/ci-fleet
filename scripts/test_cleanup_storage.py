#!/usr/bin/env python3
"""Storage retention, rollback protection, and host cleanup regressions."""

from contextlib import redirect_stdout
from datetime import datetime, timezone
import errno
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import cleanup_storage as cleanup
import health
from test_cleanup_networks import BOOT_ID


NOW = 1800000000
IMAGE = "sha256:" + "a" * 64
OTHER = "sha256:" + "b" * 64
OLD = NOW - cleanup.AGE_SECONDS - 1


def image(identity=IMAGE, **changes):
    return {"Id": identity, "RepoTags": ["example:old"], "RepoDigests": [], "Containers": 0,
            "Labels": {}, "Created": OLD, **changes}


def cache(**changes):
    return {"ID": "cache-1", "InUse": False, "LastUsedAt": datetime.fromtimestamp(OLD, timezone.utc).isoformat(), **changes}


def usage():
    return {"Images": [image()], "Containers": [], "BuildCache": [cache()]}


class StorageTests(unittest.TestCase):
    def test_failed_fresh_policy_is_prospective_but_original_ids_remain_protected(self):
        for portable in (False, True):
            with self.subTest(portable=portable), tempfile.TemporaryDirectory() as directory, \
                 patch.dict(os.environ, {"CI_FLEET_ROOT_PREFIX": directory}):
                root = Path(directory)
                current = root / "etc/ci-fleet/ci-fleet.env"
                current.parent.mkdir(parents=True)
                revision = "a" * 40
                values = f"CI_FLEET_ENGINE_REF={revision}\nCI_FLEET_RUNNER_IMAGE=runner:B\nCI_FLEET_CONTROLLER_IMAGE=controller:B\nCI_FLEET_RUNNER_IMAGE_DIGEST={IMAGE}\nCI_FLEET_CONTROLLER_IMAGE_DIGEST={OTHER}\n"
                if portable:
                    values += f"CI_FLEET_RUNNER_RUNTIME_IMAGE_ID={IMAGE}\nCI_FLEET_CONTROLLER_RUNTIME_IMAGE_ID={OTHER}\n"
                current.write_text(values)
                release = root / "opt/ci-fleet/releases" / revision
                release.mkdir(parents=True)
                (release / "engine-capabilities.json").write_text(json.dumps({"schema_version": 1, "capabilities": {"portable_managed_image_ids": portable}}))
                fresh = root / "var/lib/ci-fleet/checkpoints/failed-A"
                fresh.mkdir(parents=True)
                prospective = "sha256:" + "c" * 64
                retained = "sha256:" + "d" * 64
                (fresh / "fresh-image-env").write_text(f"CI_FLEET_ENGINE_REF={'b' * 40}\nCI_FLEET_RUNNER_IMAGE=runner:A\nCI_FLEET_CONTROLLER_IMAGE=controller:A\nCI_FLEET_RUNNER_IMAGE_DIGEST={prospective}\nCI_FLEET_CONTROLLER_IMAGE_DIGEST={prospective}\nCI_FLEET_RUNNER_RUNTIME_IMAGE_ID={prospective}\n")
                captured = fresh / "fresh-image-ids"
                captured.write_text("CI_FLEET_RUNNER_IMAGE_ID=absent\nCI_FLEET_CONTROLLER_IMAGE_ID=absent\n")
                binding = {"roles": {name: {"config_digest": value, "runtime_image_id": value, "manifest_digest": None}
                                     for name, value in (("runner", IMAGE), ("controller", OTHER))}}
                accounting = {"Images": [image(IMAGE), image(OTHER)]}
                with patch.object(cleanup, "read_bindings", return_value=binding) as read:
                    protected = cleanup.protected_images(accounting)
                    self.assertNotIn(prospective, protected)
                    self.assertEqual(read.call_count, int(portable))
                    captured.write_text(f"CI_FLEET_RUNNER_IMAGE_ID={retained}\nCI_FLEET_CONTROLLER_IMAGE_ID=absent\n")
                    with self.assertRaisesRegex(ValueError, "unresolved"):
                        cleanup.protected_images(accounting)
                    accounting["Images"].append(image(retained))
                    self.assertIn(retained, cleanup.protected_images(accounting))
                    captured.unlink()
                    with self.assertRaises((OSError, ValueError)):
                        cleanup.protected_images(accounting)
                    captured.write_text("CI_FLEET_RUNNER_IMAGE_ID=absent\n")
                    with self.assertRaisesRegex(ValueError, "mappings"):
                        cleanup.protected_images(accounting)
                    if portable:
                        captured.write_text("CI_FLEET_RUNNER_IMAGE_ID=absent\nCI_FLEET_CONTROLLER_IMAGE_ID=absent\n")
                        read.side_effect = ValueError("unknown current proof")
                        with self.assertRaisesRegex(ValueError, "unknown current proof"):
                            cleanup.protected_images(accounting)

    def test_retained_historical_source_protects_distinct_opaque_and_portable_ids(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"CI_FLEET_ROOT_PREFIX": directory}):
            root = Path(directory)
            current = root / "etc/ci-fleet/ci-fleet.env"
            current.parent.mkdir(parents=True)
            checkpoint = root / "var/lib/ci-fleet/checkpoints/legacy"
            checkpoint.mkdir(parents=True)
            old_ref, new_ref = "a" * 40, "b" * 40
            old_runner, old_controller, runner_c, controller_c, runner_r, controller_r = (
                "sha256:" + character * 64 for character in "123456")
            legacy = (f"CI_FLEET_ENGINE_REF={old_ref}\nCI_FLEET_RUNNER_IMAGE=runner:legacy\nCI_FLEET_CONTROLLER_IMAGE=controller:legacy\n"
                      f"CI_FLEET_RUNNER_IMAGE_DIGEST={old_runner}\nCI_FLEET_CONTROLLER_IMAGE_DIGEST={old_controller}\n")
            (checkpoint / "ci-fleet.env").write_text(legacy)
            (checkpoint / "image-env").write_text(legacy)
            (checkpoint / "image-ids.env").write_text(f"CI_FLEET_RUNNER_IMAGE_ID=absent\nCI_FLEET_CONTROLLER_IMAGE_ID={old_controller}\n")
            current.write_text(f"CI_FLEET_ENGINE_REF={new_ref}\nCI_FLEET_RUNNER_IMAGE=runner:current\nCI_FLEET_CONTROLLER_IMAGE=controller:current\n"
                               f"CI_FLEET_RUNNER_IMAGE_DIGEST={runner_c}\nCI_FLEET_CONTROLLER_IMAGE_DIGEST={controller_c}\n"
                               f"CI_FLEET_RUNNER_RUNTIME_IMAGE_ID={runner_r}\nCI_FLEET_CONTROLLER_RUNTIME_IMAGE_ID={controller_r}\n")
            install_root = root / "opt/ci-fleet"
            new = install_root / "releases" / new_ref
            new.mkdir(parents=True)
            (new / "engine-capabilities.json").write_text(json.dumps({"schema_version": 1, "capabilities": {"portable_managed_image_ids": True}}))
            old = install_root / "manager/releases/historical-runtime"
            old.mkdir(parents=True)
            marker = old / ".ci-fleet-engine-ref"
            marker.write_text(old_ref + "\n")
            manifest = old / "engine-capabilities.json"
            manifest.write_text(json.dumps({"schema_version": 1, "capabilities": {"portable_managed_image_ids": False}}))
            pointer = checkpoint / "release-target"
            pointer.write_text(str(old) + "\n")
            bound = {"roles": {role: {"config_digest": config, "runtime_image_id": runtime, "manifest_digest": None}
                               for role, config, runtime in (("runner", runner_c, runner_r), ("controller", controller_c, controller_r))}}
            accounting = {"Images": [image(identity=value, RepoTags=[]) for value in (old_runner, old_controller, runner_r, controller_r)]}
            with patch.object(cleanup, "read_bindings", return_value=bound) as read:
                protected = cleanup.protected_images(accounting)
                self.assertTrue({old_runner, old_controller, runner_c, controller_c, runner_r, controller_r} <= protected)
                self.assertEqual(read.call_count, 1)
                self.assertNotIn("absent", protected)
                for change in ("missing", "wrong-revision", "writable-parent", "symlink", "outside", "mismatched-source", "missing-pointer"):
                    with self.subTest(change=change):
                        saved = pointer.read_bytes()
                        if change == "missing": marker.unlink()
                        elif change == "wrong-revision": marker.write_text(new_ref + "\n")
                        elif change == "writable-parent": old.parent.chmod(0o777)
                        elif change == "symlink": marker.unlink(); marker.symlink_to(new / "engine-capabilities.json")
                        elif change == "outside": pointer.write_text(str(root) + "\n")
                        elif change == "mismatched-source": pointer.write_text(str(new) + "\n")
                        else: pointer.unlink()
                        with self.assertRaises((ValueError, OSError)):
                            cleanup.protected_images(accounting)
                        if marker.is_symlink(): marker.unlink()
                        marker.write_text(old_ref + "\n")
                        old.parent.chmod(0o755)
                        pointer.write_bytes(saved)
                read.side_effect = ValueError("unknown portable binding")
                with self.assertRaisesRegex(ValueError, "unknown portable binding"):
                    cleanup.protected_images(accounting)
                read.side_effect = None
                manifest.write_text(json.dumps({"schema_version": 1, "capabilities": {"portable_managed_image_ids": True}}))
                read.side_effect = lambda path, values: bound if path.parent != checkpoint else (_ for _ in ()).throw(ValueError("missing checkpoint proof"))
                with self.assertRaisesRegex(ValueError, "missing checkpoint proof"):
                    cleanup.protected_images(accounting)
            # Current historical manual installations can retain the same protected source.
            current.write_text(legacy)
            (install_root / ".ci-fleet-engine-ref").write_text(old_ref + "\n")
            (install_root / "engine-capabilities.json").write_text(json.dumps({"schema_version": 1, "capabilities": {"portable_managed_image_ids": False}}))
            manifest.write_text(json.dumps({"schema_version": 1, "capabilities": {"portable_managed_image_ids": False}}))
            with patch.object(cleanup, "read_bindings", side_effect=AssertionError("opaque source must not use portable proof")):
                self.assertTrue({old_runner, old_controller} <= cleanup.protected_images(accounting))

    def test_docker_usage_reads_detailed_lists_on_old_and_new_daemons(self):
        for modern_daemon in (False, True):
            with self.subTest(modern_daemon=modern_daemon):
                connection = unittest.mock.MagicMock()
                response = connection.getresponse.return_value
                response.status = 200

                def request(_method, path):
                    accounting = usage()
                    if modern_daemon:
                        accounting = {"ImageUsage": {"Items": accounting["Images"]},
                                      "ContainerUsage": {"Items": []},
                                      "BuildCacheUsage": {"Items": accounting["BuildCache"]}}
                        if path != "/system/df?verbose=true":
                            accounting = {key: {"TotalCount": len(value["Items"])} for key, value in accounting.items()}
                    response.read.return_value = json.dumps(accounting).encode()

                connection.request.side_effect = request
                with patch.object(cleanup.http.client, "HTTPConnection", return_value=connection), \
                     patch.object(cleanup.socket, "socket"):
                    accounting = cleanup.docker_usage()
                    self.assertEqual({field: accounting[field] for field in usage()}, usage())
                connection.request.assert_called_once_with("GET", "/system/df?verbose=true")
                connection.close.assert_called_once()

    def test_docker_usage_rejects_summary_only_or_malformed_resource_lists(self):
        for resources in ({"ImageUsage": {}, "ContainerUsage": {}, "BuildCacheUsage": {}},
                          {"ImageUsage": {"Items": {}}, "ContainerUsage": {"Items": []}, "BuildCacheUsage": {"Items": []}}):
            with self.subTest(resources=resources):
                connection = unittest.mock.MagicMock()
                connection.getresponse.return_value.status = 200
                connection.getresponse.return_value.read.return_value = json.dumps(resources).encode()
                with patch.object(cleanup.http.client, "HTTPConnection", return_value=connection), \
                     patch.object(cleanup.socket, "socket"), self.assertRaisesRegex(ValueError, "omitted resource lists"):
                    cleanup.docker_usage()
                connection.close.assert_called_once()

    def test_container_inventory_includes_inactive_references_without_storage_accounting(self):
        connection = unittest.mock.MagicMock()
        connection.getresponse.return_value.status = 200
        containers = [{"Id": "stopped", "State": "exited", "ImageID": IMAGE}]
        connection.getresponse.return_value.read.return_value = json.dumps(containers).encode()
        with patch.object(cleanup.http.client, "HTTPConnection", return_value=connection), \
             patch.object(cleanup.socket, "socket"):
            self.assertEqual(cleanup.docker_containers(), containers)
        connection.request.assert_called_once_with("GET", "/containers/json?all=true")
        connection.close.assert_called_once()
        for invalid in (None, {}, [None], ["stopped"]):
            with self.subTest(invalid=invalid), patch.object(cleanup, "docker_json", return_value=invalid):
                with self.assertRaisesRegex(ValueError, "omitted containers"):
                    cleanup.docker_containers()

    def test_retention_decisions(self):
        for change, references, protected, expected in [
            ({}, set(), set(), "eligible"),
            ({}, {IMAGE}, set(), "container-reference-or-unknown-use"),
            ({"Containers": -1}, set(), set(), "eligible"),
            ({"Containers": -1}, {IMAGE}, set(), "container-reference-or-unknown-use"),
            ({"Containers": 1}, set(), set(), "container-reference-or-unknown-use"),
            ({"Containers": None}, set(), set(), "container-reference-or-unknown-use"),
            ({"Containers": "0"}, set(), set(), "container-reference-or-unknown-use"),
            ({}, set(), {"example:old"}, "installed-or-checkpoint-release"),
            ({}, set(), {IMAGE}, "installed-or-checkpoint-release"),
            ({"RepoTags": [], "RepoDigests": ["example@" + IMAGE]}, set(), {"example@" + IMAGE}, "installed-or-checkpoint-release"),
            ({"Labels": {cleanup.PREFIX + "retention": "keep"}}, set(), set(), "explicit-keep"),
            ({"Created": NOW - 30}, set(), set(), "recent-or-unknown-age"),
            ({"Created": None}, set(), set(), "recent-or-unknown-age"),
            ({"Created": NOW + 1}, set(), set(), "recent-or-unknown-age"),
        ]:
            with self.subTest(change=change, protected=protected):
                self.assertEqual(cleanup.image_reason(image(**change), references, protected, NOW), expected)
        for changes, expected in [({}, "eligible-subject-to-cache-reserve"),
                                  ({"InUse": True}, "active-or-unknown-use"),
                                  ({"InUse": None}, "active-or-unknown-use"),
                                  ({"LastUsedAt": None}, "unknown-last-use"),
                                  ({"LastUsedAt": "2020-01-01"}, "unknown-last-use"),
                                  ({"LastUsedAt": datetime.fromtimestamp(NOW, timezone.utc).isoformat()}, "recent-last-use")]:
            self.assertEqual(cleanup.cache_reason(cache(**changes), NOW), expected)

    def run_storage(self, *, apply=True, admitted=True, accounting=None, disk=None):
        output = io.StringIO()
        env = {"CI_FLEET_DOCKER_STORAGE_RETENTION": cleanup.ADMISSION} if admitted else {}
        with patch.dict(os.environ, env, clear=True), patch.object(cleanup, "docker_usage", return_value=accounting or usage()), \
             patch.object(cleanup, "disk_percent", side_effect=disk or [81] * 10), \
             patch.object(cleanup, "protected_images", return_value=set()), \
             patch.object(cleanup, "mutate") as mutate, patch.object(cleanup.time, "time", return_value=NOW), redirect_stdout(output):
            cleanup.cleanup_storage(apply=apply)
        return output.getvalue(), mutate.call_args_list

    def test_dry_run_no_admission_and_below_trigger_never_mutate(self):
        for options in ({"apply": False}, {"admitted": False}, {"disk": [69]}):
            output, mutations = self.run_storage(**options)
            self.assertFalse(mutations)
            self.assertIn("KEEP storage", output)
        output, _ = self.run_storage(apply=False)
        self.assertIn("ELIGIBLE image", output)
        self.assertIn("ELIGIBLE build-cache", output)

    def test_maintenance_reclaims_then_restores_upgrade_headroom(self):
        output, mutations = self.run_storage(disk=[81, 81, 76, 64])
        self.assertEqual(mutations[0].args, ("image", "rm", "--no-prune", "--", "example:old"))
        self.assertEqual(mutations[1].args, ("builder", "prune", "--all", "--force", "--filter", "until=168h", "--keep-storage", str(cleanup.CACHE_KEEP_BYTES)))
        self.assertIn("after=64%", output)
        self.assertNotIn("headroom-insufficient", output)

    def test_stops_at_headroom_target_and_reports_insufficient_capacity(self):
        _, mutations = self.run_storage(disk=[70, 65, 65, 65])
        self.assertFalse(mutations)
        output, _ = self.run_storage()
        self.assertIn("headroom-insufficient", output)

    def test_image_count_bound_and_unknown_cache_preserved(self):
        accounting = usage()
        accounting["Images"] = [image(identity="sha256:" + f"{index:064x}", RepoTags=[]) for index in range(60)]
        accounting["BuildCache"] = [cache(LastUsedAt="invalid")]
        _, mutations = self.run_storage(accounting=accounting, disk=[81] * 100)
        self.assertEqual(len(mutations), cleanup.MAX_IMAGES)
        self.assertTrue(all(call.args[0] == "image" for call in mutations))

    def test_classic_builder_parents_do_not_abort_leaf_and_cache_reclamation(self):
        accounting = usage()
        accounting["Images"] = [image(RepoTags=[], Created=OLD - 1), image(identity=OTHER, ParentId=IMAGE)]
        output, mutations = self.run_storage(accounting=accounting)
        self.assertEqual([call.args[:2] for call in mutations], [("image", "rm"), ("builder", "prune")])
        self.assertEqual(mutations[0].args[-1], "example:old")
        self.assertIn(f"KEEP image {IMAGE} image-has-children", output)

    def test_kept_child_protects_untagged_parent(self):
        accounting = usage()
        accounting["Images"] = [image(RepoTags=[]), image(identity=OTHER, ParentId=IMAGE,
            Labels={cleanup.PREFIX + "retention": "keep"})]
        _, mutations = self.run_storage(accounting=accounting)
        self.assertEqual([call.args[:2] for call in mutations], [("builder", "prune")])

    def test_unadmitted_cleanup_does_not_request_storage_accounting(self):
        with patch.object(sys, "argv", ["cleanup_storage.py", "--apply"]), \
             patch.dict(os.environ, {}, clear=True), \
             patch.object(cleanup, "docker_containers", return_value=[]), \
             patch.object(cleanup, "docker_usage", side_effect=TimeoutError("image accounting stalled")) as accounting, \
             patch.object(cleanup, "cleanup_expired"), patch.object(cleanup, "disk_percent", return_value=81), \
             patch.object(cleanup.coordination, "cleanup_networks", side_effect=lambda **args: (
                 args["maintenance"](True), args["storage"](apply=True))), redirect_stdout(io.StringIO()):
            cleanup.main()
        accounting.assert_not_called()

    def test_network_recovery_precedes_storage_failure_under_the_same_locks(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {"CI_FLEET_ROOT_PREFIX": directory,
                                    "CI_FLEET_DOCKER_STORAGE_RETENTION": cleanup.ADMISSION}, clear=True):
            events = []
            root = Path(directory)
            boot = root / "proc/sys/kernel/random/boot_id"
            boot.parent.mkdir(parents=True)
            boot.write_text(BOOT_ID)

            def networks(**_kwargs):
                events.append("networks")
                for path in (root / "run/ci-fleet-installer.lock", root / "run/lock/ci-fleet/docker-maintenance.lock"):
                    with path.open("r+") as contender, self.assertRaises(BlockingIOError):
                        cleanup.coordination.fcntl.flock(contender, cleanup.coordination.fcntl.LOCK_SH | cleanup.coordination.fcntl.LOCK_NB)

            def accounting():
                events.append("storage")
                raise TimeoutError("image accounting stalled")

            with patch.object(sys, "argv", ["cleanup_storage.py", "--apply"]), \
                 patch.object(cleanup, "docker_containers", return_value=[]), \
                 patch.object(cleanup, "docker_usage", side_effect=accounting), \
                 patch.object(cleanup, "cleanup_expired", side_effect=lambda **_kwargs: events.append("expired")), \
                 patch.object(cleanup.coordination, "docker", return_value="inactive"), \
                 patch.object(cleanup.coordination, "active_work", return_value=False), \
                 patch.object(cleanup.coordination, "remove_networks", side_effect=networks), \
                 patch.object(cleanup.time, "sleep"), redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(TimeoutError, "image accounting stalled"):
                    cleanup.main()
            self.assertEqual(events, ["expired", "networks", "storage"])

    def test_old_proxy_defers_storage(self):
        accounting = usage()
        accounting["Containers"] = [{"ImageID": OTHER, "State": "running", "Labels": {cleanup.coordination.PROXY_PROTOCOL_LABEL: "v1"}}]
        output, mutations = self.run_storage(accounting=accounting)
        self.assertFalse(mutations)
        self.assertIn("old-or-uncoordinated-proxy", output)

    def test_older_api_reclaims_unreferenced_images_and_preserves_stopped_references(self):
        accounting = {"Images": [image(Containers=-1), image(identity=OTHER, Containers=-1)],
                      "Containers": [{"ImageID": OTHER, "State": "exited"}], "BuildCache": []}
        output, mutations = self.run_storage(accounting=accounting, disk=[81, 81, 64, 64])
        self.assertEqual([call.args for call in mutations], [("image", "rm", "--no-prune", "--", "example:old")])
        self.assertIn(f"KEEP image {OTHER} container-reference", output)

    def test_ipv6_registry_tag_is_removed_without_blocking_later_candidates(self):
        accounting = usage()
        tag = "[2001:db8::1]:5000/team/image:old"
        accounting["Images"] = [image(RepoTags=[tag]), image(identity=OTHER, RepoTags=["example:other"])]
        _, mutations = self.run_storage(accounting=accounting)
        self.assertEqual(mutations[0].args, ("image", "rm", "--no-prune", "--", tag))
        self.assertEqual(mutations[1].args[-1], "example:other")
        self.assertEqual(mutations[2].args[:2], ("builder", "prune"))

    def test_digest_only_images_remove_every_reference_and_continue_cleanup(self):
        digests = ["example/one@" + IMAGE, "[2001:db8::1]:5000/two@" + IMAGE]
        for tags in (None, [], ["<none>:<none>"]):
            with self.subTest(tags=tags):
                accounting = usage()
                accounting["Images"] = [image(RepoTags=tags, RepoDigests=digests), image(identity=OTHER)]
                _, mutations = self.run_storage(accounting=accounting)
                self.assertEqual(mutations[0].args, ("image", "rm", "--no-prune", "--", *digests))
                self.assertEqual(mutations[1].args[-1], "example:old")
                self.assertEqual(mutations[2].args[:2], ("builder", "prune"))

    def test_legacy_none_digest_uses_image_identity_without_blocking_cleanup(self):
        accounting = usage()
        accounting["Images"] = [image(RepoTags=["<none>:<none>"], RepoDigests=["<none>@<none>"]), image(identity=OTHER)]
        _, mutations = self.run_storage(accounting=accounting)
        self.assertEqual(mutations[0].args, ("image", "rm", "--no-prune", "--", IMAGE))
        self.assertEqual(mutations[1].args[-1], "example:old")
        self.assertEqual(mutations[2].args[:2], ("builder", "prune"))

    def test_dry_run_reuses_one_accounting_snapshot(self):
        with patch.object(sys, "argv", ["cleanup_storage.py"]), \
             patch.dict(os.environ, {"CI_FLEET_DOCKER_STORAGE_RETENTION": cleanup.ADMISSION}, clear=True), \
             patch.object(cleanup, "docker_containers", return_value=[]) as containers, \
             patch.object(cleanup, "docker_usage", return_value=usage()) as accounting, \
             patch.object(cleanup, "protected_images", return_value=set()), \
             patch.object(cleanup, "disk_percent", return_value=10), \
             patch.object(cleanup.coordination, "docker", return_value=""), \
             patch.object(cleanup.coordination, "remove_networks") as networks, \
             patch.object(cleanup, "mutate") as mutate, redirect_stdout(io.StringIO()):
            cleanup.main()
        accounting.assert_called_once()
        containers.assert_called_once()
        networks.assert_called_once_with(apply=False, instance="", pools=[])
        mutate.assert_not_called()

    def test_applying_refreshes_inventory_after_expired_container_removal(self):
        expired = {"Images": [], "BuildCache": [], "Containers": [{"Id": "expired", "ImageID": IMAGE,
            "State": "exited", "Labels": {cleanup.PREFIX + "managed": "true", cleanup.PREFIX + "expires-at": "1"}}]}
        fresh = {**usage(), "BuildCache": []}
        with patch.object(sys, "argv", ["cleanup_storage.py", "--apply"]), \
             patch.dict(os.environ, {"CI_FLEET_DOCKER_STORAGE_RETENTION": cleanup.ADMISSION}, clear=True), \
             patch.object(cleanup, "docker_containers", return_value=expired["Containers"]) as containers, \
             patch.object(cleanup, "docker_usage", return_value=fresh) as accounting, \
             patch.object(cleanup, "disk_percent", side_effect=[81, 81, 64, 64]), \
             patch.object(cleanup.time, "time", return_value=NOW), \
             patch.object(cleanup, "protected_images", return_value=set()), \
             patch.object(cleanup.coordination, "docker", return_value=""), \
             patch.object(cleanup.coordination, "cleanup_networks", side_effect=lambda **args: (
                 args["maintenance"](args["apply"]), args["storage"](apply=args["apply"]))), \
             patch.object(cleanup, "mutate") as mutate, redirect_stdout(io.StringIO()):
            cleanup.main()
        self.assertEqual(containers.call_count, 2)
        accounting.assert_called_once()
        self.assertEqual([call.args for call in mutate.call_args_list], [
            ("rm", "expired"), ("image", "rm", "--no-prune", "--", "example:old")])

    def test_stale_counters_exclude_unowned_and_unexpired_resources(self):
        expired = {cleanup.PREFIX + "managed": "true", cleanup.PREFIX + "instance": "fixture", cleanup.PREFIX + "expires-at": "1"}
        recent = {**expired, cleanup.PREFIX + "expires-at": str(NOW + 1)}
        volumes = {"unowned": {}, "recent": recent, "referenced": expired, "expired": expired}
        accounting = {"Containers": [{"Id": name, "State": "running", "Labels": labels} for name, labels in (
            ("controller", {}), ("recent", recent), ("expired", expired),
            ("other-instance", {**expired, cleanup.PREFIX + "instance": "other"}))]}

        def docker(*args):
            if args[:2] == ("volume", "ls"):
                return "\n".join(volumes)
            if args[:2] == ("volume", "inspect"):
                return json.dumps([{"Labels": volumes[args[-1]]}])
            return "stopped-container" if args[-1] == "volume=referenced" else ""

        with patch.object(cleanup.coordination, "docker", side_effect=docker), \
             patch.object(cleanup.time, "time", return_value=NOW), redirect_stdout(io.StringIO()) as output:
            cleanup.cleanup_expired(apply=False, instance="fixture", usage=accounting)
        run = lambda args: subprocess.CompletedProcess(args, 0, output.getvalue(), "")
        self.assertEqual(health._stale_resources(run, "fixture"), {"containers": 1, "networks": 0, "volumes": 2, "images": 0})

    def test_applying_volume_inventory_filters_owned_expiry_candidates(self):
        for instance in ("", "fixture"):
            with self.subTest(instance=instance), patch.object(cleanup.coordination, "docker", return_value="") as docker:
                cleanup.cleanup_expired(apply=True, instance=instance, usage={"Containers": []})
                expected = ["volume", "ls", "-q", "--filter", f"label={cleanup.PREFIX}managed=true",
                            "--filter", f"label={cleanup.PREFIX}expires-at"]
                if instance:
                    expected += ["--filter", f"label={cleanup.PREFIX}instance={instance}"]
                docker.assert_called_once_with(*expected)

    def test_service_deadline_stops_expired_resources_before_creating_uncertainty(self):
        labels = {cleanup.PREFIX + "managed": "true", cleanup.PREFIX + "instance": "fixture", cleanup.PREFIX + "expires-at": "1"}
        for resource in ("container", "volume"):
            with self.subTest(resource=resource), tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"CI_FLEET_ROOT_PREFIX": directory}):
                boot = Path(directory) / "proc/sys/kernel/random/boot_id"
                boot.parent.mkdir(parents=True)
                boot.write_text(BOOT_ID)
                # An idle wait has already spent ten of the service's fifteen minutes.
                clock = [600]
                items = [{"Id": f"fixture-{index}", "State": "exited", "Labels": labels} for index in range(100)]
                def docker(*args):
                    if args[:2] == ("volume", "ls"):
                        return "\n".join(item["Id"] for item in items) if resource == "volume" else ""
                    if args[:2] == ("volume", "inspect"):
                        return json.dumps([{"Labels": labels}])
                    return ""
                def completed(**_kwargs):
                    clock[0] += 25
                    return "", ""
                with patch.object(cleanup.time, "monotonic", side_effect=lambda: clock[0]), \
                     patch.object(cleanup.coordination, "SERVICE_DEADLINE", 840), \
                     patch.object(cleanup.coordination, "docker", side_effect=docker), \
                     patch.object(cleanup.subprocess, "Popen") as popen, redirect_stdout(io.StringIO()):
                    popen.return_value.returncode = 0
                    popen.return_value.communicate.side_effect = completed
                    with self.assertRaises(cleanup.coordination.MaintenanceDeferred):
                        cleanup.cleanup_expired(apply=True, instance="fixture", usage={"Containers": items if resource == "container" else []})
                    self.assertEqual(popen.call_count, 9)
                    self.assertFalse(cleanup.coordination.pending_requests("removals"))
                    with self.assertRaises(cleanup.coordination.MaintenanceDeferred):
                        cleanup.mutate("image", "rm", IMAGE, timeout=120)
                    with self.assertRaises(cleanup.coordination.MaintenanceDeferred):
                        cleanup.coordination.remove_network("fixture-network")
                    self.assertEqual(popen.call_count, 9)
                    self.assertFalse(cleanup.coordination.pending_requests("removals"))
                self.assertLess(clock[0], 840)

    def test_checkpoint_ids_and_aliases_are_protected_and_symlinks_fail(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"CI_FLEET_ROOT_PREFIX": directory}):
            current = Path(directory) / "etc/ci-fleet/ci-fleet.env"
            current.parent.mkdir(parents=True)
            current.write_text("CI_FLEET_RUNNER_IMAGE=runner:current\nCI_FLEET_CONTROLLER_IMAGE=controller:current\n")
            checkpoint = Path(directory) / "var/lib/ci-fleet/checkpoints/previous"
            checkpoint.mkdir(parents=True)
            reviewed = "sha256:" + "c" * 64
            (checkpoint / "ci-fleet.env").write_text("CI_FLEET_RUNNER_IMAGE=runner:previous\n")
            (checkpoint / "image-env").write_text(f"CI_FLEET_RUNNER_IMAGE=runner:previous\nCI_FLEET_RUNNER_IMAGE_DIGEST={reviewed}\n")
            ids = checkpoint / "image-ids.env"
            ids.write_text(f"CI_FLEET_CONTROLLER_IMAGE_ID={IMAGE}\nCI_FLEET_RUNNER_IMAGE_ID=absent\n")
            fresh = checkpoint.parent / "fresh"
            fresh.mkdir()
            (fresh / "fresh-image-env").write_text("CI_FLEET_RUNNER_IMAGE=runner:original\n")
            fresh_ids = fresh / "fresh-image-ids"
            fresh_ids.write_text(f"CI_FLEET_CONTROLLER_IMAGE_ID={OTHER}\nCI_FLEET_RUNNER_IMAGE_ID=absent\n")
            protected = cleanup.protected_images()
            self.assertEqual(protected, {"runner:current", "controller:current", "runner:previous", "runner:original", IMAGE, OTHER, reviewed})
            complete = {"Images": [image(identity=value, RepoTags=[]) for value in (IMAGE, OTHER, reviewed)]}
            self.assertEqual(cleanup.protected_images(complete), protected)
            with self.assertRaisesRegex(ValueError, "opaque rollback image identities are unresolved"):
                cleanup.protected_images({"Images": [image(identity=OTHER, RepoTags=[])]})
            self.assertEqual(cleanup.image_reason(image(OTHER, RepoTags=None), set(), protected, NOW), "installed-or-checkpoint-release")
            self.assertEqual(cleanup.image_reason(image(reviewed, RepoTags=None), set(), protected, NOW), "installed-or-checkpoint-release")
            for path in (ids, fresh_ids, checkpoint / "image-env"):
                content = path.read_text()
                path.unlink()
                path.symlink_to(current)
                with self.assertRaises(ValueError):
                    cleanup.protected_images()
                path.unlink()
                path.write_text(content)

    def test_uncertain_mutation_blocks_later_maintenance(self):
        for failure in (subprocess.TimeoutExpired(["docker", "builder", "prune"], 1), OSError(errno.EIO, "output read failed")):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"CI_FLEET_ROOT_PREFIX": directory}):
                boot = Path(directory) / "proc/sys/kernel/random/boot_id"
                boot.parent.mkdir(parents=True)
                boot.write_text(BOOT_ID)
                with patch.object(cleanup.subprocess, "Popen") as popen, self.assertRaisesRegex(ValueError, "uncertain"):
                    popen.return_value.communicate.side_effect = failure
                    cleanup.mutate("builder", "prune", timeout=1)
                self.assertTrue(cleanup.coordination.pending_requests("removals"))
                if isinstance(failure, subprocess.TimeoutExpired):
                    popen.return_value.kill.assert_called_once()
                    popen.return_value.wait.assert_called_once()

    def test_launch_failure_clears_only_its_marker_and_allows_retry(self):
        for failure in (FileNotFoundError(errno.ENOENT, "missing Docker client"),
                        PermissionError(errno.EACCES, "cannot execute Docker client"),
                        BlockingIOError(errno.EAGAIN, "process limit reached")):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"CI_FLEET_ROOT_PREFIX": directory}):
                boot = Path(directory) / "proc/sys/kernel/random/boot_id"
                boot.parent.mkdir(parents=True)
                boot.write_text(BOOT_ID)
                previous = cleanup.coordination.removal_marker()
                with patch.object(cleanup.subprocess, "Popen", side_effect=failure), self.assertRaisesRegex(ValueError, "no request dispatched"):
                    cleanup.mutate("image", "rm", IMAGE, timeout=1)
                self.assertEqual(list(previous.parent.iterdir()), [previous])
                previous.unlink()
                self.assertFalse(cleanup.coordination.pending_requests("removals"))
                client = Path(directory) / "docker"
                client.write_text("#!/bin/sh\nexit 0\n")
                client.chmod(0o700)
                with patch.dict(os.environ, {"PATH": directory}):
                    cleanup.mutate("image", "rm", IMAGE, timeout=1)
                self.assertFalse(cleanup.coordination.pending_requests("removals"))

    def test_confirmed_daemon_rejection_clears_marker_but_client_error_retains_it(self):
        for stderr, pending in (("Error response from daemon: image in use", False), ("Docker connection lost", True)):
            with self.subTest(stderr=stderr), tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"CI_FLEET_ROOT_PREFIX": directory, "PATH": directory}):
                boot = Path(directory) / "proc/sys/kernel/random/boot_id"
                boot.parent.mkdir(parents=True)
                boot.write_text(BOOT_ID)
                client = Path(directory) / "docker"
                client.write_text(f"#!/bin/sh\necho '{stderr}' >&2\nexit 1\n")
                client.chmod(0o700)
                with self.assertRaises(ValueError):
                    cleanup.mutate("image", "rm", IMAGE, timeout=1)
                self.assertEqual(cleanup.coordination.pending_requests("removals"), pending)

    def test_terminal_run_labels_only_report_and_unlabelled_compose_is_unknown(self):
        accounting = usage()
        accounting["Containers"] = [
            {"Id": "terminal", "State": "running", "Labels": {"com.docker.compose.project": "fixture", cleanup.PREFIX + "run-id": "123"}},
            {"Id": "legacy", "State": "running", "Labels": {"com.docker.compose.project": "fixture-123"}},
        ]
        with patch.object(cleanup.RunStatus, "get", side_effect=["terminal-run-leftover", "unknown-missing-run-identity"]), \
             patch.object(cleanup, "mutate") as mutate, redirect_stdout(io.StringIO()) as output:
            cleanup.report_workloads(accounting)
        self.assertIn("terminal-run-leftover", output.getvalue())
        self.assertIn("unknown-missing-run-identity", output.getvalue())
        mutate.assert_not_called()

    def test_run_status_uses_exact_attempt_and_keeps_credentials_out_of_report(self):
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({"id": 123, "run_attempt": 2, "status": "completed"}).encode()
        opener = unittest.mock.Mock()
        opener.open.return_value = response
        with patch.object(cleanup.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout="fixture-token")), \
             patch.object(cleanup.urllib.request, "build_opener", return_value=opener):
            runs = cleanup.RunStatus()
            self.assertEqual(runs.get("example-org/example-app", "123", "2"), "terminal-run-leftover")
            self.assertEqual(runs.get("example-org/example-app", "123", "2"), "terminal-run-leftover")
        opener.open.assert_called_once()
        request = opener.open.call_args.args[0]
        self.assertTrue(request.full_url.endswith("/runs/123/attempts/2"))

    def test_run_diagnostics_http_failures_do_not_abort_maintenance(self):
        for failure in (cleanup.http.client.IncompleteRead(b"partial"), cleanup.http.client.BadStatusLine("invalid")):
            with self.subTest(failure=failure):
                response = unittest.mock.MagicMock()
                response.__enter__.return_value.read.side_effect = failure
                opener = unittest.mock.Mock()
                opener.open.return_value = response
                accounting = {"Containers": [{"Id": "fixture", "State": "exited", "Labels": {
                    "com.docker.compose.project": "fixture",
                    cleanup.PREFIX + "repository": "example-org/example-app",
                    cleanup.PREFIX + "run-id": "123", cleanup.PREFIX + "run-attempt": "2"}}]}
                with patch.object(cleanup.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout="fixture-token")), \
                     patch.object(cleanup.urllib.request, "build_opener", return_value=opener), redirect_stdout(io.StringIO()) as output:
                    cleanup.report_workloads(accounting)
                self.assertIn("unknown-api-unavailable", output.getvalue())
                self.assertNotIn("fixture-token", output.getvalue())

    def test_run_diagnostics_enforce_total_deadline_during_headers_and_body(self):
        for phase in ("headers", "body"):
            with self.subTest(phase=phase):
                response = unittest.mock.MagicMock()
                payload = json.dumps({"id": 123, "run_attempt": 2, "status": "completed"}).encode()

                def slow_response(*_args, **_kwargs):
                    # Simulate a peer that keeps the connection active past the deadline.
                    for _ in range(20):
                        time.sleep(0.025)
                    return response if phase == "headers" else payload

                response.__enter__.return_value.read.side_effect = slow_response if phase == "body" else lambda *_args: payload
                opener = unittest.mock.Mock()
                opener.open.side_effect = slow_response if phase == "headers" else lambda *_args, **_kwargs: response
                runs = cleanup.RunStatus()
                runs.token = "fixture-token"
                runs.deadline = time.monotonic() + 0.1
                previous_handler = signal.getsignal(signal.SIGALRM)
                started = time.monotonic()
                with patch.object(cleanup.urllib.request, "build_opener", return_value=opener):
                    self.assertEqual(runs.get("example-org/example-app", "123", "2"), "unknown-api-unavailable")
                self.assertLess(time.monotonic() - started, 0.4)
                self.assertEqual(signal.getsignal(signal.SIGALRM), previous_handler)
                self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))

    def test_slow_run_diagnostics_reserve_time_for_health_inventory(self):
        clock = [100.0]
        def token(*_args, **_kwargs):
            clock[0] += 3
            return subprocess.CompletedProcess([], 0, stdout="fixture-token")
        def unavailable(*_args, **kwargs):
            clock[0] += kwargs["timeout"]
            raise TimeoutError("slow GitHub response")
        opener = unittest.mock.Mock()
        opener.open.side_effect = unavailable
        with patch.object(cleanup.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(cleanup.subprocess, "run", side_effect=token), \
             patch.object(cleanup.urllib.request, "build_opener", return_value=opener):
            runs = cleanup.RunStatus()
            self.assertEqual(runs.get("example-org/example-app", "123", "2"), "unknown-api-unavailable")
            self.assertEqual(runs.get("example-org/example-app", "124", "1"), "unknown-lookup-budget")
        self.assertLessEqual(clock[0] - 100, 5)
        opener.open.assert_called_once()

    def test_success_failure_cancellation_timeout_host_layer_without_trap(self):
        for outcome in ("success", "failure", "cancellation", "timeout"):
            with self.subTest(outcome=outcome):
                accounting = usage()
                accounting["Containers"] = [{"Id": outcome, "State": "exited", "Labels": {
                    cleanup.PREFIX + "managed": "true", cleanup.PREFIX + "instance": "fixture", cleanup.PREFIX + "expires-at": "1"}}]
                calls = []
                with patch.object(cleanup.coordination, "docker", side_effect=lambda *args: calls.append(args) or ""), \
                     patch.object(cleanup, "mutate") as mutate, redirect_stdout(io.StringIO()):
                    cleanup.cleanup_expired(apply=True, instance="fixture", usage=accounting)
                mutate.assert_called_once_with("rm", outcome, timeout=30)
                accounting["Containers"][0]["State"] = "running"
                with patch.object(cleanup.coordination, "docker", return_value=""), patch.object(cleanup, "mutate") as mutate, redirect_stdout(io.StringIO()):
                    cleanup.cleanup_expired(apply=True, instance="fixture", usage=accounting)
                mutate.assert_not_called()

    def test_referenced_and_unowned_volumes_survive(self):
        labels = {cleanup.PREFIX + "managed": "true", cleanup.PREFIX + "instance": "fixture", cleanup.PREFIX + "expires-at": "1"}
        def docker(*args):
            if args[:2] == ("volume", "ls"):
                return "owned"
            if args[:2] == ("volume", "inspect"):
                return json.dumps([{"Labels": labels}])
            return "stopped-container"
        with patch.object(cleanup.coordination, "docker", side_effect=docker), patch.object(cleanup, "mutate") as mutate, redirect_stdout(io.StringIO()):
            cleanup.cleanup_expired(apply=True, instance="fixture", usage={"Containers": []})
        mutate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
