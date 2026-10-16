#!/usr/bin/env python3
"""Process-ownership regressions for the trusted local stage supervisor."""
from __future__ import annotations

import importlib.util
import ctypes
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

MODULE_PATH = Path(__file__).with_name("local_validation_supervisor.py")
SPEC = importlib.util.spec_from_file_location("local_validation_supervisor", MODULE_PATH)
supervisor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(supervisor)

PRELUDE = """
import os, signal, sys, time
from pathlib import Path
root = Path(sys.argv[1])
def record():
    start = Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()[19]
    with (root / 'pids').open('ab', buffering=0) as handle:
        handle.write((str(os.getpid()) + ' ' + start + '\\n').encode())
def ready():
    (root / 'ready').touch()
def wait_ready():
    deadline = time.monotonic() + 5
    while not (root / 'ready').exists():
        if time.monotonic() >= deadline:
            raise RuntimeError('child did not become ready')
        time.sleep(.005)
record()
"""


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="local-supervisor-test-")
        self.root = Path(self.temporary.name)
        self.log = self.root / "stage.log"
        self.env = {"PATH": os.defpath, "LANG": "C.UTF-8"}

    def tearDown(self):
        # A broken regression must not leave its owned live sleepers running.
        for pid, start in self.recorded():
            try:
                if supervisor._identity(pid)[1] == start:
                    descriptor = os.pidfd_open(pid)
                    try:
                        if supervisor._identity(pid)[1] == start:
                            signal.pidfd_send_signal(descriptor, signal.SIGKILL)
                    finally:
                        os.close(descriptor)
            except (FileNotFoundError, ProcessLookupError):
                pass
        self.temporary.cleanup()

    def recorded(self):
        path = self.root / "pids"
        return [tuple(map(int, line.split())) for line in path.read_text().splitlines()] if path.exists() else []

    def assert_reaped(self, minimum=1):
        recorded = self.recorded()
        self.assertGreaterEqual(len(recorded), minimum)
        for pid, start in recorded:
            try:
                self.assertNotEqual(supervisor._identity(pid)[1], start, f"owned process {pid} remains")
            except (FileNotFoundError, ProcessLookupError):
                pass

    def execute(self, source, timeout=5, grace=.2):
        command = [sys.executable, "-I", "-c", PRELUDE + source, str(self.root)]
        return supervisor.run(command, self.root, self.env, self.log, timeout, grace)

    def test_success_reaps_session_escaped_child_gracefully(self):
        self.execute("""
child = os.fork()
if child == 0:
    os.setsid()
    record()
    def finish(*_):
        (root / 'cleaned').touch()
        os._exit(0)
    signal.signal(signal.SIGTERM, finish)
    ready()
    while True: signal.pause()
wait_ready()
""")
        self.assertTrue((self.root / "cleaned").exists())
        self.assert_reaped(2)

    def test_double_fork_is_adopted_and_forced_cleanup_fails(self):
        with self.assertRaisesRegex(RuntimeError, "forced termination"):
            self.execute("""
child = os.fork()
if child == 0:
    os.setsid()
    record()
    if os.fork(): os._exit(0)
    record()
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    ready()
    while True: signal.pause()
os.waitpid(child, 0)
wait_ready()
""")
        self.assert_reaped(3)

    def test_term_handler_can_wait_for_delayed_cleanup_helper(self):
        helper = PRELUDE + "time.sleep(.15); (root / 'cleaned').touch()"
        self.execute(f"""
import subprocess
if os.fork() == 0:
    os.setsid()
    record()
    def finish(*_):
        subprocess.run([sys.executable, '-I', '-c', {helper!r}, str(root)], check=True)
        os._exit(0)
    signal.signal(signal.SIGTERM, finish)
    ready()
    while True: signal.pause()
wait_ready()
""", grace=1)
        self.assertTrue((self.root / "cleaned").exists())
        self.assert_reaped(3)

    def test_rediscover_children_forked_during_cleanup(self):
        self.execute("""
if os.fork() == 0:
    os.setsid()
    generation = 0
    record()
    def finish(*_):
        global generation
        previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
        if generation == 3: os._exit(0)
        child = os.fork()
        if child: os._exit(0)
        generation += 1
        os.setsid()
        # Allow rediscovery before recording without reentering this handler.
        time.sleep(.02)
        record()
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)
    signal.signal(signal.SIGTERM, finish)
    ready()
    while True: signal.pause()
wait_ready()
""", grace=3)
        self.assert_reaped(5)

    def test_term_ignored_by_leader_times_out_after_cleanup(self):
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            self.execute("""
signal.signal(signal.SIGTERM, signal.SIG_IGN)
while True: signal.pause()
""", timeout=.1, grace=.15)
        self.assertLess(time.monotonic() - started, 3)
        self.assert_reaped()

    def test_leader_failure_is_preserved_after_child_cleanup(self):
        with self.assertRaisesRegex(RuntimeError, "check exited 7"):
            self.execute("""
if os.fork() == 0:
    os.setsid()
    record()
    ready()
    while True: signal.pause()
wait_ready()
sys.exit(7)
""")
        self.assert_reaped(2)

    def test_no_other_stage_or_unrelated_process_is_signalled(self):
        unrelated = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(30)"],
                                     start_new_session=True)
        try:
            self.test_double_fork_is_adopted_and_forced_cleanup_fails()
            self.assertIsNone(unrelated.poll())
        finally:
            unrelated.terminate()
            unrelated.wait(timeout=5)

    def test_setup_failure_launches_no_command(self):
        with mock.patch.object(supervisor, "_enable_subreaper", side_effect=OSError("setup denied")), \
                mock.patch.object(supervisor.subprocess, "Popen") as launch:
            result = supervisor._supervise({}, None)
        launch.assert_not_called()
        self.assertFalse(result["launched"])
        self.assertTrue(result["cleanup_complete"])
        self.assertIn("setup denied", result["error"])

    def test_parent_does_not_become_a_subreaper(self):
        value = ctypes.c_int()
        libc = ctypes.CDLL(None, use_errno=True)
        self.assertEqual(libc.prctl(37, ctypes.byref(value), 0, 0, 0), 0)
        before = value.value
        self.execute("pass")
        self.assertEqual(libc.prctl(37, ctypes.byref(value), 0, 0, 0), 0)
        self.assertEqual(value.value, before)

    def test_launch_failure_reports_error_and_restores_handlers(self):
        before = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
        with self.assertRaisesRegex(RuntimeError, "supervisor failed: FileNotFoundError"):
            supervisor.run([str(self.root / "missing-command")], self.root, self.env, self.log, 5, .1)
        self.assertEqual(before, {number: signal.getsignal(number) for number in before})

    def test_supervisor_imports_do_not_use_candidate_directory(self):
        (self.root / "subprocess.py").write_text("raise RuntimeError('candidate import executed')")
        self.execute("pass")
        self.assert_reaped()

    def test_missing_or_incomplete_supervisor_result_fails_closed(self):
        for response in ("", "{}", '{"version":1,"cleanup_complete":false}',
                         '{"version":1,"cleanup_complete":true,"forced":false,"timed_out":false}'):
            with self.subTest(response=response):
                program = "import sys; sys.stdin.buffer.read(); print(" + repr(response) + ")"
                with mock.patch.object(supervisor, "_SUPERVISOR_PROGRAM", program):
                    with self.assertRaisesRegex(RuntimeError, "supervisor result"):
                        self.execute("raise RuntimeError('candidate must not run')")

    def test_candidate_cannot_replace_supervisor_for_later_stage(self):
        copied = self.root / "trusted-supervisor.py"
        copied.write_text(MODULE_PATH.read_text())
        spec = importlib.util.spec_from_file_location("copied_supervisor", copied)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        forged = {"version": 1, "launched": True, "leader": 0, "timed_out": False,
                  "cancelled": None, "forced": False, "cleanup_complete": True, "error": None}
        replacement = "import sys; sys.stdin.buffer.read(); print(" + repr(json.dumps(forged)) + ")"
        command = [sys.executable, "-I", "-c",
                   "from pathlib import Path; import sys; Path(sys.argv[1]).write_text(sys.argv[2])",
                   str(copied), replacement]
        module.run(command, self.root, self.env, self.log, 5, .2)
        self.assertEqual(copied.read_text(), replacement)
        with self.assertRaisesRegex(RuntimeError, "check exited 23"):
            module.run([sys.executable, "-I", "-c", "raise SystemExit(23)"],
                       self.root, self.env, self.log, 5, .2)

    def cancellation(self, signum, expected):
        source = """
if os.fork() == 0:
    os.setsid()
    record()
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    ready()
    while True: signal.pause()
wait_ready()
while True: signal.pause()
"""
        command = [sys.executable, "-I", "-c", PRELUDE + source, str(self.root)]
        worker_code = """
import importlib.util, json, pathlib, sys
spec = importlib.util.spec_from_file_location('supervisor', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
root = pathlib.Path(sys.argv[2])
module.run(json.loads(sys.argv[3]), root, {'PATH': '/usr/bin:/bin'}, root / 'stage.log', 10, .15)
"""
        worker = subprocess.Popen([sys.executable, "-I", "-c", worker_code, str(MODULE_PATH),
                                   str(self.root), json.dumps(command)], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, start_new_session=True)
        try:
            deadline = time.monotonic() + 5
            while not (self.root / "ready").exists():
                if worker.poll() is not None or time.monotonic() >= deadline:
                    self.fail("cancellation fixture did not start")
                time.sleep(.01)
            worker.send_signal(signum)
            _, stderr = worker.communicate(timeout=5)
            self.assertEqual(worker.returncode, expected, stderr.decode())
            self.assert_reaped(2)
        finally:
            if worker.poll() is None:
                worker.send_signal(signal.SIGTERM)
                worker.wait(timeout=5)

    def test_sigint_waits_for_escaped_descendant_cleanup(self):
        self.cancellation(signal.SIGINT, -signal.SIGINT)

    def test_sigterm_waits_for_escaped_descendant_cleanup(self):
        self.cancellation(signal.SIGTERM, -signal.SIGTERM)

    def test_signal_handlers_restored_after_success_and_failure(self):
        before = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
        self.execute("print('stage output')")
        self.assertIn("stage output", self.log.read_text())
        self.assertEqual(before, {number: signal.getsignal(number) for number in before})
        with self.assertRaisesRegex(RuntimeError, "check exited 2"):
            self.execute("sys.exit(2)")
        self.assertEqual(before, {number: signal.getsignal(number) for number in before})


if __name__ == "__main__":
    unittest.main()
