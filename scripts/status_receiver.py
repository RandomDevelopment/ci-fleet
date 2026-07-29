#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hmac
import http.server
import ipaddress
import json
import os
import re
import socket
import sqlite3
import threading
import time
import urllib.parse
from contextlib import closing
from pathlib import Path
from typing import Any, Mapping

from status_auth import verify_headers

CONTROLLER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
NONCE = re.compile(r"[0-9a-f]{32}")


class StatusError(ValueError):
    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status = status
        self.code = code


class StatusReceiver:
    def __init__(
        self,
        database: Path,
        controller_keys: Mapping[str, bytes],
        *,
        read_token: str,
        history_limit: int = 288,
        retention_seconds: int = 604_800,
        min_interval_seconds: int = 30,
        max_payload_bytes: int = 32_768,
        max_clock_skew_seconds: int = 300,
    ) -> None:
        if history_limit < 1 or retention_seconds < 1:
            raise ValueError("history and retention bounds must be positive")
        if len(set(controller_keys.values())) != len(controller_keys):
            raise ValueError("controller authentication keys must be unique")
        if any(hmac.compare_digest(key, read_token.encode()) for key in controller_keys.values()):
            raise ValueError("read token must differ from controller authentication keys")
        self.database = database
        self.controller_keys = dict(controller_keys)
        self.read_token = read_token
        self.history_limit = history_limit
        self.retention_seconds = retention_seconds
        self.min_interval_seconds = min_interval_seconds
        self.max_payload_bytes = max_payload_bytes
        self.max_clock_skew_seconds = max_clock_skew_seconds
        # ponytail: one receiver-wide lock; split by controller only if measured write contention warrants it.
        self._write_lock = threading.Lock()
        self._last_attempt: dict[str, int] = {}
        self._clock = time.time
        with closing(self._connect()) as connection, connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS reports (
                    controller TEXT NOT NULL,
                    generated_at INTEGER NOT NULL,
                    received_at INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (controller, generated_at)
                );
                CREATE TABLE IF NOT EXISTS nonces (
                    controller TEXT NOT NULL,
                    nonce TEXT NOT NULL,
                    authenticated_at INTEGER NOT NULL,
                    PRIMARY KEY (controller, nonce)
                );
            """)
        os.chmod(self.database, 0o600)
        self.expire()

    def _connect(self) -> sqlite3.Connection:
        self.database.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            descriptor = os.open(self.database, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        return sqlite3.connect(self.database)

    def submit(self, body: bytes, headers: Mapping[str, str], *, now: int) -> None:
        if len(body) > self.max_payload_bytes:
            raise StatusError(413, "payload_too_large")
        claimed = {name.lower(): value for name, value in headers.items()}.get("x-ci-fleet-controller", "")
        key = self.controller_keys.get(claimed)
        known_controller = key is not None
        try:
            controller, authenticated_at, nonce = verify_headers(headers, body, key or b"\0" * 32)
        except ValueError as error:
            raise StatusError(401, "authentication_failed") from error
        if not known_controller:
            raise StatusError(401, "authentication_failed")
        if controller != claimed or abs(now - authenticated_at) > self.max_clock_skew_seconds or not NONCE.fullmatch(nonce):
            raise StatusError(401, "authentication_stale")
        try:
            report = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise StatusError(400, "invalid_json") from error
        self._validate_minimum(report, controller)
        generated_at = report["generated_at"]
        if abs(now - generated_at) > self.max_clock_skew_seconds:
            raise StatusError(409, "report_time_stale")
        encoded = json.dumps(report, separators=(",", ":"), sort_keys=True)
        with self._write_lock, closing(self._connect()) as connection, connection:
            if connection.execute("SELECT 1 FROM nonces WHERE controller=? AND nonce=?", (controller, nonce)).fetchone():
                raise StatusError(409, "replayed_report")
            last_attempt = self._last_attempt.get(controller)
            if last_attempt is not None and now - last_attempt < 1:
                raise StatusError(429, "submission_too_frequent")
            self._last_attempt[controller] = now
            try:
                connection.execute("INSERT INTO nonces VALUES (?, ?, ?)", (controller, nonce, authenticated_at))
                connection.commit()
            except sqlite3.IntegrityError as error:
                raise StatusError(409, "replayed_report") from error
            last = connection.execute(
                "SELECT generated_at, received_at FROM reports WHERE controller=? ORDER BY generated_at DESC LIMIT 1",
                (controller,),
            ).fetchone()
            if last and generated_at <= last[0]:
                raise StatusError(409, "stale_report")
            if last and now - last[1] < self.min_interval_seconds:
                raise StatusError(429, "submission_too_frequent")
            connection.execute("INSERT INTO reports VALUES (?, ?, ?, ?)", (controller, generated_at, now, encoded))
            connection.execute("DELETE FROM reports WHERE received_at < ?", (now - self.retention_seconds,))
            connection.execute(
                "DELETE FROM reports WHERE controller=? AND rowid NOT IN (SELECT rowid FROM reports WHERE controller=? ORDER BY generated_at DESC LIMIT ?)",
                (controller, controller, self.history_limit),
            )
            connection.execute("DELETE FROM nonces WHERE authenticated_at < ?", (now - self.max_clock_skew_seconds,))

    def _expire(self, connection: sqlite3.Connection, now: int) -> None:
        connection.execute("DELETE FROM reports WHERE received_at < ?", (now - self.retention_seconds,))
        connection.execute("""
            DELETE FROM reports WHERE rowid IN (
                SELECT rowid FROM (
                    SELECT rowid, ROW_NUMBER() OVER (PARTITION BY controller ORDER BY generated_at DESC, rowid DESC) AS rank
                    FROM reports
                ) WHERE rank > ?
            )
        """, (self.history_limit,))
        connection.execute("DELETE FROM nonces WHERE authenticated_at < ?", (now - self.max_clock_skew_seconds,))

    def expire(self) -> None:
        with self._write_lock, closing(self._connect()) as connection, connection:
            self._expire(connection, int(self._clock()))

    @staticmethod
    def _validate_minimum(report: Any, controller: str) -> None:
        def exact(value: Any, keys: set[str]) -> bool:
            return isinstance(value, dict) and set(value) == keys

        def integer(value: Any, minimum: int = 0) -> bool:
            return isinstance(value, int) and not isinstance(value, bool) and value >= minimum

        def number(value: Any, minimum: float = 0) -> bool:
            return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= minimum and value < float("inf")

        def enum(value: Any, choices: set[str]) -> bool:
            return isinstance(value, str) and value in choices

        if not isinstance(report, dict) or type(report.get("schema_version")) is not int or report["schema_version"] != 1:
            raise StatusError(400, "unsupported_schema")
        root_keys = {"schema_version", "controller", "configuration", "reconciliation", "drift", "process", "timers", "runners", "metrics", "docker", "error", "generated_at"}
        if set(report) != root_keys:
            raise StatusError(400, "invalid_report")
        identity = report["controller"]
        if not exact(identity, {"id", "software_version", "boot_time", "ssh"}) or identity["id"] != controller or not CONTROLLER_ID.fullmatch(controller):
            raise StatusError(403, "controller_identity_mismatch")
        if not isinstance(identity["software_version"], str) or not re.fullmatch(r"[A-Za-z0-9_.+-]{1,64}", identity["software_version"]):
            raise StatusError(400, "invalid_report")
        if not integer(identity["boot_time"]) or not enum(identity["ssh"], {"enabled", "disabled", "unknown"}):
            raise StatusError(400, "invalid_report")
        generated_at = report["generated_at"]
        if not integer(generated_at) or identity["boot_time"] > generated_at:
            raise StatusError(400, "invalid_report")

        configuration = report["configuration"]
        commit = lambda value: isinstance(value, str) and (value == "" or re.fullmatch(r"[0-9a-f]{40}", value))
        if not exact(configuration, {"desired_commit", "applied_commit"}) or not all(commit(configuration[name]) for name in configuration):
            raise StatusError(400, "invalid_report")
        reconciliation = report["reconciliation"]
        reconcile_states = {"bootstrap", "converged", "drift", "failed", "invalid", "missing", "pending", "reconciling", "rolled_back", "unknown"}
        if not exact(reconciliation, {"state", "last_success_at"}) or not enum(reconciliation["state"], reconcile_states):
            raise StatusError(400, "invalid_report")
        if reconciliation["last_success_at"] is not None and (not integer(reconciliation["last_success_at"]) or reconciliation["last_success_at"] > generated_at):
            raise StatusError(400, "invalid_report")
        if not exact(report["drift"], {"state"}) or not enum(report["drift"]["state"], {"ok", "stale", "failed", "unknown"}):
            raise StatusError(400, "invalid_report")
        process = report["process"]
        if not exact(process, {"state", "restart_count"}) or not enum(process["state"], {"created", "exited", "missing", "paused", "restarting", "running", "unknown"}) or not integer(process["restart_count"]):
            raise StatusError(400, "invalid_report")
        timers = report["timers"]
        if not exact(timers, {"reconciliation", "drift", "health", "cleanup"}) or any(not enum(value, {"ok", "stale", "failed", "unknown"}) for value in timers.values()):
            raise StatusError(400, "invalid_report")
        runners = report["runners"]
        if not exact(runners, {"current", "busy", "maximum"}) or not all(integer(value) for value in runners.values()) or not (runners["busy"] <= runners["current"] <= runners["maximum"]):
            raise StatusError(400, "invalid_report")

        metrics = report["metrics"]
        if not exact(metrics, {"cpu", "memory", "swap", "disk", "inodes", "load"}):
            raise StatusError(400, "invalid_report")
        cpu = metrics["cpu"]
        if not exact(cpu, {"logical", "used_percent"}) or not integer(cpu["logical"], 1) or not number(cpu["used_percent"]) or cpu["used_percent"] > 100:
            raise StatusError(400, "invalid_report")
        for name in ("memory", "swap"):
            value = metrics[name]
            total_key, part_key = ("total_bytes", "available_bytes") if name == "memory" else ("total_bytes", "used_bytes")
            if not exact(value, {total_key, part_key}) or not integer(value[total_key]) or not integer(value[part_key]) or value[part_key] > value[total_key]:
                raise StatusError(400, "invalid_report")
        for group, keys in (("disk", {"total_bytes", "used_bytes"}), ("inodes", {"total", "used"})):
            if not exact(metrics[group], {"root", "docker"}):
                raise StatusError(400, "invalid_report")
            total_key, used_key = tuple(keys)
            if total_key.startswith("used"):
                total_key, used_key = used_key, total_key
            for value in metrics[group].values():
                if not exact(value, keys) or not integer(value[total_key]) or not integer(value[used_key]) or value[used_key] > value[total_key]:
                    raise StatusError(400, "invalid_report")
        load = metrics["load"]
        if not exact(load, {"one", "five", "fifteen"}) or not all(number(value) for value in load.values()):
            raise StatusError(400, "invalid_report")
        docker = report["docker"]
        if not exact(docker, {"healthy", "oom"}) or not all(isinstance(value, bool) for value in docker.values()):
            raise StatusError(400, "invalid_report")
        error = report["error"]
        if error is not None and (not exact(error, {"code", "message"}) or not isinstance(error["code"], str) or not re.fullmatch(r"[a-z0-9_]{1,64}", error["code"]) or error["message"] != error["code"].replace("_", " ")):
            raise StatusError(400, "invalid_report")

    def _authorize_read(self, read_token: str) -> None:
        if not hmac.compare_digest(read_token.encode(), self.read_token.encode()):
            raise StatusError(401, "read_authentication_failed")

    def latest(self, controller: str, read_token: str) -> dict[str, Any] | None:
        self._authorize_read(read_token)
        with self._write_lock, closing(self._connect()) as connection, connection:
            self._expire(connection, int(self._clock()))
            row = connection.execute(
                "SELECT payload FROM reports WHERE controller=? ORDER BY generated_at DESC LIMIT 1", (controller,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def history(self, controller: str, read_token: str, limit: int | None = None) -> list[dict[str, Any]]:
        self._authorize_read(read_token)
        count = min(max(limit or self.history_limit, 1), self.history_limit)
        with self._write_lock, closing(self._connect()) as connection, connection:
            self._expire(connection, int(self._clock()))
            rows = connection.execute(
                "SELECT payload FROM reports WHERE controller=? ORDER BY generated_at DESC LIMIT ?", (controller, count)
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def latest_and_history(self, controller: str, read_token: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        self._authorize_read(read_token)
        with self._write_lock, closing(self._connect()) as connection, connection:
            self._expire(connection, int(self._clock()))
            rows = connection.execute(
                "SELECT payload FROM reports WHERE controller = ? ORDER BY generated_at DESC LIMIT ?",
                (controller, self.history_limit),
            ).fetchall()
        history = [json.loads(row[0]) for row in rows]
        return (history[0] if history else None), history

    def list_latest(self, read_token: str) -> list[dict[str, Any]]:
        self._authorize_read(read_token)
        with self._write_lock, closing(self._connect()) as connection, connection:
            self._expire(connection, int(self._clock()))
            rows = connection.execute("""
                SELECT reports.payload FROM reports
                JOIN (SELECT controller, MAX(generated_at) AS generated_at FROM reports GROUP BY controller) latest
                USING (controller, generated_at)
                ORDER BY reports.controller
            """).fetchall()
        return [json.loads(row[0]) for row in rows]


class _BoundedHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args: Any, max_requests: int = 32, request_timeout: int = 15, **kwargs: Any) -> None:
        self.max_requests = max_requests
        self.request_timeout = request_timeout
        self._slots = threading.BoundedSemaphore(max_requests)
        self._deadlines: dict[Any, threading.Timer] = {}
        super().__init__(*args, **kwargs)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            request.close()
            return
        try:
            request.settimeout(self.request_timeout)
            timer = threading.Timer(self.request_timeout, self._expire_request, (request,))
            timer.daemon = True
            self._deadlines[request] = timer
            timer.start()
            super().process_request(request, client_address)
        except Exception:
            timer = self._deadlines.pop(request, None)
            if timer:
                timer.cancel()
            self._slots.release()
            raise

    @staticmethod
    def _expire_request(request: Any) -> None:
        try:
            request.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            timer = self._deadlines.pop(request, None)
            if timer:
                timer.cancel()
            self._slots.release()


def create_server(bind: str, port: int, receiver: StatusReceiver) -> _BoundedHTTPServer:
    class Handler(http.server.BaseHTTPRequestHandler):
        def send_json(self, status: int, value: Any) -> None:
            body = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def bearer(self) -> str:
            authorization = self.headers.get("Authorization", "")
            return authorization[7:] if authorization.startswith("Bearer ") else ""

        def do_POST(self) -> None:
            if self.path != "/v1/status":
                self.send_json(404, {"error": "not_found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "-1"))
                if length < 0 or length > receiver.max_payload_bytes:
                    raise StatusError(413, "payload_too_large")
                receiver.submit(self.rfile.read(length), dict(self.headers.items()), now=int(__import__("time").time()))
                self.send_json(202, {"accepted": True})
            except (ValueError, StatusError) as error:
                failure = error if isinstance(error, StatusError) else StatusError(400, "invalid_request")
                self.send_json(failure.status, {"error": failure.code})

        def do_GET(self) -> None:
            path = urllib.parse.urlsplit(self.path)
            try:
                if path.query or path.fragment:
                    raise StatusError(400, "invalid_request")
                if path.path == "/v1/controllers":
                    value = {"schema_version": 1, "controllers": receiver.list_latest(self.bearer())}
                elif path.path.startswith("/v1/controllers/"):
                    controller = urllib.parse.unquote(path.path.removeprefix("/v1/controllers/"))
                    if not CONTROLLER_ID.fullmatch(controller):
                        raise StatusError(404, "not_found")
                    latest, history = receiver.latest_and_history(controller, self.bearer())
                    value = {
                        "schema_version": 1,
                        "latest": latest,
                        "history": history,
                    }
                else:
                    raise StatusError(404, "not_found")
                self.send_json(200, value)
            except StatusError as error:
                self.send_json(error.status, {"error": error.code})

        def log_message(self, format: str, *args: Any) -> None:
            pass

    if ipaddress.ip_address(bind).version == 6:
        class IPv6Server(_BoundedHTTPServer):
            address_family = socket.AF_INET6
        return IPv6Server((bind, port), Handler)
    return _BoundedHTTPServer((bind, port), Handler)


def _read_secret(path: Path, *, textual: bool = False) -> bytes:
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError(f"secret must be owned by the receiver user with mode 0600: {path}")
    value = path.read_bytes()
    if textual:
        value = value.strip()
        if any(byte < 0x21 or byte > 0x7e for byte in value):
            raise ValueError(f"textual secret must contain visible ASCII only: {path}")
    if not 32 <= len(value) <= 128:
        raise ValueError(f"secret must contain 32-128 bytes: {path}")
    return value


def load_auth_config(path: Path) -> tuple[dict[str, bytes], str]:
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError(f"auth config must be owned by the receiver user with mode 0600: {path}")
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or set(value) != {"controllers", "read_token_file"} or not isinstance(value["controllers"], dict):
        raise ValueError("auth config must contain controllers and read_token_file only")
    resolve = lambda name: Path(name) if Path(name).is_absolute() else path.parent / name
    keys = {}
    for controller, key_file in value["controllers"].items():
        if not isinstance(controller, str) or not CONTROLLER_ID.fullmatch(controller) or not isinstance(key_file, str):
            raise ValueError("invalid controller auth mapping")
        keys[controller] = _read_secret(resolve(key_file))
    if not keys or not isinstance(value["read_token_file"], str):
        raise ValueError("auth config requires at least one controller and a read token")
    return keys, _read_secret(resolve(value["read_token_file"]), textual=True).decode()


def _expiration_loop(receiver: StatusReceiver, stop: threading.Event, interval: float = 60) -> None:
    while not stop.wait(interval):
        try:
            receiver.expire()
        except (OSError, sqlite3.Error):
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Authenticated ci-fleet status receiver")
    parser.add_argument("--auth-config", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--history-limit", type=int, default=288)
    parser.add_argument("--retention-seconds", type=int, default=604_800)
    args = parser.parse_args()
    if not ipaddress.ip_address(args.bind).is_loopback:
        parser.error("--bind must be a loopback address; terminate HTTPS in a reverse proxy")
    keys, read_token = load_auth_config(args.auth_config)
    receiver = StatusReceiver(
        args.database, keys, read_token=read_token,
        history_limit=args.history_limit, retention_seconds=args.retention_seconds,
    )
    stop = threading.Event()
    maintenance = threading.Thread(target=_expiration_loop, args=(receiver, stop), daemon=True)
    maintenance.start()
    server = create_server(args.bind, args.port, receiver)
    try:
        server.serve_forever()
    finally:
        stop.set()
        maintenance.join()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
