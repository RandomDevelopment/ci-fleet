#!/usr/bin/env python3
"""Acceptance tests for system-manager stage ownership on a disposable worker."""
from __future__ import annotations

import importlib.util
import hashlib
import json
import os
from pathlib import Path
import resource
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

MODULE_PATH = Path(__file__).with_name("local_validation_executor.py")
SPEC = importlib.util.spec_from_file_location("local_validation_executor", MODULE_PATH)
executor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(executor)

PRELUDE = '''import os, signal, sys, time
from pathlib import Path
root = Path(sys.argv[1])
def record():
    with (root / 'pids').open('ab', buffering=0) as output:
        output.write((str(os.getpid()) + '\\n').encode())
def ready():
    (root / 'ready').touch()
def wait_ready():
    deadline = time.monotonic() + 5
    while not (root / 'ready').exists():
        if time.monotonic() > deadline: raise RuntimeError('child not ready')
        time.sleep(.01)
record()
'''


class AdmissionTests(unittest.TestCase):
    def test_installed_revision_and_bytes_must_match_in_a_protected_directory(self):
        with tempfile.TemporaryDirectory(dir="/run") as temporary:
            install = Path(temporary)
            program = install / "executor.py"
            manifest = install / "revision.json"
            program.write_bytes(b"reviewed executor bytes")
            manifest.write_text(json.dumps({"revision": "a" * 40, "sha256": hashlib.sha256(program.read_bytes()).hexdigest()}))
            with patch.object(executor, "INSTALL", install), patch.object(executor, "__file__", str(program)):
                executor.require_revision("a" * 40)
                with self.assertRaisesRegex(RuntimeError, "exact reviewed"):
                    executor.require_revision("b" * 40)
                program.write_bytes(b"substituted executor")
                with self.assertRaisesRegex(RuntimeError, "exact reviewed"):
                    executor.require_revision("a" * 40)
                program.chmod(0o666)
                with self.assertRaisesRegex(RuntimeError, "unsafe installed"):
                    executor.require_revision("a" * 40)
                program.unlink()
                program.symlink_to(manifest)
                with self.assertRaisesRegex(RuntimeError, "unsafe installed"):
                    executor.require_revision("a" * 40)

    def test_no_system_manager_never_launches_candidate(self):
        with patch.object(executor, "require_worker", side_effect=RuntimeError("system manager required")), \
                patch.object(executor.subprocess, "Popen") as launch:
            with self.assertRaisesRegex(RuntimeError, "system manager"):
                executor.run(["true"], Path("/"), {}, Path("/unused"), 1)
            launch.assert_not_called()

    def test_invalid_requests_never_launch_candidates(self):
        with patch.object(executor, "require_worker") as admission:
            for command, environment, timeout, grace, user in (
                    ([], {}, 1, 1, "root"), (["true"], {}, 0, 1, "root"),
                    (["true"], {}, 1, -1, "root"), (["true"], {}, 1, 1, "nobody"),
                    (["true"], {"bad-key": "value"}, 1, 1, "root")):
                with self.subTest(command=command, timeout=timeout, grace=grace, user=user), self.assertRaises(ValueError):
                    executor.run(command, Path("/"), environment, Path("/unused"), timeout, grace, user=user)
            admission.assert_not_called()

    def test_missing_failed_or_malformed_manager_receipts_cannot_pass(self):
        for outcome in (None, [], {"SERVICE_RESULT": "success"},
                        {"SERVICE_RESULT": "success", "EXIT_CODE": "exited", "EXIT_STATUS": "7"}):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as temporary:
                def launch(command, **_):
                    finish = Path(next(value for value in command if value.startswith("--property=ExecStopPost=")).split()[-1])
                    if outcome is not None:
                        finish.with_name("result.json").write_text(json.dumps(outcome))
                    return type("Process", (), {"returncode": 0, "poll": lambda _: 0})()
                with patch.object(executor, "require_worker"), patch.object(executor.subprocess, "Popen", side_effect=launch), \
                        patch.object(executor.subprocess, "run"), self.assertRaises(RuntimeError):
                    executor.run(["true"], Path(temporary), {}, Path(temporary) / "stage.log", 1)


class ExecutorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Missing prerequisites fail acceptance, rather than silently skip it.
        executor.require_worker()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="executor-test-", dir="/run")
        self.root = Path(self.temporary.name)
        self.unit_id = executor.uuid.uuid4().hex
        self.unit = "ci-validation-" + self.unit_id + ".service"
        self.identity = patch.object(executor.uuid, "uuid4", return_value=type("ID", (), {"hex": self.unit_id})())
        self.identity.start()
        self.addCleanup(self.identity.stop)
        self.log = self.root / "stage.log"
        self.env = {"PATH": os.defpath, "LANG": "C.UTF-8"}

    def tearDown(self):
        subprocess.run(["systemctl", "stop", self.unit], capture_output=True, timeout=10)
        subprocess.run(["systemctl", "reset-failed", self.unit], capture_output=True, timeout=10)
        for path in Path("/run").glob("ci-validation-" + self.unit_id + "-*"):
            executor.shutil.rmtree(path)
        self.temporary.cleanup()

    def execute(self, code, timeout=5, grace=.3, **kwargs):
        executor.run([sys.executable, "-I", "-c", PRELUDE + code, str(self.root)],
                     self.root, self.env, self.log, timeout, grace, **kwargs)

    def assert_empty(self):
        group = Path("/sys/fs/cgroup/system.slice") / self.unit
        if group.exists():
            self.assertIn("populated 0", (group / "cgroup.events").read_text().splitlines())
        deadline = time.monotonic() + 3
        path = self.root / "pids"
        pids = path.read_text().splitlines() if path.exists() else []
        while any(Path("/proc", pid).exists() for pid in pids) and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse([pid for pid in pids if Path("/proc", pid).exists()])

    def test_success_has_exact_output_and_no_inherited_environment(self):
        with patch.dict(os.environ, CI_EXECUTOR_PRIVATE="must-not-escape"):
            self.execute("assert 'CI_EXECUTOR_PRIVATE' not in os.environ; print('$HOME %n literal')")
        self.assertEqual(self.log.read_text(), "$HOME %n literal\n")
        self.assert_empty()

    def test_detached_child_is_cleaned_before_next_stage(self):
        self.execute('''
if os.fork() == 0:
    os.setsid(); record()
    def clean(*_):
        (root / 'cleaned').touch(); sys.exit(0)
    signal.signal(signal.SIGTERM, clean); ready(); time.sleep(60)
else:
    wait_ready()
''')
        self.assertTrue((self.root / "cleaned").exists())
        self.assert_empty()

    def test_double_fork_requiring_kill_fails(self):
        with self.assertRaisesRegex(RuntimeError, "forced termination"):
            self.execute('''
if os.fork() == 0:
    os.setsid()
    if os.fork() != 0: os._exit(0)
    record(); signal.signal(signal.SIGTERM, signal.SIG_IGN); ready(); time.sleep(60)
else:
    wait_ready()
''')
        self.assert_empty()

    def test_failure_and_timeout_cannot_pass(self):
        with self.assertRaises(RuntimeError):
            self.execute("raise SystemExit(7)")
        self.assert_empty()
        with self.assertRaises(subprocess.TimeoutExpired):
            self.execute("signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)", timeout=1)
        self.assert_empty()

    def test_descriptor_exhaustion_does_not_leave_children(self):
        limits = resource.getrlimit(resource.RLIMIT_NOFILE)
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (32, limits[1]))
            self.execute('''
for _ in range(36):
    if os.fork() == 0:
        record(); time.sleep(60); os._exit(0)
time.sleep(.15)
''')
        finally:
            resource.setrlimit(resource.RLIMIT_NOFILE, limits)
        self.assertGreaterEqual(len((self.root / "pids").read_text().splitlines()), 37)
        self.assert_empty()

    def driver(self, timeout):
        code = "import sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); import local_validation_executor as x; "
        code += "x.uuid.uuid4=lambda:type('ID',(),{'hex':sys.argv[3]})(); "
        code += "r=Path(sys.argv[2]); x.run([sys.executable,'-I','-c',sys.argv[4],str(r)],r,{'PATH':'/usr/bin:/bin'},r/'stage.log',int(sys.argv[5]),.3); (r/'accepted').touch()"
        stage = PRELUDE + "ready(); signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)"
        process = subprocess.Popen([sys.executable, "-I", "-c", code, str(MODULE_PATH.parent), str(self.root), self.unit_id, stage, str(timeout)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(process.communicate)
        deadline = time.monotonic() + 8
        while not (self.root / "ready").exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue((self.root / "ready").exists())
        return process

    def test_cancellation_cleans_up_and_never_accepts(self):
        for signum in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=signum):
                (self.root / "ready").unlink(missing_ok=True)
                process = self.driver(5)
                process.send_signal(signum)
                process.communicate(timeout=8)
                self.assertNotEqual(process.returncode, 0)
                self.assertFalse((self.root / "accepted").exists())
                self.assert_empty()

    def test_killed_launcher_leaves_manager_deadline_and_no_acceptance(self):
        process = self.driver(1)
        process.kill()
        process.communicate(timeout=5)
        time.sleep(2)
        self.assertFalse((self.root / "accepted").exists())
        self.assert_empty()

    def test_unprivileged_stage_cannot_write_trusted_inputs_or_control_managers(self):
        trusted = self.root / "trusted"
        trusted.write_text("preserved")
        scratch = self.root / "scratch"
        scratch.mkdir()
        self.root.chmod(0o711)
        executor.prepare_stage([scratch], [trusted])
        code = '''import os, subprocess, sys
from pathlib import Path
assert os.geteuid() != 0
try: Path(sys.argv[1]).write_text('changed')
except PermissionError: pass
else: raise AssertionError('trusted input writable')
for args in (['systemctl','--system','show-environment'],['systemctl','--user','show-environment']):
    assert subprocess.run(args,capture_output=True).returncode != 0
assert not os.access('/run/docker.sock', os.R_OK | os.W_OK)
Path('scratch-result').write_text('ok')
'''
        executor.run([sys.executable, "-I", "-c", code, str(trusted)], scratch, self.env, self.log, 5, user=executor.USER)
        self.assertEqual(trusted.read_text(), "preserved")
        self.assertEqual((scratch / "scratch-result").read_text(), "ok")
        self.assert_empty()

    def test_missing_receipt_fails_closed(self):
        with patch.object(executor, "FINISH", "pass\n"), self.assertRaisesRegex(RuntimeError, "receipt"):
            self.execute("print('leader succeeded')")
        self.assert_empty()

    def test_an_unrelated_concurrent_service_is_untouched(self):
        unrelated = "executor-unrelated-" + self.unit_id + ".service"
        subprocess.run(["systemd-run", "--quiet", "--unit=" + unrelated, "/bin/sleep", "30"], check=True)
        try:
            self.execute("print('done')")
            self.assertEqual(subprocess.run(["systemctl", "is-active", "--quiet", unrelated]).returncode, 0)
        finally:
            subprocess.run(["systemctl", "stop", unrelated], check=True)
        self.assert_empty()


if __name__ == "__main__":
    unittest.main()
