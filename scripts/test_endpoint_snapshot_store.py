#!/usr/bin/env python3
"""Exercise protected files, interrupted publication, and bounded reads."""

import errno
import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

import endpoint_snapshot_store as store


class SnapshotStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.owner = os.geteuid()
        self.initial_gid = os.getegid()
        self.observer_gid = self.initial_gid
        if self.owner == 0:
            self.observer_gid = 65532
        self.addCleanup(patch.stopall)
        patch.object(store, "OWNER_UID", self.owner).start()
        patch.object(store, "INITIAL_GID", self.initial_gid).start()
        self.base = self.root / "observer"
        self.exports = self.base / "exports"
        self.exports.mkdir(parents=True)
        for directory in (self.base, self.exports):
            directory.chmod(0o750)
            os.chown(directory, -1, self.observer_gid)
        self.endpoint = self.exports / "endpoint.json"

    def write(self, path: Path, value: bytes, *, mode: int = 0o600, gid: int | None = None) -> None:
        path.write_bytes(value)
        path.chmod(mode)
        if gid is not None:
            os.chown(path, -1, gid)

    def assert_error(self, code: str, operation, *args, **kwargs) -> store.StoreError:
        with self.assertRaises(store.StoreError) as caught:
            operation(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)
        return caught.exception

    def publish(self, value: dict) -> None:
        store.publish_endpoint(self.base, value, self.observer_gid)

    def read(self) -> dict:
        return store.read_endpoint(self.base, self.observer_gid)

    def test_private_exports_reject_access_default_and_uninspectable_acls(self) -> None:
        self.publish({'incumbent': True})
        before = self.endpoint.read_bytes()
        for attribute in ('system.posix_acl_access', 'system.posix_acl_default', 'denied'):
            with self.subTest(attribute=attribute):
                def acl(descriptor, name):
                    if attribute == 'denied':
                        raise OSError(errno.EACCES, 'denied')
                    if name == attribute:
                        return b'extended-acl'
                    raise OSError(errno.ENODATA, 'absent')
                with patch.object(store.os, 'getxattr', side_effect=acl):
                    self.assert_error('unsafe_path', self.publish, {'replacement': True})
                    self.assert_error('unsafe_path', self.read)
                self.assertEqual(self.endpoint.read_bytes(), before)

    def test_export_file_acl_is_rejected_without_replacing_incumbent(self) -> None:
        self.publish({'incumbent': True})
        before = self.endpoint.read_bytes()
        def acl(descriptor, name):
            if stat.S_ISREG(os.fstat(descriptor).st_mode):
                return b'extended-acl'
            raise OSError(errno.ENODATA, 'absent')
        with patch.object(store.os, 'getxattr', side_effect=acl):
            self.assert_error('unsafe_file', self.read)
            self.assert_error('unsafe_file', self.publish, {'replacement': True})
        self.assertEqual(self.endpoint.read_bytes(), before)

    def test_atomic_publication_has_protected_metadata(self) -> None:
        self.publish({"schema_version": 1, "controller": "example"})
        self.assertEqual(self.read(), {"schema_version": 1, "controller": "example"})
        info = self.endpoint.stat()
        self.assertEqual((info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode), info.st_nlink),
                         (self.owner, self.observer_gid, 0o640, 1))
        self.assertEqual(list(self.exports.iterdir()), [self.endpoint])

    def test_sources_reject_symlinks_hard_links_and_fifo_without_opening(self) -> None:
        source = self.root / "source.json"
        self.write(source, b'{"valid":true}')
        link = self.root / "linked.json"
        link.symlink_to(source)
        self.assert_error("unsafe_file", store.read_json, link)
        link.unlink()
        os.link(source, link)
        self.assert_error("unsafe_file", store.read_json, source)
        link.unlink()
        fifo = self.root / "fifo"
        os.mkfifo(fifo, 0o600)
        with patch.object(store.os, "open", wraps=os.open) as opened:
            self.assert_error("unsafe_file", store.read_bytes, fifo)
        self.assertFalse(any(call.args[0] == "fifo" for call in opened.call_args_list))

    def test_ancestry_rejects_symlinks_and_writable_directories(self) -> None:
        source = self.root / "source.json"
        self.write(source, b"{}")
        link = self.root / "redirected"
        link.symlink_to(self.root, target_is_directory=True)
        self.assert_error("unsafe_path", store.read_json, link / source.name)
        self.root.chmod(0o770)
        self.assert_error("unsafe_path", store.read_json, source)
        self.root.chmod(0o700)
        self.assert_error("unsafe_path", store.read_json, self.root / ".." / self.root.name / source.name)

    def test_source_exact_modes_owner_and_group(self) -> None:
        source = self.root / "source.json"
        self.write(source, b"{}", mode=0o644)
        self.assert_error("unsafe_file", store.read_json, source)
        self.assertEqual(store.read_json(source, modes=(0o644,)), {})
        self.assert_error("unsafe_file", store.read_json, source, modes=(0o644,), gid=self.initial_gid + 1)
        with patch.object(store, "OWNER_UID", self.owner + 1):
            self.assert_error("unsafe_path" if self.owner else "unsafe_file", store.read_json, source,
                              modes=(0o644,))

    def test_json_rejects_duplicates_nonfinite_invalid_utf8_and_non_objects(self) -> None:
        source = self.root / "source.json"
        for value in (b'{"x":1,"x":2}', b'{"nested":{"x":1,"x":2}}', b'{"x":NaN}',
                      b'{"x":Infinity}', b'{"x":1e999}', b'{"x":"\xff"}', b"[]", b"not json"):
            with self.subTest(value=value):
                self.write(source, value)
                self.assert_error("invalid_json", store.read_json, source)

    def test_public_decoder_bounds_bounded_command_output(self) -> None:
        self.assertEqual(store.decode_json(b'{"valid":true}'), {"valid": True})
        self.assert_error("too_large", store.decode_json, b" " * (store.MAX_ENDPOINT_BYTES + 1))
        self.assert_error("invalid_json", store.decode_json, b'{"x":1,"x":2}')
        self.assert_error("invalid_json", store.decode_json, b'{"x":1e999}')
        nested = b'{"x":' * store.MAX_JSON_DEPTH + b"{}" + b"}" * store.MAX_JSON_DEPTH
        self.assert_error("invalid_json", store.decode_json, nested)

    def test_read_limits_size_and_growth_after_metadata(self) -> None:
        source = self.root / "source.json"
        self.write(source, b"12345")
        self.assert_error("too_large", store.read_bytes, source, max_bytes=4)
        self.write(source, b"1")
        original_read = os.read
        grew = False

        def read_growing(descriptor, size):
            nonlocal grew
            if not grew:
                grew = True
                source.write_bytes(b"12345")
            return original_read(descriptor, size)

        with patch.object(store.os, "read", side_effect=read_growing):
            self.assert_error("too_large", store.read_bytes, source, max_bytes=4)

    def test_source_swap_between_stat_and_open_is_rejected(self) -> None:
        source = self.root / "source.json"
        replacement = self.root / "replacement.json"
        self.write(source, b'{"old":true}')
        self.write(replacement, b'{"new":true}')
        original_open = os.open

        def swapping_open(name, flags, *args, **kwargs):
            if name == source.name:
                replacement.replace(source)
            return original_open(name, flags, *args, **kwargs)

        with patch.object(store.os, "open", side_effect=swapping_open):
            self.assert_error("unsafe_file", store.read_json, source)

    def test_opened_parent_is_pinned_when_path_becomes_a_symlink(self) -> None:
        parent = self.root / "source"
        parent.mkdir(mode=0o700)
        source = parent / "record.json"
        self.write(source, b'{"approved":true}')
        outside = self.root / "outside"
        outside.mkdir(mode=0o700)
        self.write(outside / source.name, b'{"unrelated":true}')
        moved = self.root / "moved"
        original_open = os.open

        def swapping_parent(name, flags, *args, **kwargs):
            if name == source.name:
                parent.rename(moved)
                parent.symlink_to(outside, target_is_directory=True)
            return original_open(name, flags, *args, **kwargs)

        with patch.object(store.os, "open", side_effect=swapping_parent):
            self.assertEqual(store.read_json(source), {"approved": True})

    def test_publication_refuses_unknown_entries_without_removal(self) -> None:
        self.publish({"old": True})
        unknown = self.exports / "private.json"
        self.write(unknown, b"sensitive")
        self.assert_error("unsafe_file", self.publish, {"new": True})
        self.assertEqual(unknown.read_bytes(), b"sensitive")
        self.assertEqual(json.loads(self.endpoint.read_bytes()), {"old": True})

    def test_safe_interrupted_temp_states_are_removed_without_being_read(self) -> None:
        for name in ("endpoint.json.tmp", "jobs.json.tmp"):
            for mode, gid in ((0o600, self.initial_gid), (0o600, self.observer_gid),
                              (0o640, self.observer_gid)):
                with self.subTest(name=name, mode=mode, gid=gid):
                    temporary = self.exports / name
                    self.write(temporary, b"invalid partial contents", mode=mode, gid=gid)
                    self.publish({"valid": True})
                    self.assertEqual(self.read(), {"valid": True})
                    self.assertFalse(temporary.exists())

    def test_unsafe_temp_is_not_removed_or_reused(self) -> None:
        self.publish({"old": True})
        temporary = self.exports / "endpoint.json.tmp"
        outside = self.root / "outside"
        self.write(outside, b"must survive")
        temporary.symlink_to(outside)
        self.assert_error("unsafe_file", self.publish, {"new": True})
        self.assertTrue(temporary.is_symlink())
        self.assertEqual(outside.read_bytes(), b"must survive")
        temporary.unlink()
        os.link(outside, temporary)
        self.assert_error("unsafe_file", self.publish, {"new": True})
        self.assertEqual(outside.read_bytes(), b"must survive")
        temporary.unlink()
        self.write(temporary, b"invalid mode", mode=0o644, gid=self.observer_gid)
        self.assert_error("unsafe_file", self.publish, {"new": True})
        self.assertEqual(temporary.read_bytes(), b"invalid mode")

    def test_untrusted_final_destination_is_never_followed(self) -> None:
        outside = self.root / "outside"
        self.write(outside, b"must survive")
        self.endpoint.symlink_to(outside)
        self.assert_error("unsafe_file", self.publish, {"new": True})
        self.assertTrue(self.endpoint.is_symlink())
        self.assertEqual(outside.read_bytes(), b"must survive")
        self.endpoint.unlink()
        self.write(self.endpoint, b"{}", mode=0o644, gid=self.observer_gid)
        self.assert_error("unsafe_file", self.publish, {"new": True})

    def test_failed_before_chown_or_rename_preserves_incumbent_and_recovers(self) -> None:
        for function in ("fchown", "replace"):
            with self.subTest(function=function):
                self.publish({"old": True})
                with patch.object(store.os, function, side_effect=OSError(errno.EIO, "private details")):
                    self.assert_error("io", self.publish, {"new": True})
                self.assertEqual(self.read(), {"old": True})
                self.assertTrue((self.exports / "endpoint.json.tmp").exists())
                self.publish({"recovered": True})
                self.assertEqual(self.read(), {"recovered": True})

    def test_quota_and_payload_bounds_preserve_incumbent(self) -> None:
        self.publish({"old": True})
        self.assert_error("too_large", self.publish, {"large": "x" * store.MAX_ENDPOINT_BYTES})
        self.assertEqual(self.read(), {"old": True})
        with patch.object(store, "MAX_EXPORT_BYTES", self.endpoint.stat().st_size):
            self.assert_error("too_large", self.publish, {"new": True})
        self.assertEqual(self.read(), {"old": True})
        jobs = self.exports / "jobs.json"
        self.write(jobs, b"x" * (store.MAX_JOB_BYTES + 1), mode=0o640, gid=self.observer_gid)
        self.assert_error("too_large", self.publish, {"new": True})

    def test_publication_lock_is_bounded_and_uses_no_extra_file(self) -> None:
        descriptor = os.open(self.exports, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assert_error("busy", self.publish, {"new": True})
        finally:
            os.close(descriptor)
        self.assertEqual(list(self.exports.iterdir()), [])

    def test_missing_publication_directories_are_not_created(self) -> None:
        missing = self.root / "not-provisioned"
        self.assert_error("not_found", store.publish_endpoint, missing, {}, self.observer_gid)
        self.assertFalse(missing.exists())

    def test_directory_modes_and_group_are_exact(self) -> None:
        self.base.chmod(0o755)
        self.assert_error("unsafe_path", self.publish, {})
        self.base.chmod(0o750)
        self.exports.chmod(0o700)
        self.assert_error("unsafe_path", self.publish, {})
        self.exports.chmod(0o750)
        self.assert_error("unsafe_path", store.read_endpoint, self.base, self.observer_gid + 1)

    def test_read_deadline_and_missing_file_errors_are_controlled(self) -> None:
        source = self.root / "source.json"
        self.assert_error("not_found", store.read_json, source)
        self.write(source, b"{}")
        with patch.object(store.time, "monotonic", side_effect=(0.0, 3.0)):
            self.assert_error("io", store.read_bytes, source)

    def test_repeated_reads_and_failures_close_all_descriptors(self) -> None:
        source = self.root / "source.json"
        self.write(source, b"{}")
        before = len(os.listdir("/proc/self/fd"))
        for _ in range(10):
            self.assertEqual(store.read_json(source), {})
            self.publish({"valid": True})
            self.assertEqual(self.read(), {"valid": True})
            self.assert_error("not_found", store.read_bytes, self.root / "missing")
        self.assertEqual(len(os.listdir("/proc/self/fd")), before)


if __name__ == "__main__":
    unittest.main()
