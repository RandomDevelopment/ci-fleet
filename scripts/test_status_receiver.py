#!/usr/bin/env python3
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


status_auth = load("status_auth")
status_receiver = load("status_receiver")


def valid_report(controller: str = "example-ci-01", generated_at: int = 1_000) -> dict:
    return {
        "schema_version": 1,
        "controller": {
            "id": controller,
            "software_version": "1" * 40,
            "boot_time": 900,
            "ssh": "disabled",
        },
        "configuration": {"desired_commit": "2" * 40, "applied_commit": "2" * 40},
        "reconciliation": {"state": "converged", "last_success_at": 990},
        "drift": {"state": "ok"},
        "process": {"state": "running", "restart_count": 0},
        "timers": {name: "ok" for name in ("reconciliation", "drift", "health", "cleanup")},
        "runners": {"current": 0, "busy": 0, "maximum": 6},
        "metrics": {
            "cpu": {"logical": 8, "used_percent": 25.0},
            "memory": {"total_bytes": 1024, "available_bytes": 768},
            "swap": {"total_bytes": 512, "used_bytes": 0},
            "disk": {name: {"total_bytes": 4096, "used_bytes": 1024} for name in ("root", "docker")},
            "inodes": {name: {"total": 1000, "used": 100} for name in ("root", "docker")},
            "load": {"one": 0.1, "five": 0.2, "fifteen": 0.3},
        },
        "docker": {"healthy": True, "oom": False},
        "error": None,
        "generated_at": generated_at,
    }


class StatusReceiverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.key = b"controller-key"
        self.receiver = status_receiver.StatusReceiver(
            Path(self.temporary.name) / "status.db",
            {"example-ci-01": self.key, "other-ci-01": b"other-key"},
            read_token="reader-token",
            history_limit=3,
            retention_seconds=3_600,
            min_interval_seconds=0,
        )
        self.receiver._clock = lambda: 1_000

    def signed(self, report: dict, *, timestamp: int = 1_000, nonce: str = "a" * 32,
               controller: str = "example-ci-01", key: bytes | None = None) -> tuple[bytes, dict[str, str]]:
        body = json.dumps(report, separators=(",", ":"), sort_keys=True).encode()
        return body, status_auth.sign_headers(controller, body, key or self.key, timestamp=timestamp, nonce=nonce)

    def submit(self, report: dict, **kwargs) -> None:
        body, headers = self.signed(report, **kwargs)
        self.receiver.submit(body, headers, now=kwargs.get("timestamp", 1_000))

    def assert_status_error(self, status: int, code: str, call) -> None:
        with self.assertRaises(status_receiver.StatusError) as caught:
            call()
        self.assertEqual((caught.exception.status, caught.exception.code), (status, code))

    def test_authenticated_report_is_stored_and_read_as_latest(self) -> None:
        report = valid_report()
        self.submit(report)
        self.assertEqual(self.receiver.latest("example-ci-01", "reader-token"), report)

    def test_concurrent_report_writes_are_serialized(self) -> None:
        body, headers = self.signed(
            valid_report("other-ci-01"), controller="other-ci-01", key=b"other-key"
        )
        finished = threading.Event()
        errors = []

        def submit() -> None:
            try:
                self.receiver.submit(body, headers, now=1_000)
            except Exception as error:
                errors.append(error)
            finally:
                finished.set()

        self.receiver._write_lock.acquire()
        thread = threading.Thread(target=submit)
        thread.start()
        try:
            self.assertFalse(finished.wait(0.05))
        finally:
            self.receiver._write_lock.release()
        thread.join()
        self.assertEqual(errors, [])

    def test_read_token_cannot_reuse_controller_key(self) -> None:
        with self.assertRaisesRegex(ValueError, "read token"):
            status_receiver.StatusReceiver(
                Path(self.temporary.name) / "shared-secret.db",
                {"example-ci-01": b"shared-secret"},
                read_token="shared-secret",
            )

    def test_duplicate_controller_keys_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unique"):
            status_receiver.StatusReceiver(
                Path(self.temporary.name) / "duplicate.db",
                {"example-ci-01": self.key, "other-ci-01": self.key},
                read_token="reader-token",
            )

    def test_retention_bounds_must_be_positive(self) -> None:
        for values in ({"history_limit": 0}, {"retention_seconds": -1}):
            with self.assertRaisesRegex(ValueError, "positive"):
                status_receiver.StatusReceiver(
                    Path(self.temporary.name) / "invalid.db", {"example-ci-01": self.key},
                    read_token="reader-token", **values,
                )

    def test_authentication_rejects_tampering_and_unknown_controller(self) -> None:
        body, headers = self.signed(valid_report())
        self.assert_status_error(401, "authentication_failed", lambda: self.receiver.submit(body + b" ", headers, now=1_000))
        headers["X-CI-Fleet-Controller"] = "missing"
        self.assert_status_error(401, "authentication_failed", lambda: self.receiver.submit(body, headers, now=1_000))

    def test_controller_identity_isolation(self) -> None:
        report = valid_report("other-ci-01")
        body, headers = self.signed(report)
        self.assert_status_error(403, "controller_identity_mismatch", lambda: self.receiver.submit(body, headers, now=1_000))

    def test_replay_stale_authentication_and_stale_report_are_rejected(self) -> None:
        report = valid_report()
        body, headers = self.signed(report)
        self.receiver.submit(body, headers, now=1_000)
        self.assert_status_error(409, "replayed_report", lambda: self.receiver.submit(body, headers, now=1_000))
        old_body, old_headers = self.signed(valid_report(generated_at=1_001), timestamp=1_000, nonce="b" * 32)
        self.assert_status_error(401, "authentication_stale", lambda: self.receiver.submit(old_body, old_headers, now=1_301))
        delayed = valid_report(generated_at=600)
        delayed["controller"]["boot_time"] = 500
        delayed["reconciliation"]["last_success_at"] = 590
        delayed_body, delayed_headers = self.signed(delayed, timestamp=1_000, nonce="d" * 32)
        self.assert_status_error(409, "report_time_stale", lambda: self.receiver.submit(delayed_body, delayed_headers, now=1_000))
        stale_body, stale_headers = self.signed(valid_report(), timestamp=1_001, nonce="c" * 32)
        self.assert_status_error(409, "stale_report", lambda: self.receiver.submit(stale_body, stale_headers, now=1_001))

    def test_payload_and_submission_frequency_are_bounded(self) -> None:
        small = status_receiver.StatusReceiver(
            Path(self.temporary.name) / "small.db", {"example-ci-01": self.key},
            read_token="reader-token", max_payload_bytes=16, min_interval_seconds=0,
        )
        body, headers = self.signed(valid_report())
        self.assert_status_error(413, "payload_too_large", lambda: small.submit(body, headers, now=1_000))

        limited = status_receiver.StatusReceiver(
            Path(self.temporary.name) / "limited.db", {"example-ci-01": self.key},
            read_token="reader-token", min_interval_seconds=30,
        )
        limited.submit(body, headers, now=1_000)
        second_body, second_headers = self.signed(valid_report(generated_at=1_001), timestamp=1_001, nonce="b" * 32)
        self.assert_status_error(429, "submission_too_frequent", lambda: limited.submit(second_body, second_headers, now=1_001))

    def test_schema_compatibility_and_malformed_metrics(self) -> None:
        future = valid_report()
        future["schema_version"] = 2
        body, headers = self.signed(future)
        self.assert_status_error(400, "unsupported_schema", lambda: self.receiver.submit(body, headers, now=1_000))

        boolean = valid_report()
        boolean["schema_version"] = True
        body, headers = self.signed(boolean, nonce="f" * 32)
        self.assert_status_error(400, "unsupported_schema", lambda: self.receiver.submit(body, headers, now=1_000))

        malformed = valid_report()
        malformed["metrics"]["memory"]["available_bytes"] = -1
        body, headers = self.signed(malformed, nonce="b" * 32)
        self.assert_status_error(400, "invalid_report", lambda: self.receiver.submit(body, headers, now=1_000))

        extra = valid_report()
        extra["secret"] = "must not be accepted"
        body, headers = self.signed(extra, nonce="c" * 32)
        self.assert_status_error(400, "invalid_report", lambda: self.receiver.submit(body, headers, now=1_000))

    def test_history_and_retention_are_bounded(self) -> None:
        for offset, nonce in enumerate(("a", "b", "c", "d")):
            generated = 1_000 + offset
            self.submit(valid_report(generated_at=generated), timestamp=generated, nonce=nonce * 32)
        self.assertEqual([item["generated_at"] for item in self.receiver.history("example-ci-01", "reader-token")], [1_003, 1_002, 1_001])

        self.submit(valid_report(generated_at=5_000), timestamp=5_000, nonce="e" * 32)
        self.assertEqual([item["generated_at"] for item in self.receiver.history("example-ci-01", "reader-token")], [5_000])
        latest, history = self.receiver.latest_and_history("example-ci-01", "reader-token")
        self.assertEqual(latest, history[0])

    def test_time_retention_is_enforced_without_traffic(self) -> None:
        receiver = status_receiver.StatusReceiver(
            Path(self.temporary.name) / "expiry.db", {"example-ci-01": self.key},
            read_token="reader-token", retention_seconds=10, min_interval_seconds=0,
        )
        body, headers = self.signed(valid_report())
        receiver.submit(body, headers, now=1_000)
        receiver._clock = lambda: 1_011
        stop = threading.Event()
        thread = threading.Thread(target=status_receiver._expiration_loop, args=(receiver, stop, 0.01))
        thread.start()
        try:
            deadline = time.time() + 1
            while time.time() < deadline:
                with sqlite3.connect(receiver.database) as connection:
                    if connection.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 0:
                        break
                time.sleep(0.01)
            else:
                self.fail("expired report remained without request traffic")
        finally:
            stop.set()
            thread.join()

    def test_receiver_restart_reloads_rotated_key(self) -> None:
        directory = Path(self.temporary.name)
        key_file, token_file, config_file = directory / "key", directory / "token", directory / "auth.json"
        raw_key = b"\n" + b"a" * 30 + b" "
        key_file.write_bytes(raw_key)
        token_file.write_bytes(b"r" * 32)
        config_file.write_text(json.dumps({"controllers": {"example-ci-01": "key"}, "read_token_file": "token"}))
        for path in (key_file, token_file, config_file):
            path.chmod(0o600)
        first, _ = status_receiver.load_auth_config(config_file)
        key_file.write_bytes(b"b" * 32)
        second, _ = status_receiver.load_auth_config(config_file)
        self.assertEqual((first["example-ci-01"], second["example-ci-01"]), (raw_key, b"b" * 32))

    def test_http_post_and_read_only_api(self) -> None:
        server = status_receiver.create_server("127.0.0.1", 0, self.receiver)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        now = int(time.time())
        report = valid_report(generated_at=now)
        report["controller"]["boot_time"] = now - 100
        report["reconciliation"]["last_success_at"] = now - 10
        body, headers = self.signed(report, timestamp=now)
        request = urllib.request.Request(base + "/v1/status", data=body, headers=headers, method="POST")
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.status, 202)
        request = urllib.request.Request(base + "/v1/controllers", headers={"Authorization": "Bearer reader-token"})
        with urllib.request.urlopen(request) as response:
            payload = json.load(response)
        self.assertEqual(payload, {"schema_version": 1, "controllers": [report]})
        request = urllib.request.Request(base + "/v1/controllers/example-ci-01", headers={"Authorization": "Bearer reader-token"})
        with urllib.request.urlopen(request) as response:
            payload = json.load(response)
        self.assertEqual(payload["latest"], report)
        self.assertEqual(payload["history"], [report])

    def test_read_api_authentication_and_controller_listing(self) -> None:
        self.submit(valid_report())
        self.assert_status_error(401, "read_authentication_failed", lambda: self.receiver.latest("example-ci-01", "wrong"))
        self.assert_status_error(401, "read_authentication_failed", lambda: self.receiver.latest("example-ci-01", "tök"))
        self.assertEqual(self.receiver.list_latest("reader-token"), [valid_report()])

    def test_http_server_bounds_slow_clients(self) -> None:
        server = status_receiver.create_server("127.0.0.1", 0, self.receiver)
        self.addCleanup(server.server_close)
        self.assertEqual((server.max_requests, server.request_timeout), (32, 15))
        for _ in range(server.max_requests):
            self.assertTrue(server._slots.acquire(blocking=False))
        self.assertFalse(server._slots.acquire(blocking=False))
        for _ in range(server.max_requests):
            server._slots.release()

        expired = threading.Event()
        class SlowRequest:
            def shutdown(self, how: int) -> None:
                self.how = how
                expired.set()
        request = SlowRequest()
        timer = threading.Timer(0.01, server._expire_request, (request,))
        timer.start()
        self.assertTrue(expired.wait(1))
        self.assertEqual(request.how, status_receiver.socket.SHUT_RDWR)

        if status_receiver.socket.has_ipv6:
            ipv6 = status_receiver.create_server("::1", 0, self.receiver)
            self.addCleanup(ipv6.server_close)
            self.assertEqual(ipv6.address_family, status_receiver.socket.AF_INET6)


if __name__ == "__main__":
    unittest.main()
