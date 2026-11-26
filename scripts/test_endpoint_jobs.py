"""Fixture-only coverage for protected job collection and publication."""
import fcntl
import os
from pathlib import Path
import struct
import tempfile
import time
import unittest
from unittest.mock import patch
import zlib

import endpoint_jobs as jobs
import endpoint_snapshot as snapshot
import endpoint_snapshot_store as store
from test_endpoint_snapshot import BEARER, CONTROLLER, NOW, documents

SCALE = "example-ci-01-scale-set"
BOOT = "b0000000-0000-4000-8000-000000000001"
EPOCH = "e0000000-0000-4000-8000-000000000001"
GEN = "a0000000-0000-4000-8000-000000000001"
GEN2 = "a0000000-0000-4000-8000-000000000002"
HEADERS = {"Authorization": "Bearer " + BEARER}


def frame(record):
    payload = store.encode_json(record).rstrip(b"\n")
    return (struct.pack("<II", len(payload), zlib.crc32(payload)) + payload).ljust(jobs.FRAME, b"\0")


def active(job="opaque:job/17"):
    return {"state": "running", "job_id": job, "owner": "example", "repository": "build",
            "run_id": "9007199254740993", "start_observed_at": NOW - 3, "last_observed_at": NOW - 3,
            "activity_epoch": EPOCH, "runner_name": "worker-17", "queued_at": NOW - 30}


def terminal(job="opaque:17"):
    return {"state": "completed", "job_id": job, "last_observed_at": NOW - 2,
            "completion_observed_at": NOW - 2, "finished_at": NOW - 4, "result": "failure"}


def state(slots, generation=GEN):
    return {"version": 1, "boot": BOOT, "controller": CONTROLLER, "scale_set": SCALE, "generation": generation,
            "activity_epoch": EPOCH, "session_open": True, "dirty": False, "poll_at": NOW - 1,
            "gaps": {"initial_unknown": 1}, "slots": slots}


class JobExportTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.base = self.root / "var/lib/ci-fleet-observer"
        self.exports = self.base / "exports"
        self.private = self.base / "private"
        self.exports.mkdir(parents=True, mode=0o750)
        self.private.mkdir(mode=0o700)
        self.base.chmod(0o750)
        self.gid = os.getegid()
        self.clock = (BOOT, 100 * 10**9)
        self.writer = None
        self.addCleanup(self.stop_writer)
        self.addCleanup(patch.stopall)
        patch.object(snapshot, "allowance_clock", side_effect=lambda root: self.clock).start()
        patch.object(snapshot, "collect_documents", return_value=(documents(), {})).start()
        self.reader = snapshot.SnapshotReader(self.base, observer_gid=self.gid, controller=CONTROLLER, backend_bearer=BEARER)

    def write(self, name, content):
        path = self.private / name
        path.write_bytes(content)
        path.chmod(0o600)

    def stop_writer(self):
        if self.writer is not None:
            os.close(self.writer)
            self.writer = None

    def prepare(self, rows, **updates):
        self.write("records", b"".join(map(frame, rows)))
        self.update(len(rows), **updates)
        if self.writer is None:
            self.writer = os.open(self.private / "records", os.O_RDWR)
            fcntl.flock(self.writer, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def update(self, slots, **updates):
        committed_file = jobs.record_file_digest((self.private / "records").stat())
        self.write("state.json", store.encode_json({**state(slots), "record_count": slots,
                                                   "record_file": committed_file, **updates}))

    def publish(self, elapsed=0):
        return snapshot.publish(CONTROLLER, observer_gid=self.gid, root=self.root, now=NOW + elapsed)["sources"]["jobs"]

    def read(self, elapsed=0):
        return self.reader.read_job_history(HEADERS, now=NOW + elapsed)

    def test_pending_and_stale_mask_current_work_but_keep_historical_results(self):
        self.prepare([terminal(), active()])
        pending = self.publish()["data"]
        self.assertTrue(pending["pending"])
        self.assertFalse((self.exports / "jobs.json").exists())

        self.clock = (BOOT, 160 * 10**9)
        published = self.publish(60)["data"]
        self.assertEqual(published["generation"], GEN)
        self.assertFalse(published["pending"])
        history = self.read(60)
        self.assertEqual(history["records"][0]["current_activity"], "running")
        self.assertFalse(history["current_activity_verified"])
        self.assertEqual(history["records"][0]["run_url"], "https://github.com/example/build/actions/runs/9007199254740993")
        self.assertNotIn("job_url", history["records"][0])
        self.assertEqual(history["records"][1]["result"], "failure")

        inode = (self.exports / "jobs.json").stat().st_ino
        self.update(2, generation=GEN2, poll_at=NOW + 89)
        self.clock = (BOOT, 190 * 10**9)
        self.assertTrue(self.publish(90)["data"]["pending"])
        self.assertEqual((self.exports / "jobs.json").stat().st_ino, inode)
        self.assertEqual(self.read(90)["records"][0]["current_activity"], "unknown")
        self.assertEqual(self.read(90)["records"][1]["result"], "failure")

        self.clock = (BOOT, 220 * 10**9)
        self.assertFalse(self.publish(120)["data"]["pending"])
        self.assertEqual(self.read(120)["records"][0]["current_activity"], "running")
        self.assertEqual(self.read(210)["records"][0]["current_activity"], "unknown")

    def test_failed_write_and_interrupted_reservation_enforce_cooldown(self):
        self.prepare([active()])
        self.publish()
        self.clock = (BOOT, 160 * 10**9)
        with patch.object(jobs, "publish_jobs", side_effect=OSError("fixture disk full")) as write:
            self.assertTrue(self.publish(60)["data"]["pending"])
            self.clock = (BOOT, 219 * 10**9)
            self.assertTrue(self.publish(119)["data"]["pending"])
            self.assertEqual(write.call_count, 1)

        self.write("attempt.json", store.encode_json({"boot": BOOT, "ticks": 220 * 10**9, "in_flight": True}))
        self.clock = (BOOT, 221 * 10**9)
        self.assertTrue(self.publish(121)["data"]["pending"])
        self.clock = (BOOT, 280 * 10**9)
        self.assertTrue(self.publish(180)["data"]["pending"])
        self.clock = (BOOT, 281 * 10**9)
        self.assertFalse(self.publish(181)["data"]["pending"])

    def test_missing_writer_and_records_mask_activity_independently(self):
        self.prepare([active(), terminal()])
        self.publish()
        self.clock = (BOOT, 160 * 10**9)
        self.assertTrue(self.publish(60)["data"]["source_available"])
        self.stop_writer()
        self.assertFalse(self.publish(61)["data"]["source_available"])
        self.assertEqual(self.read(61)["records"][0]["current_activity"], "unknown")
        (self.private / "records").unlink()
        summary = self.publish(62)["data"]
        self.assertTrue(summary["pending"])
        self.assertIn("storage_corruption", summary["gaps"])
        self.assertEqual(self.read(62)["records"][1]["result"], "failure")

    def test_same_size_private_corruption_invalidates_incumbent_activity(self):
        self.prepare([active(), terminal()])
        self.publish()
        self.clock = (BOOT, 160 * 10**9)
        self.assertTrue(self.publish(60)["data"]["source_available"])
        with (self.private / "records").open("r+b") as damaged:
            damaged.write(bytes(jobs.FRAME))
        self.assertTrue(self.publish(61)["data"]["pending"])
        self.assertEqual(self.read(61)["records"][0]["current_activity"], "unknown")
        self.assertEqual(self.read(61)["records"][1]["result"], "failure")

        self.update(2, generation=GEN2)
        self.clock = (BOOT, 220 * 10**9)
        self.assertFalse(self.publish(120)["data"]["pending"])
        self.assertEqual(self.read(120)["gaps"]["storage_corruption"], 1)
        self.assertEqual(self.read(120)["records"][0]["result"], "failure")

    def test_clock_uncertainty_invalidates_the_previous_allowance(self):
        self.prepare([active()])
        self.publish()
        self.clock = (BOOT, 160 * 10**9)
        self.assertFalse(self.publish(60)["data"]["pending"])

        with jobs.private_lock(self.base, self.gid):
            with self.assertRaisesRegex(snapshot.SnapshotError, "clock_uncertain"):
                self.publish(59)
        with jobs.private_lock(self.base, self.gid) as fd:
            self.assertEqual(store.decode_json(jobs.read_private(fd, "attempt.json")), {"uncertain": True})
        self.clock = None
        self.assertFalse(self.publish(120)["data"]["source_available"])
        self.update(1, generation=GEN2, poll_at=NOW + 121)
        self.clock = (BOOT, 221 * 10**9)
        self.assertTrue(self.publish(121)["data"]["pending"])
        self.clock = (BOOT, 280 * 10**9)
        self.assertTrue(self.publish(180)["data"]["pending"])
        self.clock = (BOOT, 281 * 10**9)
        self.assertFalse(self.publish(181)["data"]["pending"])

    def test_job_change_during_export_cannot_clear_pending(self):
        self.prepare([active()])
        self.publish()
        self.clock = (BOOT, 160 * 10**9)
        write = jobs.publish_jobs

        def interleave(*args, **kwargs):
            self.assertTrue(store.read_endpoint(self.base, self.gid)["sources"]["jobs"]["data"]["pending"])
            write(*args, **kwargs)
            self.update(1, generation=GEN2, poll_at=NOW + 60)

        with patch.object(jobs, "publish_jobs", side_effect=interleave):
            summary = self.publish(60)["data"]
        self.assertTrue(summary["pending"])
        self.assertEqual(summary["generation"], GEN)
        self.assertEqual(self.read(60)["records"][0]["current_activity"], "unknown")

    def test_authentication_and_unsafe_private_entries(self):
        with patch.object(snapshot, "read_jobs") as read:
            with self.assertRaisesRegex(snapshot.SnapshotError, "authentication_failed"):
                self.reader.read_job_history({"Remote-User": "operator"})
            read.assert_not_called()
        self.prepare([terminal()])
        (self.private / "attempt.json").symlink_to("state.json")
        self.assertEqual(self.publish()["availability"], "unavailable")
        self.assertEqual(store.read_endpoint(self.base, self.gid)["sources"]["controller"]["data"]["busy"], 1)

    def test_projection_bounds_order_corruption_and_overhead(self):
        rows = [{**active(f"job/{index}"), "display_name": "\u2028" * 170, "job_id": f"job/{index}"}
                for index in range(jobs.SLOTS - 2)]
        rows += [terminal(), {**terminal("wrong"), "run_url": "https://invalid.example/"}]
        rows[0]["activity_epoch"] = GEN2
        self.prepare(rows)
        self.publish()
        self.clock = (BOOT, 160 * 10**9)
        started = time.monotonic()
        published = self.publish(60)
        duration = time.monotonic() - started
        history = self.read(60)
        self.assertFalse(published["data"]["pending"])
        self.assertEqual(history["gaps"]["storage_corruption"], 1)
        self.assertEqual(history["retained"], jobs.SLOTS - 1)
        self.assertGreater(history["omitted_nonterminal"], 0)
        self.assertFalse(history["current_activity_verified"])
        self.assertTrue(all(row["state"] == "unknown" for row in history["records"]))
        path = self.exports / "jobs.json"
        allocated = path.stat().st_blocks * 512
        print(f"job projection fixture: retained {history['retained']}, exported {history['exported']}, "
              f"omitted {history['omitted']}, {path.stat().st_size} file bytes, {allocated} allocated bytes, "
              f"publication {duration:.3f}s", flush=True)
        self.assertLessEqual(path.stat().st_size, store.MAX_JOB_BYTES)
        self.assertLessEqual(sum(item.stat().st_blocks * 512 for item in self.private.iterdir()), 32 << 20)


if __name__ == "__main__":
    unittest.main()
