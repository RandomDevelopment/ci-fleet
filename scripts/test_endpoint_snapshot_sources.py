#!/usr/bin/env python3
"""Exercise protected sources and bounded readers using local fixtures only."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import endpoint_snapshot_sources as sources
import endpoint_snapshot_store as store


REAL_POPEN = subprocess.Popen
CONTROLLER_STATUS = {
    "controller": "example-ci", "software_version": "a" * 40,
    "current": 1, "busy": 1, "maximum": 2, "generated_at": 123,
}


class SnapshotSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        owner = patch.object(store, "OWNER_UID", os.geteuid())
        owner.start()
        self.addCleanup(owner.stop)
        self.children = []
        self.addCleanup(self.stop_children)
        self.write("installation", {"schema_version": 1, "controller": "example-ci"})
        self.write("rendered", b"CI_FLEET_INSTANCE=example-ci\n")
        self.write("health", {"schema_version": 1, "controller": "example-ci", "timestamp": 121})
        self.write("reconciliation", {"status": "failed", "checked_at": 122})

    def stop_children(self) -> None:
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=2)
            if child.stdout is not None:
                child.stdout.close()

    def write(self, name: str, value) -> Path:
        relative, mode = sources.SOURCE_PATHS[name]
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_bytes(value if isinstance(value, bytes) else json.dumps(value).encode())
        path.chmod(mode)
        return path

    def reader(self, program: str):
        def launch(command, **kwargs):
            self.assertEqual(command, [
                "docker", "--host", "unix:///var/run/docker.sock", "exec",
                "ci-fleet-controller-1", "cat", "/run/ci-fleet/status.json",
            ])
            self.assertEqual(kwargs["env"], {"PATH": "/usr/bin:/bin"})
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            self.assertEqual(kwargs["stdout"], subprocess.PIPE)
            self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
            self.assertTrue(kwargs["close_fds"])
            self.assertNotIn("shell", kwargs)
            child = REAL_POPEN([sys.executable, "-c", program], **kwargs)
            self.children.append(child)
            return child
        return patch.object(sources.subprocess, "Popen", side_effect=launch)

    def assert_reader_error(self, code: str, program: str, *, timeout: float = 2.0) -> None:
        started = time.monotonic()
        with self.reader(program):
            with self.assertRaises(sources.SourceError) as caught:
                sources._docker_status(timeout_seconds=timeout)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)
        self.assertLess(time.monotonic() - started, timeout + 0.5)
        self.assertIsNotNone(self.children[-1].poll())
        self.assertTrue(self.children[-1].stdout.closed)

    def test_collects_real_protected_files_and_actual_reader_protocol(self) -> None:
        program = f"print({json.dumps(CONTROLLER_STATUS)!r})"
        with self.reader(program) as launched:
            documents, errors = sources.collect_documents("example-ci", root=self.root)
        self.assertEqual(errors, {})
        self.assertEqual(set(documents), set(sources.SOURCE_PATHS) | {"controller"})
        self.assertEqual(documents["installation"]["controller"], "example-ci")
        self.assertEqual(documents["rendered"]["CI_FLEET_INSTANCE"], "example-ci")
        self.assertEqual(documents["health"]["timestamp"], 121)
        self.assertEqual(documents["reconciliation"]["checked_at"], 122)
        self.assertEqual(documents["controller"], CONTROLLER_STATUS)
        launched.assert_called_once()

    def test_source_failures_are_independent_and_redacted(self) -> None:
        self.write("rendered", b"A=first\nA=second\n")
        self.write("reconciliation", {"status": "failed"}).chmod(0o644)
        with self.reader("print('{}')"):
            documents, errors = sources.collect_documents("example-ci", root=self.root)
        self.assertEqual(errors, {"rendered": "source_invalid", "reconciliation": "source_unsafe"})
        self.assertIsNone(documents["rendered"])
        self.assertIsNone(documents["reconciliation"])
        self.assertEqual(documents["health"]["timestamp"], 121)
        self.assertEqual(documents["controller"], {})

    def test_rendered_values_are_plain_text_and_never_execute(self) -> None:
        marker = self.root / "must-not-exist"
        text = (f"# An ordinary comment\n\nCI_FLEET_INSTANCE=example-ci\n"
                f"COMMAND=$(touch {marker})\nBACKTICKS=`touch {marker}`\n")
        self.write("rendered", text.encode())
        with self.reader("print('{}')"):
            documents, errors = sources.collect_documents("example-ci", root=self.root)
        self.assertEqual(errors, {})
        self.assertEqual(documents["rendered"]["COMMAND"], f"$(touch {marker})")
        self.assertEqual(documents["rendered"]["BACKTICKS"], f"`touch {marker}`")
        self.assertFalse(marker.exists())

    def test_rendered_rejects_ambiguous_or_invalid_text(self) -> None:
        for value in (b"lower=value\n", b"A=1\nA=2\n", b"A=1\r\n", b"A=1\0", b"A=\xff", b"export A=1\n"):
            with self.subTest(value=value):
                self.write("rendered", value)
                with self.reader("print('{}')"):
                    documents, errors = sources.collect_documents("example-ci", root=self.root)
                self.assertIsNone(documents["rendered"])
                self.assertEqual(errors, {"rendered": "source_invalid"})
                self.assertEqual(documents["health"]["timestamp"], 121)

    def test_missing_installation_prevents_docker_read(self) -> None:
        (self.root / sources.SOURCE_PATHS["installation"][0]).unlink()
        with patch.object(sources.subprocess, "Popen") as launched:
            documents, errors = sources.collect_documents("example-ci", root=self.root)
        launched.assert_not_called()
        self.assertEqual(errors, {"installation": "not_found", "controller": "unsupported"})
        self.assertIsNone(documents["controller"])
        self.assertEqual(documents["health"]["timestamp"], 121)

    def test_untrusted_installation_prevents_docker_read(self) -> None:
        for value in (
            {"schema_version": 2, "controller": "example-ci"},
            {"schema_version": True, "controller": "example-ci"},
            {"schema_version": 1, "controller": "another-ci"},
            b'{"schema_version":1,"schema_version":2,"controller":"example-ci"}',
        ):
            with self.subTest(value=value):
                self.write("installation", value)
                with patch.object(sources.subprocess, "Popen") as launched:
                    documents, errors = sources.collect_documents("example-ci", root=self.root)
                launched.assert_not_called()
                self.assertEqual(errors["controller"], "unsupported")
                self.assertIsNone(documents["controller"])
        self.write("installation", {"schema_version": 1, "controller": "example-ci"}).chmod(0o644)
        with patch.object(sources.subprocess, "Popen") as launched:
            _, errors = sources.collect_documents("example-ci", root=self.root)
        launched.assert_not_called()
        self.assertEqual(errors, {"installation": "source_unsafe", "controller": "unsupported"})

    def test_invalid_requested_identity_prevents_docker_read(self) -> None:
        for controller in ("EXAMPLE", "../example-ci", "example_ci", "example-ci;true", "a" * 64):
            with self.subTest(controller=controller):
                self.write("installation", {"schema_version": 1, "controller": controller})
                with patch.object(sources.subprocess, "Popen") as launched:
                    _, errors = sources.collect_documents(controller, root=self.root)
                launched.assert_not_called()
                self.assertEqual(errors["controller"], "unsupported")

    def test_reader_ignores_inherited_remote_endpoint_and_proxy_settings(self) -> None:
        environment = {
            "DOCKER_HOST": "tcp://must-not-contact.invalid:2375", "DOCKER_CONTEXT": "remote",
            "HTTP_PROXY": "http://must-not-contact.invalid", "DOCKER_CONFIG": "/must-not-read",
        }
        with patch.dict(os.environ, environment), self.reader("print('{}')"):
            self.assertEqual(sources._docker_status(timeout_seconds=2), {})

    def test_exact_output_limit_is_accepted_and_excess_is_rejected(self) -> None:
        count = sources.MAX_SOURCE_BYTES - len('{"x":""}')
        program = f"import sys; sys.stdout.write('{{\"x\":\"' + 'a' * {count} + '\"}}')"
        with self.reader(program):
            result = sources._docker_status(timeout_seconds=2)
        self.assertEqual(len(result["x"]), count)
        self.assert_reader_error("source_too_large", f"print('a' * {sources.MAX_SOURCE_BYTES})")

    def test_invalid_stdout_json_is_rejected_without_details(self) -> None:
        for output in ('{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}', '{"x":1e999}', '[]', 'private error text'):
            with self.subTest(output=output):
                self.assert_reader_error("source_invalid", f"print({output!r})")

    def test_reader_nonzero_exit_and_spawn_failure_are_controlled(self) -> None:
        self.assert_reader_error("source_unavailable", "import sys; sys.stderr.write('private error text'); sys.exit(2)")
        with patch.object(sources.subprocess, "Popen", side_effect=OSError("private process error")):
            with self.assertRaises(sources.SourceError) as caught:
                sources._docker_status()
        self.assertEqual(str(caught.exception), "source_unavailable")

    def test_stalled_stdout_times_out_and_reaps_child(self) -> None:
        self.assert_reader_error("source_timeout", "import time; time.sleep(10)", timeout=1)

    def test_closed_stdout_does_not_allow_unbounded_process_wait(self) -> None:
        marker = self.root / "stdout-closed"
        program = (f"import os,time; from pathlib import Path; Path({str(marker)!r}).touch(); "
                   "os.close(1); time.sleep(10)")
        self.assert_reader_error("source_timeout", program, timeout=1)
        self.assertTrue(marker.exists())


if __name__ == "__main__":
    unittest.main()
