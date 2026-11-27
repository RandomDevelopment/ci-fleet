"""Exercise the worker observer over HTTP against protected temporary exports."""

from contextlib import closing
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import socket
import tempfile
from threading import Thread
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlencode

import endpoint_jobs as jobs
import endpoint_snapshot as snapshot
import endpoint_snapshot_store as store
import endpoint_ui as ui
from endpoint_views import time_text
from test_endpoint_jobs import GEN, GEN2, HEADERS, SCALE, active, frame, state, terminal
from test_endpoint_snapshot import BEARER, CONTROLLER, NOW, documents


class WorkerObserverTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name) / "observer"
        self.gid = os.getegid()
        self.exports = self.base / "exports"
        self.base.mkdir(mode=0o750)
        self.exports.mkdir(mode=0o750)
        owner = patch.object(store, "OWNER_UID", os.geteuid())
        owner.start()
        self.addCleanup(owner.stop)
        self.now = NOW
        clock = patch.object(ui, "time", SimpleNamespace(time=lambda: self.now, monotonic=ui.time.monotonic))
        clock.start()
        self.addCleanup(clock.stop)
        self.reader = snapshot.SnapshotReader(self.base, observer_gid=self.gid, controller=CONTROLLER,
                                              backend_bearer=BEARER)
        with closing(socket.socket()) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            self.server = ui.ObserverServer(listener.dup(), ui.WorkerObserver(self.reader))
        worker = Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01})
        worker.start()
        self.addCleanup(worker.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, path="/", *, method="GET", headers=HEADERS):
        with closing(HTTPConnection(*self.server.server_address, timeout=5)) as connection:
            connection.request(method, path, headers=headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()

    def json(self, path, **kwargs):
        status, _, body = self.request(path, **kwargs)
        return status, json.loads(body)

    def write(self, name, value):
        staged = self.exports / f"{name}.tmp"
        staged.write_bytes(store.encode_json(value))
        staged.chmod(0o640)
        staged.replace(self.exports / name)

    def endpoint(self, docs=None, *, errors=None, previous=None, jobs_summary=None):
        value = snapshot.build_snapshot(CONTROLLER, docs or documents(), errors or {}, now=NOW, previous=previous)
        if jobs_summary:
            value["sources"]["jobs"] = snapshot.evidence(jobs_summary, observed_at=NOW)
        self.write("endpoint.json", value)
        return value

    def history(self, rows, *, generation=GEN):
        metadata = {**state(len(rows), generation), "record_count": len(rows)}
        value = jobs.projection(metadata, b"".join(map(frame, rows)), NOW)
        self.write("jobs.json", value)
        identity, _ = store.read_jobs(self.base, self.gid)
        return {"scale_set": SCALE, "pending": False, "source_available": True, "last_poll_at": NOW - 1,
                "generation": generation, "export_fingerprint": jobs.fingerprint(identity), "gaps": value["gaps"]}

    def test_authentication_precedes_access_and_routes_are_read_only(self):
        with patch.object(self.reader, "read", side_effect=AssertionError):
            for path in ("/", "/api/v1/jobs", "/worker.css", "/worker.js", "/missing"):
                status, headers, body = self.request(path, headers={"X-Forwarded-User": "operator"})
                self.assertEqual((status, json.loads(body)), (401, {"error": "authentication_failed"}))
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertNotIn("Access-Control-Allow-Origin", headers)
        status, headers, body = self.request("/worker.js", method="HEAD")
        self.assertEqual((status, body), (200, b""))
        self.assertGreater(int(headers["Content-Length"]), 0)
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        status, headers, body = self.request("/jobs", method="POST")
        self.assertEqual((status, headers["Allow"]), (405, "GET, HEAD"))
        self.assertEqual(self.json("/api/v1/diagnostics"), (404, {"error": "not_found"}))

        for _ in range(ui.MAX_HANDLERS):
            self.assertTrue(self.server.handlers.acquire(blocking=False))
            self.addCleanup(self.server.handlers.release)
        self.assertEqual(self.request(headers={})[0], 401)
        self.assertEqual(self.json("/api/v1/endpoint"), (503, {"error": "observer_busy"}))

    def test_request_and_response_bounds(self):
        with socket.create_connection(self.server.server_address, timeout=5) as connection:
            connection.sendall(b"GET / HTTP/1.1\r\nHost: observer\r\nX-Long: " + b"x" * ui.MAX_HEADERS)
            response = connection.makefile("rb")
            with response:
                self.assertEqual(response.readline(), b"HTTP/1.1 431 Request Header Fields Too Large\r\n")
        self.assertEqual(self.request("/api/v1/jobs?result=success&result=failure")[0], 400)

        partial = Mock()
        partial.recv.return_value = b"GET /"
        with patch.object(ui.time, "monotonic", side_effect=[0, ui.HEADER_SECONDS]):
            with self.assertRaises(TimeoutError):
                ui.receive_headers(partial, ui.HEADER_SECONDS)
        partial.recv.assert_called_once()

        with patch.object(ui, "MAX_RESPONSE", 1):
            self.assertEqual(self.json("/worker.css"), (503, {"error": "response_too_large"}))

    def test_worker_views_use_source_times_and_keep_readiness_states_distinct(self):
        docs = documents()
        docs["rendered"]["CI_FLEET_POOL"] = "validation"
        docs["controller"].update(current=0, busy=0)
        sample = {"timestamp": NOW - 130, "pool": "validation",
                  "host": {"memory_used_bytes": 2**20, "swap_used_bytes": 0, "disk_used_bytes": {}, "inode_used": {},
                           "cpu_total_ticks": 20, "cpu_idle_ticks": 10},
                  "runners": [{"cpu_percent": 75, "memory_used_bytes": 2**20, "memory_limit_bytes": 2**21}]}
        docs["capacity"] = {"history": store.encode_json(sample)}
        previous = self.endpoint(docs)
        status, _, body = self.request()
        self.assertEqual(status, 200)
        for phrase in ("Readiness unknown", "Busy runners", "Configured maximum", time_text(NOW - 10)):
            self.assertIn(phrase.encode(), body)
        body = self.request("/health")[2]
        for phrase in ("Last sample while active", time_text(NOW - 130), time_text(NOW - 20), "75.0%"):
            self.assertIn(phrase.encode(), body)
        self.assertIn(b"Owner confirmation required", self.request("/enrollment")[2])
        self.assertIn(b"3" * 40, self.request("/configuration")[2])
        self.now += 150
        self.assertIn(b"Stale evidence", self.request()[2])

        self.now = NOW
        docs.pop("controller")
        docs["health"].update(status="unhealthy")
        next(check for check in docs["health"]["checks"] if check["id"] == "docker")["status"] = "critical"
        self.endpoint(docs, errors={"controller": "source_unavailable"}, previous=previous)
        body = self.request()[2]
        self.assertIn(b"Controller evidence unavailable", body)
        self.assertIn(b"Last successful observation", body)
        self.assertIn(b"Docker daemon", body)
        self.assertIn(b"Critical", body)

        docs["installation"]["controller_state"] = "drained"
        docs["rendered"].update(CI_FLEET_CONTROLLER_STATE="drained", CI_FLEET_MAX_RUNNERS="0")
        docs["health"]["desired_state"] = "drained"
        self.endpoint(docs)
        self.assertIn(b"Intentionally drained", self.request()[2])

        (self.exports / "endpoint.json").unlink()
        self.assertEqual(self.request("/health")[0], 200)
        self.assertIn(b"Worker snapshot unavailable", self.request("/health")[2])
        self.assertEqual(self.json("/api/v1/capacity"), (503, {"error": "observation_unavailable"}))

    def test_job_rows_render_safely_and_keep_historical_results_during_outages(self):
        name = '<b>Build & verify</b>'
        running = {**active(), "display_name": name}
        ended = {**terminal(), "owner": "example", "repository": "build", "job_id": "12345"}
        summary = self.history([running, ended])
        self.endpoint(jobs_summary=summary)

        body = self.request("/jobs")[2]
        self.assertNotIn(name.encode(), body)
        self.assertIn(b"&lt;b&gt;Build &amp; verify&lt;/b&gt;", body)
        self.assertIn(b'https://github.com/example/build/actions/runs/9007199254740993', body)
        self.assertNotIn(b"/jobs/12345", body)
        self.assertIn(b"Since locally observed start", body)

        self.endpoint(jobs_summary={**summary, "pending": True, "gaps": {**summary["gaps"], "event_loss": 3}})
        status, page = self.json("/api/v1/jobs")
        self.assertEqual(status, 200)
        self.assertEqual(page["records"][0]["current_activity"], "unknown")
        self.assertEqual(page["records"][1]["result"], "failure")
        pending = self.request("/jobs")[2]
        self.assertIn(b"Job or coverage changes pending", pending)
        with self.subTest("publisher gaps during pending publication"):
            self.assertIn(b"Publisher coverage gaps", pending)
            self.assertIn(b"Event loss (3)", pending)

        (self.exports / "endpoint.json").unlink()
        self.assertIn(b"Endpoint snapshot unavailable", self.request("/jobs")[2])
        self.assertEqual(self.json("/api/v1/jobs?result=failure")[1]["records"][0]["result"], "failure")
        self.now = NOW - 600
        with self.subTest("clock rollback during snapshot outage"):
            rolled_back = self.request("/jobs")[2]
            self.assertIn(b"Clock uncertain", rolled_back)
            self.assertIn(b"Failure", rolled_back)
        self.now = NOW

        _, history = store.read_jobs(self.base, self.gid)
        history["records"][0]["run_url"] = "javascript:invalid"
        self.write("jobs.json", history)
        self.assertNotIn(b"javascript:", self.request("/jobs")[2])
        self.assertEqual(self.json("/api/v1/jobs"), (503, {"error": "observation_unavailable"}))

    def test_pagination_is_bound_to_the_export_and_filters(self):
        rows = [{**terminal(f"job-{index:03}"), "owner": "example", "repository": "build"}
                for index in range(ui.PAGE_SIZE + 1)]
        self.history(rows)
        path = "/api/v1/jobs?"
        status, first = self.json(path + "repository=example%2Fbuild")
        self.assertEqual(status, 200)
        self.assertEqual(len(first["records"]), ui.PAGE_SIZE)
        cursor = first["next_cursor"]
        query = urlencode({"repository": "example/build", "cursor": cursor})
        status, more = self.json(path + query)
        self.assertEqual((status, len(more["records"]), more["next_cursor"]), (200, 1, None))
        self.assertNotIn(more["records"][0], first["records"])
        self.assertEqual(self.json(path + urlencode({"cursor": cursor}))[0], 409)

        self.history(rows, generation=GEN2)
        status, _, body = self.request("/jobs?" + query)
        self.assertEqual(status, 409)
        self.assertIn(b"Job page expired", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
