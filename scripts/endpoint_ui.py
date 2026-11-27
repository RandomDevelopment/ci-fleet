#!/usr/bin/env python3
"""Read-only worker HTTP application for an already prepared loopback listener."""

from __future__ import annotations

from hashlib import sha256
from http import HTTPStatus
import hmac
from pathlib import Path
import re
import secrets
import socket
from socketserver import ThreadingTCPServer
from threading import BoundedSemaphore, Lock
import time
from urllib.parse import parse_qsl

from endpoint_jobs import RESULTS, record_key
from endpoint_snapshot import SnapshotError, SnapshotReader
from endpoint_snapshot_store import StoreError, encode_json
import endpoint_views as views

MAX_HEADERS = 16 * 1024
MAX_RESPONSE = 1024 * 1024
HEADER_SECONDS = 5
IO_SECONDS = 30
MAX_CONNECTIONS = 16
MAX_HANDLERS = 4
PAGE_SIZE = 50
TOKEN = re.compile(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
CURSOR = re.compile(r"([0-9a-f]{64})\.([0-9a-f]{64})")
JSON = "application/json"
HTML = "text/html; charset=utf-8"
ASSET_DIR = Path(__file__).with_name("endpoint_ui_assets")
ASSETS = {"/worker.css": ("text/css; charset=utf-8", "worker.css"),
          "/worker.js": ("text/javascript; charset=utf-8", "worker.js")}
API = {"/api/v1/endpoint", "/api/v1/jobs", "/api/v1/capacity"}
JOB_STATES = ("running", "completed", "unknown")
NO_STORE = (
    b"Cache-Control: no-store\r\n"
    b"Connection: close\r\n"
    b"Content-Security-Policy: default-src 'none'; style-src 'self'; script-src 'self'; "
    b"connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'\r\n"
    b"X-Content-Type-Options: nosniff\r\n"
    b"Referrer-Policy: no-referrer\r\n"
)


class RequestError(ValueError):
    def __init__(self, status, code):
        super().__init__(code)
        self.status, self.code = status, code


def receive_headers(connection, deadline):
    raw = bytearray()
    while not raw.endswith(b"\r\n\r\n"):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        if len(raw) == MAX_HEADERS:
            raise RequestError(431, "headers_too_large")
        connection.settimeout(remaining)
        chunk = connection.recv(min(4096, MAX_HEADERS - len(raw)))
        if not chunk:
            raise EOFError
        raw.extend(chunk)
        if b"\r\n\r\n" in raw and not raw.endswith(b"\r\n\r\n"):
            raise RequestError(400, "invalid_request")
    return parse_headers(raw[:-4])


def parse_headers(raw):
    request, *lines = raw.split(b"\r\n")
    match = re.fullmatch(rb"([^ ]+) (/[^ ]*) HTTP/1\.[01]", request)
    if not match or not TOKEN.fullmatch(match[1]) or not re.fullmatch(rb"[!-~]+", match[2]):
        raise RequestError(400, "invalid_request")
    headers = {}
    for line in lines:
        name, colon, value = line.partition(b":")
        if not colon or not TOKEN.fullmatch(name) or not re.fullmatch(rb"[\t -~]*", value):
            raise RequestError(400, "invalid_request")
        name = name.decode("ascii").lower()
        if name in headers:
            raise RequestError(400, "invalid_request")
        headers[name] = value.decode("ascii").strip(" \t")
    if (request.endswith(b"HTTP/1.1") and not headers.get("host")
            or {"transfer-encoding", "expect"} & headers.keys()
            or headers.get("content-length", "0") != "0"):
        raise RequestError(400, "invalid_request")
    return match[1].decode("ascii"), match[2].decode("ascii"), headers


def job_query(query):
    if len(query) > 1024 or re.search(r"%(?![0-9a-fA-F]{2})", query):
        raise RequestError(400, "invalid_filters")
    try:
        pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True, errors="strict", max_num_fields=4)
    except ValueError:
        raise RequestError(400, "invalid_filters") from None
    params = dict(pairs)
    repo, state, result, cursor = (params.get(field, "") for field in ("repository", "state", "result", "cursor"))
    if (len(params) != len(pairs) or set(params) - {"repository", "state", "result", "cursor"}
            or not re.fullmatch(r"[A-Za-z0-9_./-]{0,140}", repo)
            or state and state not in JOB_STATES or result and result not in RESULTS):
        raise RequestError(400, "invalid_filters")
    if cursor and not CURSOR.fullmatch(cursor):
        raise RequestError(400, "invalid_cursor")
    return {key: value for key, value in (("repository", repo), ("state", state), ("result", result)) if value}, cursor


def row_anchor(row):
    kind, value = record_key(row)
    return sha256((kind + "\x00" + value).encode()).hexdigest()


def jobs_page(history, filters, cursor, cursor_key):
    generation = history["generation"]
    context = encode_json({"generation": generation, "export": history["export_fingerprint"], "filters": filters})

    def sign(anchor):
        return hmac.digest(cursor_key, context + anchor.encode("ascii"), "sha256").hex()

    rows = history["records"]
    repo, state, result = (filters.get(field, "") for field in ("repository", "state", "result"))
    matching = [row for row in rows
                if (not repo or repo.lower() in views.repository(row).lower())
                and (not state or state == row["state"]) and (not result or result == row.get("result"))]
    start = 0
    if cursor:
        anchor, signature = cursor.split(".")
        if not hmac.compare_digest(sign(anchor), signature):
            raise RequestError(409, "cursor_expired")
        for start, row in enumerate(matching, 1):
            if row_anchor(row) == anchor:
                break
        else:
            raise RequestError(409, "cursor_expired")
    page = matching[start:start + PAGE_SIZE]
    next_cursor = None
    if start + len(page) < len(matching):
        anchor = row_anchor(page[-1])
        next_cursor = anchor + "." + sign(anchor)
    return {key: value for key, value in history.items() if key not in {"records", "export_fingerprint"}} | {
        "read_schema_version": 1, "records": page, "matching": len(matching), "offset": start,
        "next_cursor": next_cursor,
    }


class WorkerObserver:
    def __init__(self, reader: SnapshotReader):
        self.reader = reader
        self._cursor_key = secrets.token_bytes(32)
        # The file reader keeps one validated job generation.
        self._read_lock = Lock()
        self.assets = {}
        for route, (content_type, name) in ASSETS.items():
            with (ASSET_DIR / name).open("rb") as asset:
                body = asset.read(MAX_RESPONSE + 1)
            if len(body) > MAX_RESPONSE:
                raise ValueError("asset_too_large")
            self.assets[route] = (content_type, body)

    def _response(self, method, target, headers):
        if method not in {"GET", "HEAD"}:
            raise RequestError(405, "method_not_allowed")
        path, _, query = target.partition("?")
        if path not in views.PAGES and path not in API and path not in self.assets:
            raise RequestError(404, "not_found")
        is_jobs = path in {"/jobs", "/api/v1/jobs"}
        if query and not is_jobs:
            raise RequestError(400, "invalid_request")
        if path in self.assets:
            content_type, body = self.assets[path]
            return 200, content_type, body

        filters, cursor = job_query(query) if is_jobs else ({}, "")
        with self._read_lock:
            now = int(time.time())
            return self._read(path, headers, now, filters, cursor)

    def _read(self, path, headers, now, filters, cursor):
        is_jobs = path in {"/jobs", "/api/v1/jobs"}
        try:
            if is_jobs:
                history = self.reader.read_job_history(headers, now=now)
                evidence = jobs_page(history, filters, cursor, self._cursor_key)
            else:
                evidence = self.reader.read(headers, now=now)
        except (StoreError, SnapshotError) as error:
            if path in API:
                raise RequestError(503, "observation_unavailable") from None
            return 200, HTML, views.page(path, None, now, filters, unavailable=error.code)
        except RequestError as error:
            if path != "/jobs" or error.code != "cursor_expired":
                raise
            return 409, HTML, views.page(path, None, now, filters, unavailable=error.code)

        if path == "/api/v1/capacity":
            evidence = {"read_schema_version": 1, "schema_version": evidence["schema_version"],
                        "controller": evidence["controller"], "clock_state": evidence["clock_state"],
                        "published_at": evidence["published_at"], "publisher_freshness": evidence["publisher_freshness"],
                        "capacity": evidence["sources"]["capacity"]}
        if path == "/api/v1/endpoint":
            evidence = {"read_schema_version": 1, **evidence}
        if path in API:
            return 200, JSON, encode_json(evidence)
        return 200, HTML, views.page(path, evidence, now, filters)


class ObserverServer(ThreadingTCPServer):
    """Owns an already listening IPv4 loopback socket and never binds a port."""

    def __init__(self, listener: socket.socket, observer: WorkerObserver):
        if (listener.family != socket.AF_INET or listener.getsockname()[0] != "127.0.0.1"
                or not listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN)):
            raise ValueError("invalid_listener")
        super().__init__(listener.getsockname(), None, bind_and_activate=False)
        self.socket.close()
        self.socket = listener
        self.observer = observer
        self.connections = BoundedSemaphore(MAX_CONNECTIONS)
        self.handlers = BoundedSemaphore(MAX_HANDLERS)

    def process_request(self, request, address):
        if not self.connections.acquire(blocking=False):
            self.close_request(request)
            return
        try:
            super().process_request(request, time.monotonic() + HEADER_SECONDS)
        except BaseException:
            self.connections.release()
            raise

    def process_request_thread(self, request, deadline):
        head = False
        try:
            try:
                method, target, headers = receive_headers(request, deadline)
            except RequestError as error:
                send_failure(request, error)
                return
            head = method == "HEAD"
            try:
                self.observer.reader.authenticate(headers)
            except SnapshotError:
                send_failure(request, RequestError(401, "authentication_failed"), head=head)
                return
            if not self.handlers.acquire(blocking=False):
                send_failure(request, RequestError(503, "observer_busy"), head=head)
                return
            try:
                try:
                    status, content_type, body = self.observer._response(method, target, headers)
                    if len(body) > MAX_RESPONSE:
                        raise RequestError(503, "response_too_large")
                    send_response(request, status, content_type, body, head=head)
                except RequestError as error:
                    send_failure(request, error, head=head)
            finally:
                self.handlers.release()
        except (OSError, EOFError):
            pass
        except Exception:
            try:
                send_failure(request, RequestError(503, "observer_unavailable"), head=head)
            except OSError:
                pass
        finally:
            self.shutdown_request(request)
            self.connections.release()


def send_failure(connection, error, *, head=False):
    send_response(connection, error.status, JSON, encode_json({"error": error.code}), head=head)


def send_response(connection, status, content_type, body, *, head):
    extra = {401: b'WWW-Authenticate: Bearer realm="ci-fleet-observer"\r\n',
             405: b"Allow: GET, HEAD\r\n"}.get(status, b"")
    headers = (f"HTTP/1.1 {status} {HTTPStatus(status).phrase}\r\nContent-Type: {content_type}\r\n"
               f"Content-Length: {len(body)}\r\n").encode("ascii") + NO_STORE + extra + b"\r\n"
    connection.settimeout(IO_SECONDS)
    connection.sendall(headers if head else headers + body)
