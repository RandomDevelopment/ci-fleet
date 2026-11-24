#!/usr/bin/env python3
"""Unprivileged fixtures: no daemon, host proc probing, services or mounts."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

import disk_admission_sample as sample


def mounts(upper, extra=""):
    return f"10 1 0:10 / / rw - overlay overlay rw,lowerdir=/lower,upperdir={upper},workdir=/work\n" + extra


class MappingTests(unittest.TestCase):
    def test_instance_pattern_matches_managed_controller_keys(self):
        schema = json.loads((Path(__file__).resolve().parent.parent / "templates/config-repository/fleet.schema.json").read_text())
        self.assertEqual(schema["properties"]["controllers"]["propertyNames"], {"$ref": "#/$defs/slug"})
        self.assertEqual(sample.INSTANCE_PATTERN.pattern, schema["$defs"]["slug"]["pattern"])
        for value in ("example", "1", "a" * 63, "example-2"):
            self.assertIsNotNone(sample.INSTANCE_PATTERN.fullmatch(value))
        for value in ("", "A", "a_b", "a.b", "-example", "a" * 64, "example\n"):
            self.assertIsNone(sample.INSTANCE_PATTERN.fullmatch(value))

    def test_classic_store_and_split_containerd_store_without_runner(self):
        upper, base = sample.backing_path(mounts("/docker/overlay2/id/diff"), "overlay2", "/docker", "/home/runner/_work")
        self.assertEqual((str(upper), str(base)), ("/docker/overlay2/id/diff", "/docker/overlay2"))
        upper, base = sample.backing_path(mounts("/snapshots/io.containerd.snapshotter.v1.overlayfs/snapshots/12/fs"), "overlayfs", "/separate/docker", "/tmp")
        self.assertEqual(str(base), "/snapshots/io.containerd.snapshotter.v1.overlayfs/snapshots")
        self.assertNotIn("/separate/docker", str(upper))

    def test_uncertain_mappings_never_fall_back_to_root(self):
        for text, driver in [(mounts("/elsewhere/diff"), "overlay2"),
                             (mounts("/docker/overlay2/id/diff"), "vfs"),
                             (mounts("/docker/overlay2/id/diff", "11 10 0:11 / /tmp rw - tmpfs tmpfs rw\n"), "overlay2"),
                             ("10 1 8:1 / / rw - ext4 /dev/test rw\n", "overlay2"),
                             (mounts("/docker/overlay2/../diff"), "overlay2")]:
            with self.subTest(text=text), self.assertRaises((ValueError, IndexError)):
                sample.backing_path(text, driver, "/docker", "/tmp")

    def test_roles_same_device_grouped_and_failures_remain_null(self):
        class FakeCollector(sample.Collector):
            def docker(self, *args):
                if args[0] == "info":
                    return {"root": "/docker", "driver": "overlay2"}
                if args[0] == "image":
                    assert args[-1] == "sha256:" + "a" * 64
                    return None
                raise AssertionError(args)
            def backing(self, service, *args):
                return Path("/fixture"), Path("/same-store")
        statfs = types.SimpleNamespace(f_blocks=100, f_frsize=1024**3, f_bavail=40, f_files=1000, f_ffree=800)
        with patch.dict(os.environ, CI_FLEET_RUNNER_IMAGE="different-tag", CI_FLEET_RUNNER_RUNTIME_IMAGE_ID="sha256:" + "a" * 64), patch.object(sample, "read_small", return_value="11111111-1111-4111-8111-111111111111"), patch.object(sample.os, "open", return_value=123), patch.object(sample.os, "close"), patch.object(sample.os, "fstat", return_value=types.SimpleNamespace(st_dev=9)), patch.object(sample.os, "fstatvfs", return_value=statfs):
            result = FakeCollector().collect("example")
            self.assertEqual(len(result["filesystems"]), 1)
            self.assertEqual(result["filesystems"][0]["roles"], list(sample.ROLES))
            self.assertEqual(result["filesystems"][0]["available_bytes"], 40 * 1024**3)
            self.assertNotIn("fixture", json.dumps(result))
            with patch.object(FakeCollector, "backing", side_effect=ValueError):
                result = FakeCollector().collect("example")
            by_role = {role: fs for fs in result["filesystems"] for role in fs["roles"]}
            self.assertEqual(by_role["docker"]["state"], "measured")
            for role in ("workspace", "spool"):
                self.assertEqual(by_role[role]["state"], "unknown")
                self.assertIsNone(by_role[role]["capacity_bytes"])
                self.assertIsNone(by_role[role]["observed_at"])

    def test_pid_recreation_reuse_and_override_refused(self):
        before = dict(id="id", pid=1, started="start", running=True, mounts=[])
        # /proc stat starttime is field22 (index19 after the comm closing ')').
        process = "1 (fixture) " + " ".join(["0"] * 19 + ["10"])
        def reads(path, *args):
            if path.name == "stat":
                return process
            return mounts("/docker/overlay2/id/diff")
        collector = sample.Collector()
        for changed in ({**before, "id": "other"}, {**before, "pid": 2}, {**before, "started": "new"}):
            with patch.object(collector, "inspect", side_effect=[before, changed]), patch.object(sample, "read_small", side_effect=reads), self.assertRaises(ValueError):
                collector.backing("controller", "overlay2", "/docker", "/home/runner/_work")
        with patch.object(collector, "inspect", return_value=before), patch.object(sample, "read_small", side_effect=[process, mounts("/docker/overlay2/id/diff"), process[:-2]+"20", process[:-2]+"20"]), self.assertRaises(ValueError):
            collector.backing("controller", "overlay2", "/docker", "/home/runner/_work")
        with patch.object(collector, "inspect", return_value={**before, "mounts": [{"Destination": "/tmp"}]}), patch.object(sample, "read_small", side_effect=reads), self.assertRaises(ValueError):
            collector.backing("docker-socket-proxy", "overlay2", "/docker", "/tmp")

    def test_complete_classic_mapping_accepts_merged_views_not_backing_overrides(self):
        identities = {"controller": (101, "controller-id"), "docker-socket-proxy": (102, "proxy-id")}
        class FakeDaemon(sample.Collector):
            def docker(self, *args):
                if args[0] == "info":
                    return {"root": "/docker", "driver": "overlay2"}
                if args[0] == "image":
                    return None
                service = "controller" if args[-1] == "ci-fleet-controller-1" else "docker-socket-proxy"
                pid, identifier = identities[service]
                return dict(id=identifier, pid=pid, started="start", running=True, mounts=[],
                            labels={"com.docker.compose.project": "ci-fleet", "com.docker.compose.service": service})
        host = "1 0 8:1 / / rw - ext4 /dev/fixture rw\n"
        for identifier in ("controller-id", "proxy-id", "existing-job"):
            host += f"10 1 0:10 / /docker/overlay2/{identifier}/merged rw - overlay overlay rw,upperdir=/docker/overlay2/{identifier}/diff\n"
        process = "101 (fixture) " + " ".join(["0"] * 19 + ["10"])
        def reads(path, *args):
            if path.name == "boot_id":
                return "11111111-1111-4111-8111-111111111111"
            if str(path) == "/proc/self/mountinfo":
                return host
            if path.name == "stat":
                return process
            identifier = "controller-id" if path.parent.name == "101" else "proxy-id"
            return mounts(f"/docker/overlay2/{identifier}/diff")
        statfs = types.SimpleNamespace(f_blocks=100, f_frsize=1024**3, f_bavail=40, f_files=1000, f_ffree=800)
        with patch.dict(os.environ, CI_FLEET_RUNNER_RUNTIME_IMAGE_ID="sha256:" + "a" * 64), patch.object(sample, "read_small", side_effect=reads), patch.object(sample.os, "stat", return_value=types.SimpleNamespace(st_dev=9)), patch.object(sample.os, "open", return_value=123), patch.object(sample.os, "close"), patch.object(sample.os, "fstat", return_value=types.SimpleNamespace(st_dev=9)), patch.object(sample.os, "fstatvfs", return_value=statfs):
            result = FakeDaemon().collect("example")
            self.assertEqual(len(result["filesystems"]), 1)
            self.assertEqual(result["filesystems"][0]["roles"], list(sample.ROLES))
            self.assertEqual(result["filesystems"][0]["state"], "measured")
            normal_host = host
            for override in ("/docker/overlay2/controller-id/diff", "/docker/overlay2/other", "/docker/overlay2/controller-id/merged/subdir"):
                host = normal_host + f"11 1 8:2 / {override} rw - ext4 /dev/other rw\n"
                result = FakeDaemon().collect("example")
                self.assertEqual({role for fs in result["filesystems"] if fs["state"] == "unknown" for role in fs["roles"]}, {"workspace", "spool"})
            host = normal_host.replace("upperdir=/docker/overlay2/controller-id/diff", "upperdir=/elsewhere/diff")
            result = FakeDaemon().collect("example")
            self.assertTrue(any(fs["state"] == "unknown" for fs in result["filesystems"]))

    def test_malformed_inspect_is_controlled_unknown(self):
        valid = dict(id="id", pid=101, started="start", running=True, mounts=[],
                     labels={"com.docker.compose.project": "ci-fleet", "com.docker.compose.service": "controller"})
        statfs = types.SimpleNamespace(f_blocks=100, f_frsize=1024**3, f_bavail=40, f_files=1000, f_ffree=800)
        for value in (None, [], {**valid, "labels": None}, {**valid, "mounts": None},
                      {**valid, "mounts": [None]}, {**valid, "mounts": [{"Destination": None}]},
                      {**valid, "id": None}, {**valid, "pid": True}, {**valid, "running": None}):
            with self.subTest(value=value), patch.dict(os.environ, CI_FLEET_RUNNER_RUNTIME_IMAGE_ID="fixture"), patch.object(sample, "read_small", return_value="11111111-1111-4111-8111-111111111111"), patch.object(sample.Collector, "docker", side_effect=[{"root": "/docker", "driver": "overlay2"}, None, value]), patch.object(sample.os, "open", return_value=123), patch.object(sample.os, "close"), patch.object(sample.os, "fstat", return_value=types.SimpleNamespace(st_dev=9)), patch.object(sample.os, "fstatvfs", return_value=statfs):
                result = sample.Collector().collect("example")
                unknown = [fs for fs in result["filesystems"] if fs["state"] == "unknown"]
                self.assertEqual({role for fs in unknown for role in fs["roles"]}, {"workspace", "spool"})
                self.assertTrue(all(fs["observed_at"] is None and fs["available_bytes"] is None for fs in unknown))

    def test_deadline_prevents_another_daemon_call(self):
        collector = sample.Collector()
        collector.deadline = 0
        with patch.object(sample.subprocess, "run") as run, self.assertRaises(ValueError):
            collector.docker("info")
        run.assert_not_called()


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.owner = patch.object(sample.store, "OWNER_UID", os.getuid())
        self.owner.start()
    def tearDown(self):
        self.owner.stop()
        self.temp.cleanup()

    def test_atomic_protected_publication_and_bad_entries(self):
        path = self.root / "export"
        fd = sample.prepare(path)
        try:
            # Root-group check is the production contract. Unit publication can
            # use the local primary group without requiring chown privileges.
            original = sample.store._regular
            def local_regular(meta, **kwargs):
                kwargs["gid"] = os.getgid()
                return original(meta, **kwargs)
            with patch.object(sample.store, "_regular", side_effect=local_regular):
                sample.publish(fd, {"schema_version": 1})
                self.assertEqual((path / "measurement.json").stat().st_mode & 0o777, 0o600)
                previous = (path / "measurement.json").read_bytes()
                with patch.object(sample.os, "replace", side_effect=OSError), self.assertRaises(OSError):
                    sample.publish(fd, {"new": True})
                self.assertEqual((path / "measurement.json").read_bytes(), previous)
                (path / "stranger").write_text("preserve")
                with self.assertRaises(ValueError):
                    sample.publish(fd, {})
                self.assertEqual((path / "stranger").read_text(), "preserve")
        finally:
            os.close(fd)

    def test_symlink_directory_modes_hardlinks_fifo_and_acl(self):
        target = self.root / "target"
        target.mkdir(mode=0o700)
        link = self.root / "link"
        link.symlink_to(target)
        with self.assertRaises(OSError):
            sample.prepare(link)
        target.chmod(0o755)
        with self.assertRaises(ValueError):
            sample.prepare(target)
        target.chmod(0o700)
        with patch.object(sample.store, "_no_acl", side_effect=ValueError), self.assertRaises(ValueError):
            sample.prepare(target)
        for kind in ("symlink", "hardlink", "fifo"):
            with self.subTest(kind=kind):
                fd = sample.prepare(target)
                file = target / "measurement.json"
                if kind == "symlink":
                    file.symlink_to(self.root / "missing")
                elif kind == "hardlink":
                    source = self.root / "source"
                    source.write_text("{}")
                    source.chmod(0o600)
                    file.hardlink_to(source)
                else:
                    os.mkfifo(file, 0o600)
                try:
                    with self.assertRaises(ValueError):
                        sample.publish(fd, {})
                finally:
                    os.close(fd)
                    file.unlink()

    def test_prepare_and_scoped_remove_preserve_history_and_unknown_files(self):
        path = self.root / "export"
        history = self.root / "capacity"
        history.mkdir()
        (history / "samples.jsonl").write_text("preserve history")
        original = sample.store._regular
        def local_regular(meta, **kwargs):
            kwargs["gid"] = os.getgid()
            return original(meta, **kwargs)
        with patch.object(sample.store, "_regular", side_effect=local_regular):
            for _ in range(2):
                with patch("sys.argv", ["sample", "--output", str(path), "--prepare"]):
                    self.assertEqual(sample.main(), 0)
                self.assertFalse((path / "measurement.json").exists())
            fd = sample.prepare(path)
            try:
                sample.publish(fd, {"schema_version": 1})
            finally:
                os.close(fd)
            previous = (path / "measurement.json").read_bytes()
            (path / "stranger").write_text("keep")
            with patch("sys.argv", ["sample", "--output", str(path), "--remove"]):
                self.assertEqual(sample.main(), 1)
            self.assertEqual((path / "measurement.json").read_bytes(), previous)
            self.assertEqual((path / "stranger").read_text(), "keep")
            (path / "stranger").unlink()
            for _ in range(2):
                with patch("sys.argv", ["sample", "--output", str(path), "--remove"]):
                    self.assertEqual(sample.main(), 0)
            self.assertFalse(path.exists())
            self.assertEqual((history / "samples.jsonl").read_text(), "preserve history")
            path.symlink_to(self.root / "absent")
            with patch("sys.argv", ["sample", "--output", str(path), "--remove"]):
                self.assertEqual(sample.main(), 1)
            self.assertTrue(path.is_symlink())

    def test_concurrent_prepare_sample_remove_and_no_late_republication(self):
        path = self.root / "export"
        fd = sample.prepare(path)
        late = None
        original = sample.store._regular
        def local_regular(meta, **kwargs):
            kwargs["gid"] = os.getgid()
            return original(meta, **kwargs)
        with patch.object(sample.store, "_regular", side_effect=local_regular):
            try:
                sample.publish(fd, {"schema_version": 1})
                original_bytes = (path / "measurement.json").read_bytes()
                original_inode = path.stat().st_ino
                sample.lock_export(fd)
                with patch("sys.argv", ["sample", "--output", str(path), "--prepare"]):
                    self.assertEqual(sample.main(), 0)
                self.assertEqual(path.stat().st_ino, original_inode)
                self.assertEqual((path / "measurement.json").read_bytes(), original_bytes)
                with patch("sys.argv", ["sample", "--output", str(path)]), patch.object(sample, "Collector") as collector:
                    self.assertEqual(sample.main(), 1)
                    collector.assert_not_called()
                late = sample.prepare(path, create=False)
                release = threading.Timer(0.05, sample.fcntl.flock, args=(fd, sample.fcntl.LOCK_UN))
                release.start()
                try:
                    with patch("sys.argv", ["sample", "--output", str(path), "--remove"]):
                        self.assertEqual(sample.main(), 0)
                finally:
                    release.join()
                self.assertFalse(path.exists())
                sample.lock_export(late)
                with self.assertRaises(FileNotFoundError):
                    sample.publish(late, {"late": True})
                with patch("sys.argv", ["sample", "--output", str(path)]), patch.object(sample, "Collector") as collector:
                    self.assertEqual(sample.main(), 1)
                    collector.assert_not_called()
                self.assertFalse(path.exists())
            finally:
                os.close(fd)
                if late is not None:
                    os.close(late)

    def test_remove_lock_wait_is_bounded_and_preserves_entries(self):
        path = self.root / "export"
        fd = sample.prepare(path)
        try:
            sample.publish(fd, {"preserve": True})
            before = (path / "measurement.json").read_bytes()
            with patch.object(sample.fcntl, "flock", side_effect=BlockingIOError), patch.object(sample.time, "monotonic", side_effect=[0, 21]), patch("sys.argv", ["sample", "--output", str(path), "--remove"]):
                self.assertEqual(sample.main(), 1)
            self.assertEqual((path / "measurement.json").read_bytes(), before)
        finally:
            os.close(fd)

    def test_policy_downgrade_removes_only_after_success(self):
        source = Path(__file__).with_name("install-worker-controller.sh").read_text()
        converge = source.split("perform_converge() {", 1)[1].split("\n}\n", 1)[0]
        selection = "    if ((policy_status == 0)); then" + converge.split("    if ((policy_status == 0)); then", 1)[1].split("    if docker_network_policy_matches; then", 1)[0]
        old = self.root / "old"
        old.mkdir()
        new = self.root / "new"
        (new / "scripts").mkdir(parents=True)
        (new / "scripts/disk_admission_sample.py").write_text("fixture")
        for status, release, removed in ((0, old, True), (0, new, False), (20, old, False)):
            script = ('set -eu\nremove_disk_admission_export() { printf "REMOVED\\n"; }\nnote() { :; }\n'
                      'finish_policy() {\n' + selection + '\n}\nfinish_policy\n')
            environment = {**os.environ, "policy_status": str(status), "release_dir": str(release),
                           "mode": "upgrade", "controller_id": "example", "config_ref": "fixture",
                           "engine_ref": "fixture", "target_state": "active", "checkpoint_dir": "fixture"}
            result = subprocess.run(["bash", "-c", script], env=environment, capture_output=True, text=True)
            self.assertEqual(result.returncode, status, result.stderr)
            self.assertEqual("REMOVED" in result.stdout, removed)

    def test_sampler_failure_does_not_suppress_existing_history(self):
        scripts = self.root / "scripts"
        scripts.mkdir()
        original = Path(__file__).with_name("capacity-sample.sh").read_text()
        (scripts / "capacity-sample.sh").write_text(original)
        (scripts / "disk_admission_sample.py").write_text("raise SystemExit(1)\n")
        (scripts / "health.py").write_text("print('HISTORY_EXECUTED')\n")
        config = self.root / "etc/ci-fleet"
        config.mkdir(parents=True)
        (config / "ci-fleet.env").write_text("CI_FLEET_INSTANCE=example\n")
        result = subprocess.run(["bash", str(scripts / "capacity-sample.sh")], env={**os.environ, "CI_FLEET_TESTING": "1", "CI_FLEET_ROOT_PREFIX": str(self.root)}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("HISTORY_EXECUTED", result.stdout)


if __name__ == "__main__":
    from validation_diagnostics import TimedTestRunner
    unittest.main(testRunner=TimedTestRunner)
