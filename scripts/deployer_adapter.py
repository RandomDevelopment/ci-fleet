#!/usr/bin/env python3
"""Constrain application adapters to one rootless Docker target."""

from __future__ import annotations

import argparse
import contextlib
import ctypes
from datetime import datetime, timezone
import errno
import grp
import hashlib
import http.client
import http.server
import json
import os
import pwd
from pathlib import Path
import re
import shutil
import signal
import socket
import socketserver
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

MAX_REQUEST = 1024 * 1024
PR_SET_DUMPABLE = 4
MAX_RESPONSE = 32 * 1024 * 1024
CONTAINER_LIMITS = {"Memory": 2 * 1024**3, "NanoCpus": 2 * 10**9, "PidsLimit": 256}
LABEL = "io.randomdevelopment.ci-fleet."
SERVICE_LABEL = LABEL + "service"
EXCLUSIVE_SERVICE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
GRACEFUL_STOP_SIGNALS = frozenset({"SIGTERM", "SIGINT", "SIGQUIT", "SIGHUP", "SIGUSR1", "SIGUSR2", "SIGWINCH"})
DEFAULT_ROLLBACK_STOP_TIMEOUT = 120
MAX_ROLLBACK_STOP_TIMEOUT = 600
MAX_ROLLBACK_STOP_TOTAL = 1800
MAX_EXCLUSIVE_SERVICES = 32
FORCED_KILL_VERIFY_SECONDS = 30
IMAGE = re.compile(r"^[a-z0-9][a-z0-9.:-]*/[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}$")
UNIT = re.compile(r"^ci-fleet-deployer-adapter-[0-9]+-[0-9]+\.service$")
CHROMIUM_SECCOMP_V1_SHA256 = "f8f5cbe84f4135431d920e43e676b641d538ea24be76ddef4656a86015bac6e7"


def normalize_graceful_signal(raw: object) -> str:
    """Normalize a container StopSignal to a reviewed graceful SIG* name."""
    if raw is None or raw == "":
        return "SIGTERM"
    require(isinstance(raw, str) and not raw.isdigit(), "unsupported stop signal")
    name = raw.upper()
    if not name.startswith("SIG"):
        name = "SIG" + name
    require(name in GRACEFUL_STOP_SIGNALS, f"unsupported stop signal: {raw}")
    return name


def _parse_single_stop_policy(values: dict[str, str]) -> tuple[list[str], int | None, str | None]:
    raw_services = values.get("ROLLBACK_EXCLUSIVE_SERVICES")
    if raw_services is None:
        require("ROLLBACK_STOP_TIMEOUT_SECONDS" not in values, "ROLLBACK_STOP_TIMEOUT_SECONDS requires ROLLBACK_EXCLUSIVE_SERVICES")
        require("ROLLBACK_STOP_SIGNAL" not in values, "ROLLBACK_STOP_SIGNAL requires ROLLBACK_EXCLUSIVE_SERVICES")
        return [], None, None
    require(bool(raw_services), "ROLLBACK_EXCLUSIVE_SERVICES must not be empty")
    items = raw_services.split(",")
    require(1 <= len(items) <= MAX_EXCLUSIVE_SERVICES, "ROLLBACK_EXCLUSIVE_SERVICES exceeds maximum count")
    prefix = "ci-fleet-deploy-" + values.get("DEPLOYER_IDENTITY", "") + "-"
    seen: set[str] = set()
    for item in items:
        require(bool(EXCLUSIVE_SERVICE.fullmatch(item)), f"invalid exclusive service name: {item}")
        require(item not in seen, f"duplicate exclusive service name: {item}")
        require(len(prefix + item) <= 128, f"exclusive service container name exceeds 128 characters: {item}")
        seen.add(item)
    timeout: int | None = None
    if "ROLLBACK_STOP_TIMEOUT_SECONDS" in values:
        raw_timeout = values["ROLLBACK_STOP_TIMEOUT_SECONDS"]
        require(bool(re.fullmatch(r"[1-9][0-9]{0,2}", raw_timeout)), "ROLLBACK_STOP_TIMEOUT_SECONDS must be a positive integer")
        timeout = int(raw_timeout)
        require(1 <= timeout <= MAX_ROLLBACK_STOP_TIMEOUT, "ROLLBACK_STOP_TIMEOUT_SECONDS must be between 1 and 600")
    signal_name: str | None = None
    if "ROLLBACK_STOP_SIGNAL" in values:
        signal_name = values["ROLLBACK_STOP_SIGNAL"]
        require(signal_name in GRACEFUL_STOP_SIGNALS, "ROLLBACK_STOP_SIGNAL must be a supported graceful signal")
    return items, timeout, signal_name


def parse_rollback_stop_policy(values: dict[str, str], incumbent_values: dict[str, str] | None = None) -> dict:
    """Validate and merge exclusive-service rollback policy fields from active and LKG policies."""
    lkg_services, lkg_timeout, lkg_signal = _parse_single_stop_policy(values)
    if lkg_timeout is not None:
        require(len(lkg_services) * lkg_timeout <= MAX_ROLLBACK_STOP_TOTAL, "rollback stop timeout budget exceeds 1800 seconds")
    if incumbent_values is not None:
        for key in ("ENVIRONMENT", "TARGET_ID", "DEPLOYER_IDENTITY"):
            require(incumbent_values.get(key) == values.get(key), f"incumbent policy {key} does not match rollback policy")
        if incumbent_values.get("DOCKER_SOCKET") is not None or values.get("DOCKER_SOCKET") is not None:
            require(incumbent_values.get("DOCKER_SOCKET") == values.get("DOCKER_SOCKET"),
                    "incumbent policy DOCKER_SOCKET does not match rollback policy")
        inc_services, inc_timeout, inc_signal = _parse_single_stop_policy(incumbent_values)
        if inc_timeout is not None:
            require(len(inc_services) * inc_timeout <= MAX_ROLLBACK_STOP_TOTAL, "incumbent rollback stop timeout budget exceeds 1800 seconds")
        services = list(dict.fromkeys([*inc_services, *lkg_services]))
        require(len(services) <= MAX_EXCLUSIVE_SERVICES, "combined exclusive services exceed maximum count")
        timeout = inc_timeout if inc_timeout is not None else (lkg_timeout if lkg_timeout is not None else (DEFAULT_ROLLBACK_STOP_TIMEOUT if services else 0))
        signal_name = inc_signal if inc_signal is not None else lkg_signal
    else:
        services = lkg_services
        timeout = lkg_timeout if lkg_timeout is not None else (DEFAULT_ROLLBACK_STOP_TIMEOUT if services else 0)
        signal_name = lkg_signal
    if services:
        require(len(services) * timeout <= MAX_ROLLBACK_STOP_TOTAL, "combined rollback stop timeout budget exceeds 1800 seconds")
    return {"services": services, "timeout_seconds": timeout, "signal": signal_name}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON key")
        result[key] = value
    return result


def decode(body: bytes) -> dict:
    value = json.loads(body, object_pairs_hook=unique_object,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid JSON number")))
    require(isinstance(value, dict), "request must be a JSON object")
    return value


def security_options(value: object) -> bool:
    if value in (["no-new-privileges"], ["no-new-privileges:true"]):
        return True
    if not isinstance(value, list) or len(value) != 2 or value[0] not in ("no-new-privileges", "no-new-privileges:true"):
        return False
    option = value[1]
    if not isinstance(option, str) or not option.startswith("seccomp="):
        return False
    try:
        profile = decode(option[len("seccomp="):].encode())
        canonical = json.dumps(profile, sort_keys=True, separators=(",", ":")).encode()
    except (ValueError, UnicodeError, RecursionError):
        return False
    return hashlib.sha256(canonical).hexdigest() == CHROMIUM_SECCOMP_V1_SHA256


def protected_file(path: Path, uid: int = 0, *, adapter_fd: bool = False) -> bytes:
    descriptor_path = adapter_fd and bool(re.fullmatch(r"/proc/[0-9]+/fd/7", str(path)))
    require(path.is_absolute() and (descriptor_path or path.resolve() == path), "protected path is not canonical")
    flags = os.O_RDONLY | os.O_NONBLOCK | (0 if descriptor_path else os.O_NOFOLLOW)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == uid and info.st_nlink == 1
                and stat.S_IMODE(info.st_mode) in (0o600, 0o700), "protected file is unsafe")
        body = stream.read(MAX_REQUEST + 1)
        require(len(body) <= MAX_REQUEST, "protected file is too large")
        return body


def parse_policy(body: bytes) -> dict[str, str]:
    result = {}
    for line in body.decode().splitlines():
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        require(bool(separator) and key not in result and bool(re.fullmatch(r"[A-Z][A-Z0-9_]*", key)),
                "invalid policy")
        result[key] = value
    return result


def reject_acl(path: Path | int) -> None:
    for name in ("system.posix_acl_access", "system.posix_acl_default"):
        try:
            os.getxattr(path, name, **({} if isinstance(path, int) else {"follow_symlinks": False}))
        except OSError as error:
            require(error.errno in (errno.ENODATA, errno.ENOTSUP), "access ACL cannot be inspected")
        else:
            raise ValueError("extended access or default ACL is forbidden")


def check_backend_socket(path: str, uid: int, gid: int) -> None:
    metadata = os.lstat(path)
    require(stat.S_ISSOCK(metadata.st_mode) and metadata.st_uid == uid and metadata.st_gid == gid
            and stat.S_IMODE(metadata.st_mode) in (0o600, 0o660), "rootless Docker socket permissions are unsafe")
    for entry in (Path(path), *Path(path).parents):
        reject_acl(entry)
        if entry != Path(path):
            parent = os.lstat(entry)
            require(stat.S_ISDIR(parent.st_mode) and parent.st_uid in (0, uid)
                    and not parent.st_mode & 0o022, "backend socket ancestry is unsafe")
    try:
        group = grp.getgrgid(gid)
        require(all(pwd.getpwnam(name).pw_uid in (0, uid) for name in group.gr_mem)
                and all(user.pw_uid in (0, uid) for user in pwd.getpwall() if user.pw_gid == gid),
                "Docker daemon group has unrelated members")
    except KeyError:
        raise ValueError("Docker daemon group membership is unavailable") from None


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, *, rootless: bool = True, timeout: float = 120):
        super().__init__("localhost", timeout=timeout)
        self.path, self.rootless, self.peer_uid, self.peer_gid = path, rootless, None, None

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)
        pid, uid, gid = struct.unpack("3i", self.sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if self.rootless:
            require(uid > 0 and gid > 0, "Docker daemon must run without host-root authority")
            check_backend_socket(self.path, uid, gid)
            process = {}
            for line in Path(f"/proc/{pid}/status").read_text().splitlines():
                key, _, value = line.partition(":")
                process[key] = value.split()
            require(all(int(value) == uid for value in process["Uid"])
                    and all(int(value) == gid for value in process["Gid"]), "Docker peer retains another user or group identity")
            groups = {int(value) for value in process["Groups"]} | {gid}
            for name in ("sudo", "wheel", "docker"):
                try:
                    require(grp.getgrnam(name).gr_gid not in groups, "Docker peer has a privileged group")
                except KeyError:
                    pass
            require(0 not in groups, "Docker peer belongs to the root group")
            raw = Path("/run/docker.sock")
            if raw.exists() and str(raw.resolve()) != self.path:
                try:
                    os.getxattr(raw, "system.posix_acl_access")
                except OSError as error:
                    require(error.errno in (errno.ENODATA, errno.ENOTSUP), "host Docker socket ACL cannot be inspected")
                else:
                    raise ValueError("host Docker socket has an extended access ACL")
                raw_info = raw.stat()
                require(not raw_info.st_mode & 0o002
                        and not (raw_info.st_uid == uid and raw_info.st_mode & 0o200)
                        and (not raw_info.st_mode & 0o020 or raw_info.st_gid not in groups),
                        "Docker peer can access the host-root daemon")
        self.peer_uid, self.peer_gid = uid, gid


def reject_rootful_workloads(backend_path: str, deadline: float | None = None) -> None:
    socket_path = Path("/run/docker.sock")
    if not socket_path.exists() or str(socket_path.resolve()) == backend_path:
        return
    connection = UnixConnection(str(socket_path), rootless=False, timeout=30)
    try:
        deadline = deadline if deadline is not None else time.monotonic() + 30
        for path in ("/containers/json?all=1", "/networks", "/volumes"):
            remaining = deadline - time.monotonic()
            require(remaining > 0, "host Docker inventory deadline elapsed")
            connection.timeout = remaining
            if connection.sock is not None:
                connection.sock.settimeout(remaining)
            connection.request("GET", path)
            response = connection.getresponse()
            body = response.read(MAX_REQUEST + 1)
            require(response.status == 200 and len(body) <= MAX_REQUEST, "host Docker inventory is unavailable")
            value = json.loads(body, object_pairs_hook=unique_object)
            if path == "/volumes":
                require(isinstance(value, dict), "invalid host volume inventory")
                value = value.get("Volumes") or []
            require(isinstance(value, list), "invalid host Docker inventory")
            if path == "/networks":
                require(all(isinstance(item, dict) and item.get("Name") in ("bridge", "host", "none") for item in value),
                        "unrelated host Docker network is present")
            else:
                require(not value, "unrelated host Docker workload is present")
    finally:
        connection.close()


class _DockerClient:
    rootless = True
    uid = None
    gid = None
    _hard_stop_deadline = False

    @contextlib.contextmanager
    def stop_request_alarm(self):
        if not self._hard_stop_deadline:
            yield
            return

        # Only the dedicated stop helper's main thread uses the alarm.
        def expired(_signum, _frame):
            raise TimeoutError("Docker stop deadline elapsed")

        previous_handler = signal.signal(signal.SIGALRM, expired)
        try:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Docker stop deadline elapsed")
            signal.setitimer(signal.ITIMER_REAL, remaining)
            yield
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)

    def request(self, method: str, path: str, body: bytes = b"", headers: dict | None = None) -> tuple[int, bytes]:
        remaining = self.deadline - time.monotonic()
        require(remaining > 0, "Docker operation deadline elapsed")
        connection = UnixConnection(self.path, rootless=self.rootless, timeout=remaining)
        with self.stop_request_alarm():
            try:
                connection.connect()
                if self.rootless:
                    require((connection.peer_uid, connection.peer_gid) == (self.uid, self.gid), "Docker daemon identity changed")
                connection.request(method, path, body, headers or {})
                response = connection.getresponse()
                result = response.read(MAX_RESPONSE + 1)
                require(len(result) <= MAX_RESPONSE, "Docker response exceeded the bound")
                return response.status, result
            finally:
                connection.close()

    def inspect(self, kind: str, name: str) -> dict:
        suffix = "/json" if kind in ("containers", "images") else ""
        status, body = self.request("GET", f"/{kind}/{urllib.parse.quote(name, safe='')}{suffix}")
        require(status == 200, "owned Docker resource could not be inspected")
        return decode(body)


class Backend(_DockerClient):
    def __init__(self, path: str, seconds: int = 120):
        self.deadline = time.monotonic() + seconds + 10
        require(bool(re.fullmatch(r"/[A-Za-z0-9._/-]+", path)) and os.path.realpath(path) == path, "Docker socket must be canonical")
        self.path = path
        self.rootless = True
        startup_deadline = min(self.deadline, time.monotonic() + 30)
        connection = UnixConnection(path, timeout=max(0.001, startup_deadline - time.monotonic()))
        try:
            connection.request("GET", "/info")
            response = connection.getresponse()
            value = decode(response.read(MAX_REQUEST + 1))
            require(response.status == 200 and "name=rootless" in value.get("SecurityOptions", []),
                    "a dedicated rootless Docker daemon is required")
            self.uid, self.gid = connection.peer_uid, connection.peer_gid
            reject_rootful_workloads(path, startup_deadline)
        finally:
            connection.close()


class TestingDockerBackend(_DockerClient):
    """Test-only Docker socket client for non-rootless local test/CI daemons."""

    def __init__(self, path: str, seconds: int = 120):
        require(os.environ.get("CI_FLEET_DEPLOYER_TESTING") == "1"
                and os.environ.get("CI_FLEET_DEPLOYER_TEST_DOCKER_SOCKET") == "1",
                "test Docker socket override is test-only")
        self.deadline = time.monotonic() + seconds + 10
        require(bool(re.fullmatch(r"/[A-Za-z0-9._/-]+", path)) and os.path.realpath(path) == path, "Docker socket must be canonical")
        self.path = path
        self.rootless = False


def ownership_labels(values: dict[str, str]) -> dict[str, str]:
    return {
        LABEL + "role": "deployer",
        LABEL + "identity": values["DEPLOYER_IDENTITY"],
        LABEL + "environment": values["ENVIRONMENT"],
        LABEL + "target": values["TARGET_ID"],
    }


def container_labels(entry: dict) -> dict | None:
    config = entry.get("Config")
    if isinstance(config, dict) and "Labels" in config:
        labels = config.get("Labels")
    else:
        labels = entry.get("Labels")
    return labels if isinstance(labels, dict) else None


def container_owned(entry: dict, expected_labels: dict[str, str]) -> bool:
    labels = container_labels(entry)
    return labels is not None and all(labels.get(k) == v for k, v in expected_labels.items())


def container_matches_service(entry: dict, prefix: str, service: str, expected_labels: dict[str, str]) -> tuple[bool, bool]:
    """Return (matches, conflict) for an owned container relative to an exclusive service."""
    if not container_owned(entry, expected_labels):
        return False, False
    labels = container_labels(entry) or {}
    raw_names = entry.get("Names")
    if isinstance(raw_names, list):
        names = {n.lstrip("/") for n in raw_names if isinstance(n, str)}
    elif isinstance(entry.get("Name"), str):
        names = {entry["Name"].lstrip("/")}
    else:
        names = set()
    name_matches = (prefix + service) in names
    svc_label = labels.get(SERVICE_LABEL)
    if svc_label is not None and not isinstance(svc_label, str):
        return False, True
    if name_matches and svc_label and svc_label != service:
        return False, True
    if svc_label == service or (not svc_label and name_matches):
        return True, False
    return False, False


def container_exited_and_non_restartable(info: dict) -> tuple[bool, str]:
    """Return (True, 'exited') only when Docker reports a non-running, non-restartable container."""
    state = info.get("State")
    host = info.get("HostConfig")
    if not isinstance(state, dict) or not isinstance(host, dict):
        return False, "container_state_unavailable"
    restart_policy = host.get("RestartPolicy")
    if restart_policy is None:
        restart_name = ""
    elif isinstance(restart_policy, dict):
        restart_name = restart_policy.get("Name", "")
    else:
        restart_name = None
    if restart_name not in ("", "no"):
        return False, "restart_policy_active"
    status = state.get("Status")
    if status not in ("exited", "dead", "created"):
        return False, f"status_{status}"
    if state.get("Running") is not False or state.get("Restarting") is not False or state.get("Paused") is not False or state.get("Pid") != 0:
        return False, "container_still_active"
    return True, "exited"


def container_active(info: dict) -> bool:
    state = info.get("State")
    if not isinstance(state, dict):
        return True
    return not (
        state.get("Status") in ("exited", "dead", "created")
        and state.get("Running") is False
        and state.get("Restarting") is False
        and state.get("Paused") is False
        and state.get("Pid") == 0
    )


def list_owned_containers(backend, expected_labels: dict[str, str]) -> list[dict]:
    filters = json.dumps({"label": [f"{k}={v}" for k, v in expected_labels.items()]}, separators=(",", ":"))
    status, body = backend.request("GET", "/containers/json?" + urllib.parse.urlencode({"all": "1", "filters": filters}))
    require(status == 200, "owned container inventory is unavailable")
    items = json.loads(body, object_pairs_hook=unique_object)
    require(isinstance(items, list), "invalid container inventory")
    return [item for item in items if isinstance(item, dict) and container_owned(item, expected_labels)]


class RollbackHealthStarting(ValueError):
    pass


class Policy:
    def __init__(self, values: dict[str, str], backend, credential: str = "", marker: Path | None = None, operation: str = "deploy", uncertain_marker: Path | None = None, artifact_store: Path | None = None, protected_images: set[str] | None = None, unit: str | None = None, exclusive_services: tuple[str, ...] | list[str] = ()):
        self.values, self.backend, self.credential, self.marker = values, backend, credential, marker
        self.operation, self.uncertain_marker, self.uncertain = operation, uncertain_marker, False
        self.rollback_outcome = None
        self.unit = unit
        self.exclusive_services = tuple(exclusive_services)
        self.required_rollback_services = _parse_single_stop_policy(values)[0] if operation == "rollback" else []
        self.verified_containers: list[str] = []
        self.rollback_ready: Path | None = None
        self.rollback_incumbents: dict[str, str] = {}
        self._exclusive_lock = threading.Lock()
        self._image_id: str | None = None
        self.artifact_store = artifact_store
        self.protected_images = (protected_images or set()) | {values["ARTIFACT_IMAGE"]}
        self.image = values["ARTIFACT_IMAGE"]
        require(bool(IMAGE.fullmatch(self.image)), "approved artifact is not immutable")
        self.labels = ownership_labels(values)
        self.prefix = "ci-fleet-deploy-" + values["DEPLOYER_IDENTITY"] + "-"

    def _container_matches_approved_image(self, info: dict) -> bool:
        if self._image_id is None:
            image_id = self.backend.inspect("images", self.image).get("Id", "")
            require(isinstance(image_id, str) and bool(re.fullmatch(r"sha256:[0-9a-f]{64}", image_id)),
                    "approved rollback image ID is unavailable")
            self._image_id = image_id
        return info.get("Image") == self._image_id

    def _require_no_active_service_container(self, service: str, ignore_id: str | None = None) -> None:
        for item in list_owned_containers(self.backend, self.labels):
            cid = item.get("Id")
            require(isinstance(cid, str) and bool(cid), "invalid owned container identity")
            if ignore_id and cid == ignore_id:
                continue
            matches, conflict = container_matches_service(item, self.prefix, service, self.labels)
            require(not conflict, "ambiguous exclusive service identity")
            if matches:
                info = self.owned("containers", cid)
                m2, c2 = container_matches_service(info, self.prefix, service, self.labels)
                require(m2 and not c2 and info.get("Id") == cid, "exclusive service identity changed")
                require(container_exited_and_non_restartable(info)[0],
                        f"exclusive service {service} still has an active or restartable container")

    def capture_rollback_incumbents(self) -> None:
        for item in list_owned_containers(self.backend, self.labels):
            info = self.owned("containers", item["Id"])
            state = info.get("State") or {}
            started_at = state.get("StartedAt")
            if state.get("Status") == "running" and isinstance(started_at, str):
                self.rollback_incumbents[item["Id"]] = started_at

    def verify_rollback_commit(self) -> None:
        if not self.exclusive_services:
            return
        active_counts = {svc: 0 for svc in self.exclusive_services}
        verified_ids: list[str] = []
        for item in list_owned_containers(self.backend, self.labels):
            cid = item.get("Id")
            require(isinstance(cid, str) and bool(cid), "invalid owned container identity")
            info = self.owned("containers", cid)
            require(info.get("Id") == cid, "rollback container identity changed during inspection")
            active = container_active(info)
            exclusive = False
            for svc in self.exclusive_services:
                matches, conflict = container_matches_service(info, self.prefix, svc, self.labels)
                require(not conflict, "ambiguous exclusive service identity")
                exclusive |= matches
                if matches and not container_exited_and_non_restartable(info)[0]:
                    require(active, f"exclusive service {svc} has a stopped restartable container")
                    active_counts[svc] += 1
                    require(active_counts[svc] <= 1, f"duplicate active container for exclusive service {svc}")
            if active:
                cstate = info.get("State") or {}
                if (not exclusive and cid in self.rollback_incumbents
                        and cstate.get("StartedAt") == self.rollback_incumbents[cid]):
                    continue
                require(self._container_matches_approved_image(info), "active rollback container does not match approved artifact")
                require(cstate.get("Status") == "running" and cstate.get("Running") is True
                        and cstate.get("Restarting") is False and cstate.get("Paused") is False
                        and type(cstate.get("Pid")) is int and cstate["Pid"] > 0,
                        "active rollback container is not running")
                health = cstate.get("Health")
                if isinstance(health, dict) and health.get("Status") == "starting":
                    raise RollbackHealthStarting("Docker health check is starting")
                check = (info.get("Config") or {}).get("Healthcheck") or {}
                if health is not None or check.get("Test") not in (None, [], ["NONE"]):
                    require(isinstance(health, dict) and health.get("Status") == "healthy",
                            "active rollback container health is unverified")
                verified_ids.append(cid)
        for svc in self.required_rollback_services:
            require(active_counts[svc] == 1, f"exclusive rollback service {svc} is missing")
        self.verified_containers = verified_ids

    def artifacts(self) -> dict:
        if self.artifact_store is None or not self.artifact_store.exists():
            return {}
        uid = os.getuid() if os.environ.get("CI_FLEET_DEPLOYER_TESTING") == "1" else 0
        return decode(protected_file(self.artifact_store, uid))

    def record_artifact(self) -> None:
        if self.artifact_store is not None:
            records = self.artifacts()
            records[self.image] = self.labels
            durable_file(self.artifact_store, json.dumps(records, sort_keys=True).encode())

    def artifact_owned(self, image: str) -> bool:
        return bool(IMAGE.fullmatch(image)) and self.artifacts().get(image) == self.labels

    def owned(self, kind: str, name: str) -> dict:
        require(bool(re.fullmatch(r"[A-Za-z0-9_.-]{1,255}", name)), "invalid resource identity")
        value = self.backend.inspect(kind, name)
        config = value.get("Config")
        labels = config.get("Labels") if kind == "containers" and isinstance(config, dict) else value.get("Labels")
        require(isinstance(labels, dict) and all(labels.get(k) == v for k, v in self.labels.items()),
                "resource belongs to another deployment target")
        return value

    def named(self, value: str) -> None:
        require(value.startswith(self.prefix) and bool(re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,127}", value)),
                "resource name must be scoped to the deployer identity")

    def labels_match(self, value: dict) -> None:
        labels = value.get("Labels")
        require(isinstance(labels, dict) and all(labels.get(k) == v for k, v in self.labels.items()),
                "deployment resource labels are required")

    def mounts(self, host: dict) -> None:
        require(not host.get("VolumesFrom"), "inherited mounts are forbidden")
        for binding in host.get("Binds") or []:
            require(isinstance(binding, str), "invalid bind")
            pieces = binding.split(":")
            require(len(pieces) in (2, 3) and (len(pieces) == 2 or pieces[2] in ("ro", "rw")), "unsupported bind options")
            source, target = pieces[:2]
            self.mounts({"Mounts": [{"Type": "bind" if source.startswith("/") else "volume",
                                     "Source": source, "Target": target, "ReadOnly": len(pieces) == 3 and pieces[2] == "ro"}]})
        for mount in host.get("Mounts", []):
            require(isinstance(mount, dict) and set(mount) <= {"Type", "Source", "Target", "ReadOnly"},
                    "mount options are forbidden")
            target = mount.get("Target", "")
            require(isinstance(target, str) and target.startswith("/") and os.path.normpath(target) == target,
                    "mount target is not canonical")
            if mount.get("Type") == "volume":
                volume = self.owned("volumes", mount.get("Source", ""))
                require(volume.get("Driver") == "local" and not volume.get("Options"), "unsafe volume driver")
            elif mount.get("Type") == "bind":
                require(bool(self.credential) and mount.get("Source") == self.credential
                        and mount.get("ReadOnly") is True and target.startswith("/run/secrets/"),
                        "only the approved credential may be bound read-only")
            else:
                raise ValueError("only scoped volumes and the approved credential are permitted")

    def container(self, value: dict) -> None:
        allowed = {"Image", "Cmd", "Entrypoint", "Env", "Labels", "User", "WorkingDir", "ExposedPorts",
                   "HostConfig", "NetworkingConfig", "Healthcheck", "StopSignal", "StopTimeout",
                   "AttachStdin", "AttachStdout", "AttachStderr", "OpenStdin", "StdinOnce", "Tty"}
        neutral = {"Hostname": "", "Domainname": "", "NetworkDisabled": False, "MacAddress": "", "Volumes": None, "OnBuild": None, "Shell": None}
        value = dict(value)
        for key, default in neutral.items():
            if key in value and value[key] == default:
                value.pop(key)
        require(set(value) <= allowed and value.get("Image") == self.image,
                "container must use the approved immutable artifact")
        self.labels_match(value)
        host = value.get("HostConfig", {})
        host_keys = {"Mounts", "Binds", "VolumesFrom", "Privileged", "CapAdd", "CapDrop", "ReadonlyRootfs",
                     "SecurityOpt", "NetworkMode", "PidMode", "IpcMode", "UTSMode", "UsernsMode",
                     "CgroupnsMode", "Memory", "MemorySwap", "NanoCpus", "PidsLimit", "PortBindings",
                     "RestartPolicy", "AutoRemove", "Init", "Tmpfs", "GroupAdd"}
        require(isinstance(host, dict), "invalid host configuration")
        host = dict(host)
        neutral = {"ContainerIDFile": "", "Links": None, "ConsoleSize": [0, 0], "OomKillDisable": None,
                   "OomScoreAdj": 0, "PublishAllPorts": False, "Devices": None, "DeviceRequests": None,
                   "DeviceCgroupRules": None, "Sysctls": None, "ExtraHosts": None, "GroupAdd": None,
                   "Dns": None, "DnsOptions": None, "DnsSearch": None, "Cgroup": "", "CgroupParent": "",
                   "StorageOpt": None, "VolumeDriver": "", "CpuShares": 0, "CpuPeriod": 0, "CpuQuota": 0,
                   "CpuRealtimePeriod": 0, "CpuRealtimeRuntime": 0, "CpusetCpus": "", "CpusetMems": "",
                   "MemoryReservation": 0, "MemorySwappiness": None, "BlkioWeight": 0, "BlkioWeightDevice": None,
                   "BlkioDeviceReadBps": None, "BlkioDeviceWriteBps": None, "BlkioDeviceReadIOps": None,
                   "BlkioDeviceWriteIOps": None, "Ulimits": None, "Isolation": "", "MaskedPaths": None,
                   "ReadonlyPaths": None, "ShmSize": 67108864, "Runtime": "runc",
                   "LogConfig": {"Type": "", "Config": {}}}
        for key, default in neutral.items():
            if key in host and host[key] == default:
                host.pop(key)
        require(set(host) <= host_keys, "unsupported container host configuration")
        require(host.get("GroupAdd") in (None, [], ["0"]), "only the rootless credential group may be added")
        require(host.get("Privileged", False) is False and not host.get("CapAdd")
                and host.get("CapDrop") == ["ALL"] and host.get("ReadonlyRootfs") is True
                and security_options(host.get("SecurityOpt")),
                "container privilege restrictions are required")
        require(all(host.get(k, "") in ("", "private") for k in ("PidMode", "IpcMode", "UTSMode", "UsernsMode"))
                and host.get("CgroupnsMode") == "private", "shared host namespaces are forbidden")
        require(all(type(host.get(k)) is int and 0 < host[k] <= limit for k, limit in CONTAINER_LIMITS.items()),
                "memory, CPU and PID limits must fit the reviewed containment ceilings")
        swap = host.get("MemorySwap", 0)
        require(type(swap) is int and (swap == 0 or host["Memory"] <= swap <= 2 * host["Memory"]), "unbounded swap is forbidden")
        require(not host.get("Tmpfs"), "unbounded tmpfs is forbidden; use scoped volumes")
        network = self.owned("networks", host.get("NetworkMode", ""))
        require(network.get("Driver") == "bridge" and network.get("Internal") is True,
                "a scoped internal bridge is required")
        networking = value.get("NetworkingConfig") or {}
        require(isinstance(networking, dict) and set(networking) <= {"EndpointsConfig"}, "invalid networking configuration")
        endpoints = networking.get("EndpointsConfig") or {}
        require(isinstance(endpoints, dict) and set(endpoints) <= {host["NetworkMode"]}, "alternate network attachments are forbidden")
        for endpoint in endpoints.values():
            require(isinstance(endpoint, dict) and all(v in (None, "", [], {}) for v in endpoint.values())
                    and set(endpoint) <= {"IPAMConfig", "Links", "Aliases", "NetworkID", "EndpointID", "Gateway", "IPAddress",
                                         "IPPrefixLen", "IPv6Gateway", "GlobalIPv6Address", "GlobalIPv6PrefixLen", "MacAddress", "DriverOpts"},
                    "custom network endpoint settings are forbidden")
        for bindings in host.get("PortBindings", {}).values():
            for binding in bindings:
                require(binding.get("HostIp") == "127.0.0.1" and str(binding.get("HostPort", "")).isdigit()
                        and 1024 <= int(binding["HostPort"]) <= 65535, "only loopback application ports are allowed")
        self.mounts(host)
        image = self.backend.inspect("images", self.image)
        config = image.get("Config")
        require(isinstance(config, dict), "approved image configuration is unavailable")
        if len(host["SecurityOpt"]) == 2:
            user = value.get("User", "")
            if user == "":
                user = config.get("User", "")
            user_id = user.split(":")[0] if isinstance(user, str) else ""
            require(user_id not in ("", "root")
                    and (not re.fullmatch(r"[+-]?[0-9]+", user_id) or int(user_id) > 0),
                    "the Chromium profile requires a declared non-root container user")
        declared = config.get("Volumes") or {}
        require(isinstance(declared, dict), "invalid image volume declarations")
        mounted = {mount["Target"] for mount in host.get("Mounts", []) if mount["Type"] == "volume"}
        mounted.update(binding.split(":")[1] for binding in host.get("Binds") or [] if not binding.startswith("/"))
        require(set(declared) <= mounted, "image volumes must use pre-created scoped labelled volumes")

    def authorize(self, method: str, url: str, body: bytes) -> tuple[str, bytes]:
        parsed = urllib.parse.urlsplit(url)
        require(not parsed.scheme and not parsed.netloc and not parsed.fragment, "absolute Docker URLs are forbidden")
        path = re.sub(r"^/v[0-9]+\.[0-9]+(?=/)", "", urllib.parse.unquote(parsed.path))
        require("%" not in path and os.path.normpath(path) == path, "Docker path is not canonical")
        require(self.operation not in ("validate", "health") or method in ("GET", "HEAD"), "read-only adapter operation")
        require(self.operation != "cleanup" or method in ("GET", "HEAD", "DELETE"), "cleanup may only inspect and delete scoped resources")
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        require(all(len(v) == 1 for v in query.values()), "duplicate query arguments are forbidden")
        if method in ("GET", "HEAD") and path in ("/_ping", "/version", "/info"):
            require(not body and not query, "metadata query arguments are forbidden")
            return path, body
        if method == "POST" and path == "/images/create":
            image = query.get("fromImage", [""])[0]
            tag = query.get("tag", [""])[0]
            if tag:
                image += "@" + tag
            require(not body and set(query) <= {"fromImage", "tag"} and image == self.image,
                    "only the approved artifact may be pulled")
            return path + "?" + urllib.parse.urlencode(query, doseq=True), body
        if method == "GET" and path == "/images/json":
            require(not body and set(query) <= {"all", "filters", "digests"}, "unsupported image inventory query")
            return "/images/json?all=1", body
        if method == "DELETE" and path.startswith("/images/"):
            image = path[len("/images/"):]
            require(self.operation == "cleanup" and self.artifact_owned(image) and image not in self.protected_images
                    and not body and set(query) <= {"force", "noprune"} and query.get("force", ["false"])[0] in ("false", "0"),
                    "only unused retired target artifacts may be removed")
            return path + "?force=false&noprune=true", body
        if method == "GET" and path.startswith("/images/") and path.endswith("/json"):
            require(path[8:-5] == self.image and not query, "only the approved image may be inspected")
            return path, body
        if method == "GET" and path in ("/containers/json", "/networks", "/volumes"):
            require(set(query) <= {"all", "filters"}, "unsupported inventory query")
            filters = decode(query.get("filters", ["{}"]) [0].encode())
            require(set(filters) <= {"label", "name", "id", "status"}, "unsupported inventory filters")
            filters["label"] = [f"{k}={v}" for k, v in self.labels.items()]
            query["filters"] = [json.dumps(filters, separators=(",", ":"))]
            return path + "?" + urllib.parse.urlencode(query, doseq=True), body
        if method == "POST" and path == "/containers/create":
            require(set(query) == {"name"}, "a scoped container name is required")
            self.named(query["name"][0])
            payload = decode(body)
            self.container(payload)
            if self.operation == "rollback" and self.exclusive_services:
                candidate = {"Names": ["/" + query["name"][0]], "Labels": payload.get("Labels")}
                for svc in self.exclusive_services:
                    matches, conflict = container_matches_service(candidate, self.prefix, svc, self.labels)
                    require(not conflict, "ambiguous exclusive service identity")
                    if matches:
                        self._require_no_active_service_container(svc)
            return path + "?" + urllib.parse.urlencode(query, doseq=True), body
        if method == "POST" and path in ("/networks/create", "/volumes/create"):
            require(not query, "create query arguments are forbidden")
            value = decode(body)
            self.named(value.get("Name", ""))
            self.labels_match(value)
            if path.startswith("/networks"):
                require(set(value) <= {"Name", "Labels", "Driver", "Internal", "CheckDuplicate"}
                        and value.get("Driver", "bridge") == "bridge" and value.get("Internal") is True,
                        "only scoped internal bridge networks may be created")
            else:
                require(set(value) <= {"Name", "Labels", "Driver", "DriverOpts"}
                        and value.get("Driver", "local") == "local" and not value.get("DriverOpts"),
                        "only scoped local volumes without options may be created")
            return path, body
        match = re.fullmatch(r"/(containers|networks|volumes)/([^/]+)(?:/(json|start|stop|restart|kill|wait|logs))?", path)
        require(match is not None, "Docker endpoint is outside the deployment contract")
        kind, identity, action = match.groups()
        info = self.owned(kind, identity)
        allowed = ((method == "GET" and action in (None, "json", "logs"))
                   or (method == "DELETE" and action is None)
                   or (method == "POST" and kind == "containers" and action in ("start", "stop", "restart", "kill", "wait")))
        require(allowed and not body and set(query) <= {"t", "signal", "force", "v", "stdout", "stderr", "tail"},
                "resource operation is outside the deployment contract")
        if self.operation == "rollback" and self.exclusive_services and method == "POST" and kind == "containers" and action in ("start", "restart"):
            require(self._container_matches_approved_image(info), "rollback container must use the approved last-known-good artifact")
            for svc in self.exclusive_services:
                matches, conflict = container_matches_service(info, self.prefix, svc, self.labels)
                require(not conflict, "ambiguous exclusive service identity")
                if matches:
                    self._require_no_active_service_container(svc, ignore_id=info.get("Id"))
        return path + ("?" + urllib.parse.urlencode(query, doseq=True) if query else ""), body


class ProxyServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = False

    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        self.slots.acquire()
        request.settimeout(10)
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def executor_peer(connection: socket.socket, unit: str | None) -> bool:
    if unit is None or not UNIT.fullmatch(unit):
        return False
    try:
        pid, _uid, _gid = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        result = subprocess.run(["systemctl", "show", unit, "--property=MainPID", "--value"],
                                capture_output=True, text=True, timeout=10)
        return result.returncode == 0 and pid > 0 and result.stdout.strip() == str(pid)
    except (OSError, ValueError, struct.error, subprocess.SubprocessError):
        return False


def handler(policy: Policy):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def dispatch(self):
            mutation = False
            try:
                require(not self.headers.get("Transfer-Encoding"), "chunked requests are forbidden")
                length = int(self.headers.get("Content-Length", "0"))
                require(0 <= length <= MAX_REQUEST, "request exceeded the bound")
                body = self.rfile.read(length)
                req_norm = os.path.normpath(re.sub(r"^/v[0-9]+\.[0-9]+(?=/)", "",
                                                   urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)))
                exclusive_rollback = policy.operation == "rollback" and bool(policy.exclusive_services)
                read_only_wait = exclusive_rollback and self.command == "POST" and re.fullmatch(r"/containers/[^/]+/wait", req_norm)
                serialize_exclusive = exclusive_rollback and self.command in ("POST", "DELETE") and not read_only_wait
                lock = policy._exclusive_lock if serialize_exclusive else contextlib.nullcontext()
                with lock:
                    if serialize_exclusive:
                        require(not policy.uncertain, "Docker mutation completion is unverified")
                        require(policy.rollback_outcome is None or self.path == "/ci-fleet/rollback-commit",
                                "rollback outcome has already been published")
                    if self.command == "POST" and self.path == "/ci-fleet/rollback-commit":
                        require(executor_peer(self.connection, policy.unit), "rollback outcome requires the executor")
                        require(policy.operation == "rollback" and policy.marker is not None
                                and body in (b"committed", b"failed"), "rollback outcome is not authorized")
                        outcome = body.decode()
                        require(policy.rollback_outcome in (None, outcome), "rollback outcome conflicts")
                        if outcome == "committed":
                            if policy.rollback_ready is not None:
                                mutation = True
                                durable_file(policy.rollback_ready, json.dumps({
                                    "marker": str(policy.marker),
                                    "services": policy.exclusive_services,
                                    "incumbents": policy.rollback_incumbents,
                                }, sort_keys=True).encode())
                            else:
                                policy.verify_rollback_commit()
                                mutation = True
                                durable_file(policy.marker, b"committed\n")
                        else:
                            require(not policy.marker.exists() and not policy.marker.is_symlink(),
                                    "failed rollback conflicts with its commit marker")
                        policy.rollback_outcome = outcome
                        status, response = 204, b""
                    else:
                        path, body = policy.authorize(self.command, self.path, body)
                        headers = {"Content-Type": "application/json"}
                        mutation = self.command not in ("GET", "HEAD") and not read_only_wait
                        status, response = policy.backend.request(self.command, path, body, headers)
                        if self.command == "POST" and path.startswith("/images/create?") and 200 <= status < 300:
                            progress = [decode(line) for line in response.splitlines() if line.strip()]
                            require(bool(progress), "image pull completion is unavailable")
                            if not any(item.get("error") or item.get("errorDetail") for item in progress):
                                image = policy.backend.inspect("images", policy.image)
                                require(policy.image in (image.get("RepoDigests") or []), "pulled artifact digest is unverified")
                                policy.record_artifact()
                        if self.command == "GET" and path.startswith("/images/json?") and status == 200:
                            inventory = json.loads(response, object_pairs_hook=unique_object)
                            require(isinstance(inventory, list), "invalid image inventory")
                            owned = {image for image, labels in policy.artifacts().items() if labels == policy.labels}
                            scoped = []
                            for item in inventory:
                                require(isinstance(item, dict), "invalid image inventory entry")
                                digests = [digest for digest in item.get("RepoDigests") or [] if digest in owned]
                                if digests:
                                    item["RepoDigests"], item["RepoTags"] = digests, []
                                    scoped.append(item)
                            response = json.dumps(scoped).encode()
                        if self.command == "DELETE" and path.startswith("/images/") and 200 <= status < 300 and policy.artifact_store is not None:
                            records = policy.artifacts()
                            records.pop(path.split("?", 1)[0][len("/images/"):], None)
                            durable_file(policy.artifact_store, json.dumps(records, sort_keys=True).encode())
                        if mutation and status >= 500:
                            policy.uncertain = True
                            if policy.uncertain_marker is not None:
                                durable_file(policy.uncertain_marker, b"uncertain\n")
            except (ValueError, KeyError, TypeError, AttributeError, OSError, json.JSONDecodeError, http.client.HTTPException):
                if mutation:
                    policy.uncertain = True
                    if policy.uncertain_marker is not None:
                        durable_file(policy.uncertain_marker, b"uncertain\n")
                status, response = 403, b'{"message":"deployment Docker policy rejected the request"}'
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        do_GET = do_HEAD = do_POST = do_DELETE = dispatch
    return Handler


def durable_file(path: Path, body: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def consume_approval(store: Path, line: str | None = None, uid: int = 0) -> None:
    pending = store / "pending-consumption.json"
    if line is None and not pending.exists() and not pending.is_symlink():
        return
    ledger = store / "consumed-requests"
    for directory in (store, ledger):
        metadata = directory.lstat()
        require(directory.resolve() == directory and stat.S_ISDIR(metadata.st_mode)
                and metadata.st_uid == uid and stat.S_IMODE(metadata.st_mode) == 0o700,
                "consumption directory is unsafe")
    audit = store / "audit.log"
    descriptor = os.open(audit, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        metadata = os.fstat(descriptor)
        require(stat.S_ISREG(metadata.st_mode) and metadata.st_uid == uid and metadata.st_nlink == 1
                and stat.S_IMODE(metadata.st_mode) == 0o600, "consumption audit is unsafe")
        if line is not None:
            require(not pending.exists() and not pending.is_symlink(), "pending consumption requires recovery")
            record = {"schema_version": 1, "audit_offset": metadata.st_size, "line": line}
        else:
            record = decode(protected_file(pending, uid))
        require(set(record) == {"schema_version", "audit_offset", "line"}
                and type(record["schema_version"]) is int and record["schema_version"] == 1
                and type(record["audit_offset"]) is int and 0 <= record["audit_offset"] <= metadata.st_size,
                "pending consumption record is invalid")
        entry = record["line"]
        require(isinstance(entry, str) and entry.isascii() and len(entry) <= 4096
                and entry.endswith("\n") and entry.count("\n") == 1, "pending consumption audit is invalid")
        parts = [item.partition("=") for item in entry[:-1].split(" ")]
        values = {key: value for key, separator, value in parts if separator}
        required = {"time", "environment", "target", "source", "artifact", "approval", "approver", "policy",
                    "checkpoint", "authorized_by", "gate", "result", "phase", "status"}
        require(len(parts) == len(values) and set(values) == required
                and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", values["time"])
                and re.fullmatch(r"[a-z][a-z0-9-]{0,31}", values["environment"])
                and re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", values["target"])
                and re.fullmatch(r"[0-9a-f]{40}", values["source"])
                and re.fullmatch(r"sha256:[0-9a-f]{64}", values["artifact"])
                and all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}", values[key])
                        for key in ("approval", "approver", "policy", "checkpoint", "authorized_by", "gate"))
                and (values["result"], values["phase"], values["status"]) == ("consumed", "pre-adapter", "none"),
                "pending consumption identities are invalid")
        marker = ledger / ("approval-" + hashlib.sha256(values["approval"].encode()).hexdigest())
        if line is not None:
            require(not marker.exists() and not marker.is_symlink(), "approval is already consumed")
            durable_file(pending, json.dumps(record, sort_keys=True).encode())
        elif marker.exists() or marker.is_symlink():
            require(protected_file(marker, uid) == b"", "consumption marker is unsafe")
        durable_file(marker, b"")
        body = entry.encode()
        offset = record["audit_offset"]
        os.lseek(descriptor, offset, os.SEEK_SET)
        retained = os.read(descriptor, len(body))
        if retained != body:
            require(len(retained) < len(body) and body.startswith(retained)
                    and metadata.st_size == offset + len(retained), "pending consumption audit conflicts")
            os.ftruncate(descriptor, offset)
            os.lseek(descriptor, offset, os.SEEK_SET)
            written = 0
            while written < len(body):
                count = os.write(descriptor, body[written:])
                require(count > 0, "consumption audit write failed")
                written += count
        os.fsync(descriptor)
        pending.unlink()
        directory = os.open(store, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        os.close(descriptor)


def trusted_directory(path: Path, uid: int, mode: int) -> None:
    require(path.is_absolute() and path.resolve() == path and path.is_dir()
            and path.stat().st_uid == uid and stat.S_IMODE(path.stat().st_mode) == mode,
            "protected directory is unsafe")
    for parent in path.parents:
        metadata = parent.stat()
        require(metadata.st_uid == uid and not metadata.st_mode & 0o022, "protected directory parent is unsafe")


def capture_deployed(state: Path, journal: Path, uid: int) -> None:
    pointer = state / "deployed/current"
    if not pointer.exists() and not pointer.is_symlink():
        return
    require(pointer.is_symlink(), "deployed pointer is unsafe")
    target = os.readlink(pointer)
    require(bool(re.fullmatch(r"\.snapshot\.[A-Za-z0-9._-]+", target)), "deployed pointer is unsafe")
    source = pointer.parent / target
    trusted_directory(source, uid, 0o700)
    destination = journal / "deployed"
    destination.mkdir(mode=0o700)
    for name in ("state.json", "policy.conf"):
        durable_file(destination / name, protected_file(source / name, uid))
    durable_file(journal / "deployed-pointer", target.encode())


def unit_inactive(unit: str) -> bool:
    result = subprocess.run(["systemctl", "show", unit, "--property=LoadState", "--property=ActiveState"],
                            capture_output=True, text=True, timeout=15)
    lines = result.stdout.splitlines()
    if len(lines) != 2 or any("=" not in line for line in lines):
        return False
    state = dict(line.split("=", 1) for line in lines)
    return (state.get("LoadState") in ("loaded", "not-found") and state.get("ActiveState") in ("inactive", "failed")
            and (result.returncode == 0 or result.returncode == 1 and state == {"LoadState": "not-found", "ActiveState": "inactive"}))


def journal_complete(store: Path, unit: str, uid: int = 0) -> bool:
    try:
        require(bool(UNIT.fullmatch(unit)), "invalid adapter unit")
        journal = store / unit
        trusted_directory(journal, uid, 0o700)
        value = decode(protected_file(journal / "status.json", uid))
        uncertain = journal / "docker-uncertain"
        if value.get("operation") == "rollback":
            outcome, marker = value.get("rollback_outcome"), value.get("rollback_marker")
            require(outcome in ("committed", "failed") and isinstance(marker, str), "rollback outcome is unverified")
            marker = Path(marker)
            require(marker.is_absolute() and marker.resolve() == marker, "rollback marker path is unsafe")
            if outcome == "committed":
                require(protected_file(marker, uid) == b"committed\n"
                        and stat.S_IMODE(marker.stat().st_mode) == 0o600, "rollback commit marker is unverified")
            else:
                require(value.get("exit_code") != 0 and not marker.exists() and not marker.is_symlink(),
                        "failed rollback outcome is unverified")
        return (value.get("unit") == unit and value.get("status") == "completed"
                and type(value.get("exit_code")) is int
                and value.get("operation") in ("validate", "health", "cleanup", "deploy", "rollback")
                and (value["exit_code"] == 0 or value["operation"] in ("validate", "health", "rollback"))
                and not uncertain.exists() and not uncertain.is_symlink())
    except (ValueError, KeyError, OSError):
        return False


def retire_journal(store: Path, unit: str, uid: int = 0, *, inactive_health: bool = False) -> None:
    journal = store / unit
    if inactive_health:
        require(bool(UNIT.fullmatch(unit)) and unit_inactive(unit), "health unit termination is unverified")
        if not os.path.lexists(journal):
            return
        trusted_directory(journal, uid, 0o700)
        status_path = journal / "status.json"
        if os.path.lexists(status_path):
            status = decode(protected_file(status_path, uid))
            require(status.get("unit") == unit and status.get("operation") == "health", "health journal operation is unverified")
    else:
        require(journal_complete(store, unit, uid), "journal completion is unverified")
    shutil.rmtree(journal)
    descriptor = os.open(store, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def materialize_credential(store: Path, values: dict, body: bytes, owner: int, daemon_uid: int, daemon_gid: int) -> Path:
    reject_acl(store)
    identity = json.dumps([values[key] for key in ("DEPLOYER_IDENTITY", "ENVIRONMENT", "TARGET_ID")]
                          + [daemon_uid, daemon_gid], separators=(",", ":")).encode()
    key = hashlib.sha256(identity + b"\0" + body).hexdigest()
    directory = store / key
    if not directory.exists() and not directory.is_symlink():
        directory.mkdir(mode=0o700)
        os.chown(directory, owner, daemon_gid)
        directory.chmod(0o710)
    metadata = directory.lstat()
    require(stat.S_ISDIR(metadata.st_mode) and metadata.st_uid == owner and metadata.st_gid == daemon_gid
            and stat.S_IMODE(metadata.st_mode) == 0o710, "credential directory is unsafe")
    reject_acl(directory)
    secret = directory / "application"
    try:
        descriptor = os.open(secret, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        descriptor = os.open(secret, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            reject_acl(stream.fileno())
            metadata = os.fstat(stream.fileno())
            require(stat.S_ISREG(metadata.st_mode) and metadata.st_uid == owner and metadata.st_gid == daemon_gid
                    and metadata.st_nlink == 1 and stat.S_IMODE(metadata.st_mode) == 0o440
                    and stream.read(MAX_REQUEST + 1) == body, "retained credential changed")
    else:
        with os.fdopen(descriptor, "wb") as stream:
            reject_acl(stream.fileno())
            os.fchown(stream.fileno(), owner, daemon_gid)
            os.fchmod(stream.fileno(), 0o440)
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
    for parent in (directory, store, store.parent):
        descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return secret


def execute_adapter(operation: str) -> int:
    # The adapter must not borrow its parent's PID-authenticated socket through /proc or ptrace.
    require(ctypes.CDLL(None).prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) == 0, "executor process protection failed")
    credentials = Path(os.environ["CREDENTIALS_DIRECTORY"])
    runtime = Path(os.environ["RUNTIME_DIRECTORY"])
    values = parse_policy((credentials / "policy").read_bytes())
    values["CREDENTIAL_REF"] = str(credentials / "application")
    values["ADAPTER_PATH"] = str(runtime / "adapter")
    policy = runtime / "policy.conf"
    policy.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
    policy.chmod(0o400)
    adapter = runtime / "adapter"
    shutil.copyfile(credentials / "adapter", adapter)
    adapter.chmod(0o500)
    environment = {"PATH": "/usr/bin:/bin", "HOME": str(runtime), "DOCKER_CONFIG": str(runtime / "docker-config"),
                   "DOCKER_HOST": "unix://" + str(runtime / "docker.sock"), "CI_FLEET_DEPLOYER_CONFIG": str(policy),
                   "CI_FLEET_DEPLOYER_CONTAINER_CREDENTIAL": os.environ["CI_FLEET_DEPLOYER_CONTAINER_CREDENTIAL"]}
    (runtime / "docker-config").mkdir(mode=0o700)
    if (credentials / "request").exists():
        environment["CI_FLEET_DEPLOYER_REQUEST"] = str(credentials / "request")
    commit = runtime / "rollback-commit"
    if os.environ.get("CI_FLEET_DEPLOYER_COMMIT_ALLOWED") == "1":
        environment["CI_FLEET_DEPLOYER_ROLLBACK_COMMIT"] = str(commit)
    status = subprocess.run([str(adapter), operation], env=environment, stdin=subprocess.DEVNULL, close_fds=True).returncode
    if "CI_FLEET_DEPLOYER_ROLLBACK_COMMIT" in environment:
        if commit.exists() or commit.is_symlink():
            require(status == 0, "rollback commitment requires successful adapter completion")
            require(stat.S_ISREG(commit.lstat().st_mode) and not commit.is_symlink(), "unsafe rollback marker")
            outcome = b"committed"
        else:
            require(status > 0, "rollback outcome is unverified")
            outcome = b"failed"
        connection = UnixConnection(str(runtime / "docker.sock"), rootless=False)
        try:
            connection.request("POST", "/ci-fleet/rollback-commit", outcome)
            require(connection.getresponse().status == 204, "rollback outcome publication failed")
        finally:
            connection.close()
    return status


def sandbox_command(unit: str, seconds: int, proxy: Path, policy: Path, adapter: Path,
                    credential: Path, helper: Path, request: Path | None, commit: bool, operation: str, backend_socket: str, container_credential: str) -> list[str]:
    for path in (proxy, policy, adapter, credential, helper, request, backend_socket, container_credential):
        require(path is None or bool(re.fullmatch(r"/[A-Za-z0-9._/-]+", str(path))), "unsafe systemd path")
    directory = unit.removesuffix(".service")
    command = ["systemd-run", "--quiet", "--wait", "--pipe", "--collect", "--service-type=exec", "--unit=" + unit,
               "--property=DynamicUser=yes", "--property=NoNewPrivileges=yes", "--property=CapabilityBoundingSet=",
               "--property=AmbientCapabilities=", "--property=ProtectSystem=strict", "--property=ProtectHome=yes",
               "--property=PrivateDevices=yes", "--property=PrivateNetwork=yes", "--property=PrivateTmp=yes",
               "--property=ProtectKernelTunables=yes", "--property=ProtectKernelModules=yes",
               "--property=ProtectControlGroups=yes", "--property=RestrictNamespaces=yes",
               "--property=RestrictSUIDSGID=yes", "--property=LockPersonality=yes",
               "--property=InaccessiblePaths=-/run/docker.sock -/var/run/docker.sock -/run/user -/run/systemd/private -/run/dbus/system_bus_socket -" + backend_socket,
               "--property=RuntimeDirectory=" + directory, "--property=RuntimeDirectoryMode=0700",
               "--property=BindReadOnlyPaths=" + str(proxy) + ":/run/" + directory + "/docker.sock",
               "--property=LoadCredential=policy:" + str(policy), "--property=LoadCredential=adapter:" + str(adapter),
               "--property=LoadCredential=application:" + str(credential),
               "--property=LoadCredential=executor:" + str(helper),
               "--property=RuntimeMaxSec=" + str(seconds) + "s", "--property=TimeoutStopSec=10s",
               "--property=KillMode=control-group", "--property=MemoryMax=256M", "--property=TasksMax=32",
               "--setenv=CI_FLEET_DEPLOYER_COMMIT_ALLOWED=" + str(int(commit)),
               "--setenv=CI_FLEET_DEPLOYER_CONTAINER_CREDENTIAL=" + container_credential]
    if request is not None:
        command.append("--property=LoadCredential=request:" + str(request))
    return command + ["/usr/bin/python3", "%d/executor", "execute", operation]


def append_audit_record(audit_log: Path, fields: dict[str, str], uid: int = 0, *, once: bool = False) -> None:
    parent = audit_log.parent
    if not parent.exists() and not parent.is_symlink():
        parent.mkdir(mode=0o700)
    pmeta = parent.lstat()
    require(parent.resolve() == parent and stat.S_ISDIR(pmeta.st_mode)
            and pmeta.st_uid == uid and stat.S_IMODE(pmeta.st_mode) == 0o700,
            "audit directory is unsafe")
    parts = []
    for k, v in fields.items():
        val = str(v)
        require(bool(k) and bool(val) and not any(c.isspace() for c in k) and not any(c.isspace() for c in val),
                "invalid audit field")
        parts.append(f"{k}={val}")
    line = " ".join(parts) + "\n"
    require(line.isascii() and len(line) <= 4096, "audit entry is invalid")
    body = line.encode()
    access = os.O_RDWR if once else os.O_WRONLY
    descriptor = os.open(audit_log, access | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        metadata = os.fstat(descriptor)
        require(stat.S_ISREG(metadata.st_mode) and metadata.st_uid == uid and metadata.st_nlink == 1
                and stat.S_IMODE(metadata.st_mode) == 0o600, "audit log is unsafe")
        if once:
            require("transaction" in fields and fields.get("result") == "success", "missing terminal audit identity")
            transaction = f"transaction={fields['transaction']}".encode()
            with os.fdopen(os.dup(descriptor), "rb") as prior:
                for line in prior:
                    if line.endswith(b"\n") and {transaction, b"result=success"}.issubset(line.split()):
                        return
        written = 0
        while written < len(body):
            count = os.write(descriptor, body[written:])
            require(count > 0, "audit write failed")
            written += count
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    dir_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _service_has_active_container(backend, prefix: str, service: str, expected_labels: dict[str, str]) -> bool:
    for item in list_owned_containers(backend, expected_labels):
        matches, conflict = container_matches_service(item, prefix, service, expected_labels)
        if conflict:
            return True
        if matches:
            info = backend.inspect("containers", item["Id"])
            matches, conflict = container_matches_service(info, prefix, service, expected_labels)
            if conflict or not matches or info.get("Id") != item["Id"] or not container_exited_and_non_restartable(info)[0]:
                return True
    return False

def _wait_container_exited(backend, cid: str, prefix: str, service: str, expected_labels: dict[str, str],
                           deadline: float, start: float, monotonic, sleep, retry_signal: str | None = None) -> tuple[bool, float, str]:
    delay = 0.05
    quoted = urllib.parse.quote(cid, safe="")
    while True:
        now = monotonic()
        if now >= deadline:
            return False, round(now - start, 3), "timeout"
        try:
            status, body = backend.request("GET", f"/containers/{quoted}/json")
        except TimeoutError:
            return False, round(monotonic() - start, 3), "stop_deadline_unverified"
        now = monotonic()
        if now >= deadline:
            return False, round(now - start, 3), "timeout"
        if status == 404:
            active = _service_has_active_container(backend, prefix, service, expected_labels)
            elapsed = round(monotonic() - start, 3)
            if monotonic() >= deadline:
                return False, elapsed, "stop_deadline_unverified"
            return not active, elapsed, "unexpected_replacement_detected" if active else "removed"
        if status != 200:
            return False, round(monotonic() - start, 3), "container_inspect_failed"
        info = decode(body)
        matches, conflict = container_matches_service(info, prefix, service, expected_labels)
        if conflict or not matches or info.get("Id") != cid:
            return False, round(monotonic() - start, 3), "ownership_changed"
        if (info.get("State") or {}).get("Paused") is True:
            return False, round(monotonic() - start, 3), "container_paused"
        exited, _ = container_exited_and_non_restartable(info)
        if exited:
            return True, round(monotonic() - start, 3), "exited"
        now = monotonic()
        if now >= deadline:
            return False, round(now - start, 3), "timeout"
        cstate = info.get("State") or {}
        if retry_signal and cstate.get("Status") == "running":
            signal_status, _ = backend.request("POST", f"/containers/{quoted}/kill?" + urllib.parse.urlencode({"signal": retry_signal}))
            if signal_status in (200, 204):
                retry_signal = None
            elif signal_status not in (404, 409):
                return False, round(monotonic() - start, 3), "signal_delivery_failed"
        sleep(max(0, min(delay, deadline - monotonic())))
        delay = min(0.5, delay * 2)


def stop_exclusive_services(policy_path: Path, journal_path: Path, *,
                            incumbent_policy_path: Path | None = None,
                            reconcile: bool = False,
                            force_stop_timeout: bool = False,
                            audit_log: Path | None = None,
                            seconds: int | None = None,
                            uid: int | None = None,
                            backend_factory=None,
                            monotonic=time.monotonic,
                            sleep=time.sleep) -> int:
    """Stop declared exclusive services and verify exit before rollback replacement."""
    if uid is None:
        uid = os.getuid() if os.environ.get("CI_FLEET_DEPLOYER_TESTING") == "1" else 0
    jdir = journal_path.parent
    jmeta = jdir.lstat()
    require(jdir.resolve() == jdir and stat.S_ISDIR(jmeta.st_mode)
            and jmeta.st_uid == uid and stat.S_IMODE(jmeta.st_mode) == 0o700,
            "stop journal directory is unsafe")
    values = parse_policy(protected_file(policy_path, uid))
    incumbent_values = parse_policy(protected_file(incumbent_policy_path, uid)) if incumbent_policy_path is not None else None
    parsed = parse_rollback_stop_policy(values, incumbent_values)
    services: list[str] = parsed["services"]
    timeout_seconds: int = parsed["timeout_seconds"]
    policy_signal: str | None = parsed["signal"]
    environment = values["ENVIRONMENT"]
    require(environment != "production", "production rollback is not authorized")
    target = values["TARGET_ID"]
    identity = values["DEPLOYER_IDENTITY"]
    prefix = f"ci-fleet-deploy-{identity}-"
    expected_labels = ownership_labels(values)
    artifact_digest = values["ARTIFACT_IMAGE"].split("@", 1)[1] if "@" in values["ARTIFACT_IMAGE"] else values["ARTIFACT_IMAGE"]
    source_commit = values.get("SOURCE_COMMIT", "none")

    prior: dict = {}
    if reconcile:
        prior = decode(protected_file(journal_path, uid))
        require(prior.get("environment") == environment and prior.get("target") == target
                and prior.get("deployer_identity") == identity,
                "stop journal identity does not match rollback target")
    else:
        require(not journal_path.exists() and not journal_path.is_symlink(), "stop journal already exists")

    if not services:
        durable_file(journal_path, json.dumps({
            "schema_version": 1, "status": "verified", "signaled": False, "adapter_dispatched": False,
            "environment": environment, "target": target, "deployer_identity": identity,
            "services_state": [], "escalation": "none", "total_elapsed_seconds": 0.0,
        }, sort_keys=True).encode())
        return 0

    budget = seconds if seconds is not None else max(
        60, len(services) * timeout_seconds + (FORCED_KILL_VERIFY_SECONDS * len(services) if force_stop_timeout else 0) + 30
    )
    prior_timed_out: dict[str, str] = {}
    if reconcile:
        raw_timed_out = prior.get("timed_out_containers")
        if isinstance(raw_timed_out, dict):
            for k, v in raw_timed_out.items():
                if isinstance(k, str) and isinstance(v, str) and k and v:
                    prior_timed_out[k] = v
        if prior.get("status") == "timeout" and prior.get("service") and prior.get("container_id"):
            prior_timed_out[str(prior["service"])] = str(prior["container_id"])
    prior_services = {
        entry["service"]: entry
        for entry in prior.get("services_state", [])
        if isinstance(entry, dict) and entry.get("service")
    }
    state: dict = {
        "schema_version": 1,
        "status": "inflight",
        "signaled": bool(prior.get("signaled")) if reconcile else False,
        "adapter_dispatched": False,
        "environment": environment,
        "target": target,
        "deployer_identity": identity,
        "timeout_seconds": timeout_seconds,
        "force_stop_timeout": force_stop_timeout,
        "exclusive_services": services,
        "services_state": [],
        "timed_out_containers": dict(prior_timed_out),
        "escalation": "sigkill" if prior.get("escalation") == "sigkill" else "none",
        "total_elapsed_seconds": 0.0,
    }

    def emit_failure(reason: str, *, service: str | None = None, container_id: str = "none",
                     signal_name: str = "none", elapsed: float = 0.0, escalation: str = "none",
                     exit_code: int = 4, status_label: str = "unverified", write_audit: bool = False,
                     extra: dict | None = None) -> int:
        state["status"] = status_label
        state["reason"] = reason
        if service is not None:
            state["service"] = service
        if container_id != "none":
            state["container_id"] = container_id
        if status_label == "timeout" and service is not None and container_id != "none":
            state["timed_out_containers"][service] = container_id
        if extra:
            state.update(extra)
        durable_file(journal_path, json.dumps(state, sort_keys=True).encode())
        if (write_audit or state["signaled"]) and audit_log is not None:
            append_audit_record(audit_log, {
                "time": _utc_now(),
                "action": "rollback",
                "environment": environment,
                "target": target,
                "source": source_commit,
                "artifact": artifact_digest,
                "service": service or "none",
                "container": container_id,
                "signal": signal_name,
                "timeout_seconds": str(timeout_seconds),
                "elapsed_seconds": f"{elapsed:.3f}",
                "escalation": escalation,
                "result": "stop_failed",
                "reason": reason,
            }, uid)
        print(
            f"ERROR: rollback exclusive-service stop failed: environment={environment} target={target} "
            f"service={service or 'none'} container={container_id} reason={reason} signal={signal_name} "
            f"elapsed={elapsed:.3f}s timeout={timeout_seconds}s escalation={escalation}",
            file=sys.stderr,
        )
        return exit_code

    # Resolution phase: resolve every declared service before signaling anything.
    try:
        if backend_factory is None:
            if os.environ.get("CI_FLEET_DEPLOYER_TESTING") == "1" and os.environ.get("CI_FLEET_DEPLOYER_TEST_DOCKER_SOCKET") == "1":
                backend = TestingDockerBackend(values["DOCKER_SOCKET"], budget)
            else:
                backend = Backend(values["DOCKER_SOCKET"], budget)
        else:
            backend = backend_factory(values["DOCKER_SOCKET"], budget)
    except (ValueError, KeyError, OSError, http.client.HTTPException):
        return emit_failure("docker_backend_unavailable", exit_code=3, status_label="blocked")
    try:
        inventory = list_owned_containers(backend, expected_labels)
    except (ValueError, OSError, json.JSONDecodeError, http.client.HTTPException):
        return emit_failure("container_inventory_unavailable", exit_code=3, status_label="blocked")

    resolved: list[tuple[str, str | None, dict | None, str]] = []
    for svc in services:
        candidates: list[tuple[str, str, dict, str]] = []
        for item in inventory:
            matches, conflict = container_matches_service(item, prefix, svc, expected_labels)
            if conflict:
                cid = item.get("Id") if isinstance(item.get("Id"), str) else "unknown"
                return emit_failure("ambiguous_service_identity", service=svc, container_id=cid,
                                    exit_code=3, status_label="blocked", extra={"containers": [cid]})
            if not matches:
                continue
            cid = item.get("Id")
            if not isinstance(cid, str) or not cid:
                return emit_failure("invalid_container_identity", service=svc, exit_code=3, status_label="blocked")
            try:
                info = backend.inspect("containers", cid)
            except (ValueError, KeyError, OSError, json.JSONDecodeError, http.client.HTTPException):
                return emit_failure("container_inspect_failed", service=svc, container_id=cid,
                                    exit_code=3, status_label="blocked")
            matches, conflict = container_matches_service(info, prefix, svc, expected_labels)
            if conflict or not matches or info.get("Id") != cid:
                return emit_failure("ambiguous_service_identity", service=svc, container_id=cid,
                                    exit_code=3, status_label="blocked")
            if container_exited_and_non_restartable(info)[0]:
                continue
            cstate = info.get("State")
            if not isinstance(cstate, dict):
                return emit_failure("container_state_unavailable", service=svc, container_id=cid,
                                    exit_code=3, status_label="blocked")
            if cstate.get("Paused") is True:
                return emit_failure("container_paused", service=svc, container_id=cid,
                                    exit_code=3, status_label="blocked")
            try:
                sig = policy_signal if policy_signal is not None else normalize_graceful_signal((info.get("Config") or {}).get("StopSignal"))
            except ValueError:
                return emit_failure("unsupported_stop_signal", service=svc, container_id=cid,
                                    exit_code=3, status_label="blocked")
            candidates.append((svc, cid, info, sig))
        if len(candidates) > 1:
            cids = [candidate[1] for candidate in candidates]
            return emit_failure("ambiguous_service_identity", service=svc, container_id=",".join(cids),
                                exit_code=3, status_label="blocked", extra={"containers": cids})
        if candidates:
            resolved.append(candidates[0])
        else:
            resolved.append((svc, None, None, policy_signal or "SIGTERM"))

    overall_start = monotonic()
    operation_deadline = backend.deadline if isinstance(backend, _DockerClient) else None
    for svc, cid, info, sig in resolved:
        if cid is None or info is None:
            state["timed_out_containers"].pop(svc, None)
            state["services_state"].append({"service": svc, "action": "absent", "verified": True, "escalation": "none", "elapsed_seconds": 0.0})
            continue
        quoted = urllib.parse.quote(cid, safe="")
        state.update({"status": "inflight", "signaled": True, "service": svc, "container_id": cid, "signal": sig})
        durable_file(journal_path, json.dumps(state, sort_keys=True).encode())

        svc_start = monotonic()
        svc_deadline = svc_start + timeout_seconds
        # Disable auto-restart before signaling so Docker cannot relaunch on exit.
        restart_policy = (info.get("HostConfig") or {}).get("RestartPolicy")
        restart_name = restart_policy.get("Name", "") if isinstance(restart_policy, dict) else ""
        if restart_name not in ("", "no"):
            if operation_deadline is not None:
                backend.deadline = min(operation_deadline, svc_deadline)
                backend._hard_stop_deadline = True
            try:
                upd_status, _ = backend.request("POST", f"/containers/{quoted}/update",
                                                json.dumps({"RestartPolicy": {"Name": "no", "MaximumRetryCount": 0}}).encode(),
                                                {"Content-Type": "application/json"})
                if not 200 <= upd_status < 300:
                    return emit_failure("restart_policy_update_failed", service=svc, container_id=cid, signal_name=sig)
                info = backend.inspect("containers", cid)
            except (ValueError, OSError, json.JSONDecodeError, http.client.HTTPException):
                return emit_failure("restart_policy_update_failed", service=svc, container_id=cid, signal_name=sig)
            finally:
                if operation_deadline is not None:
                    backend.deadline = operation_deadline
                    backend._hard_stop_deadline = False
            same_service, conflict = container_matches_service(info, prefix, svc, expected_labels)
            if conflict or not same_service or info.get("Id") != cid:
                return emit_failure("ownership_changed", service=svc, container_id=cid, signal_name=sig)
            updated_rp = ((info.get("HostConfig") or {}).get("RestartPolicy") or {}).get("Name", "")
            if updated_rp not in ("", "no"):
                return emit_failure("restart_policy_update_failed", service=svc, container_id=cid, signal_name=sig)

        exited, _ = container_exited_and_non_restartable(info)
        if exited:
            prev_esc = prior_services.get(svc, {}).get("escalation", "none")
            state["timed_out_containers"].pop(svc, None)
            state["services_state"].append({"service": svc, "container_id": cid, "action": "already_stopped",
                                            "verified": True, "escalation": prev_esc, "elapsed_seconds": 0.0})
            continue

        if reconcile and prior_timed_out.get(svc) == cid and not force_stop_timeout:
            return emit_failure("still_running_after_stop_timeout", service=svc, container_id=cid,
                                signal_name=sig, status_label="timeout")

        svc_elapsed = 0.0
        svc_escalation = "none"
        skip_graceful = reconcile and prior_timed_out.get(svc) == cid and force_stop_timeout

        if not skip_graceful:
            if monotonic() >= svc_deadline:
                return emit_failure("restart_policy_update_unverified", service=svc, container_id=cid, signal_name=sig)
            if operation_deadline is not None:
                backend.deadline = min(operation_deadline, svc_deadline)
                backend._hard_stop_deadline = True
            try:
                k_status, _ = backend.request("POST", f"/containers/{quoted}/kill?" + urllib.parse.urlencode({"signal": sig}))
                if k_status == 404:
                    active = _service_has_active_container(backend, prefix, svc, expected_labels)
                    if monotonic() >= svc_deadline:
                        return emit_failure("stop_deadline_unverified", service=svc, container_id=cid, signal_name=sig)
                    if active:
                        return emit_failure("unexpected_replacement_detected", service=svc, container_id=cid, signal_name=sig)
                    state["timed_out_containers"].pop(svc, None)
                    state["services_state"].append({"service": svc, "container_id": cid, "action": "removed",
                                                    "verified": True, "escalation": "none", "elapsed_seconds": 0.0})
                    continue
                if k_status not in (200, 204, 409):
                    return emit_failure("signal_delivery_failed", service=svc, container_id=cid, signal_name=sig)

                ok, svc_elapsed, wait_reason = _wait_container_exited(backend, cid, prefix, svc, expected_labels,
                                                                      svc_deadline, svc_start, monotonic, sleep,
                                                                      retry_signal=sig if k_status == 409 else None)
            except TimeoutError:
                # A request that outlives its deadline can still have reached Docker.
                return emit_failure("docker_communication_failed", service=svc, container_id=cid,
                                    signal_name=sig, elapsed=round(monotonic() - svc_start, 3))
            except (ValueError, KeyError, OSError, json.JSONDecodeError, http.client.HTTPException):
                return emit_failure("docker_communication_failed", service=svc, container_id=cid, signal_name=sig,
                                    elapsed=round(monotonic() - svc_start, 3), escalation=state["escalation"], write_audit=True)
            finally:
                if operation_deadline is not None:
                    backend.deadline = operation_deadline
                    backend._hard_stop_deadline = False
            if ok:
                state["timed_out_containers"].pop(svc, None)
                state["services_state"].append({"service": svc, "container_id": cid, "action": wait_reason,
                                                "signal": sig, "verified": True, "escalation": "none",
                                                "elapsed_seconds": svc_elapsed})
                continue
            if wait_reason != "timeout":
                return emit_failure(wait_reason, service=svc, container_id=cid, signal_name=sig,
                                    elapsed=svc_elapsed, write_audit=True)
            if not force_stop_timeout:
                return emit_failure("timeout", service=svc, container_id=cid, signal_name=sig,
                                    elapsed=svc_elapsed, status_label="timeout", write_audit=True)

        # Explicit operator-authorized SIGKILL escalation after graceful timeout.
        print(
            f"WARN: rollback exclusive-service stop timed out; applying authorized SIGKILL escalation: "
            f"environment={environment} target={target} service={svc} container={cid} "
            f"elapsed={svc_elapsed:.3f}s timeout={timeout_seconds}s",
            file=sys.stderr,
        )
        state["escalation"] = "sigkill"
        state["escalation_requested_at"] = _utc_now()
        durable_file(journal_path, json.dumps(state, sort_keys=True).encode())
        if audit_log is not None:
            append_audit_record(audit_log, {
                "time": _utc_now(),
                "action": "rollback",
                "environment": environment,
                "target": target,
                "source": source_commit,
                "artifact": artifact_digest,
                "service": svc,
                "container": cid,
                "signal": "SIGKILL",
                "timeout_seconds": str(timeout_seconds),
                "elapsed_seconds": f"{svc_elapsed:.3f}",
                "escalation": "sigkill",
                "result": "stop_escalated",
                "reason": "timeout",
            }, uid)
        kill_start = monotonic()
        kill_deadline = kill_start + FORCED_KILL_VERIFY_SECONDS
        if operation_deadline is not None:
            backend.deadline = min(operation_deadline, kill_deadline)
            backend._hard_stop_deadline = True
        try:
            fk_status, _ = backend.request("POST", f"/containers/{quoted}/kill?" + urllib.parse.urlencode({"signal": "SIGKILL"}))
            if fk_status == 404:
                active = _service_has_active_container(backend, prefix, svc, expected_labels)
                if monotonic() >= kill_deadline:
                    return emit_failure("stop_deadline_unverified", service=svc, container_id=cid,
                                        signal_name="SIGKILL", escalation="sigkill")
                if active:
                    return emit_failure("unexpected_replacement_detected", service=svc, container_id=cid,
                                        signal_name="SIGKILL", elapsed=svc_elapsed, escalation="sigkill", write_audit=True)
                state["timed_out_containers"].pop(svc, None)
                state["services_state"].append({"service": svc, "container_id": cid, "action": "removed",
                                                "signal": "SIGKILL", "verified": True, "escalation": "sigkill",
                                                "elapsed_seconds": svc_elapsed})
                continue
            if fk_status not in (200, 204, 409):
                return emit_failure("forced_stop_signal_failed", service=svc, container_id=cid,
                                    signal_name="SIGKILL", elapsed=svc_elapsed, escalation="sigkill", write_audit=True)
            ok, kill_elapsed, kill_reason = _wait_container_exited(backend, cid, prefix, svc, expected_labels,
                                                                   kill_deadline, kill_start, monotonic, sleep,
                                                                   retry_signal="SIGKILL" if fk_status == 409 else None)
        except (ValueError, KeyError, OSError, json.JSONDecodeError, http.client.HTTPException):
            return emit_failure("docker_communication_failed", service=svc, container_id=cid, signal_name="SIGKILL",
                                elapsed=round(svc_elapsed + (monotonic() - kill_start), 3),
                                escalation="sigkill", write_audit=True)
        finally:
            if operation_deadline is not None:
                backend.deadline = operation_deadline
                backend._hard_stop_deadline = False
        total_svc = round(svc_elapsed + kill_elapsed, 3)
        if not ok:
            reason = "forced_stop_exit_unverified" if kill_reason == "timeout" else kill_reason
            return emit_failure(reason, service=svc, container_id=cid, signal_name="SIGKILL",
                                elapsed=total_svc, escalation="sigkill", write_audit=True)
        svc_escalation = "sigkill"
        state["timed_out_containers"].pop(svc, None)
        state["services_state"].append({"service": svc, "container_id": cid, "action": kill_reason,
                                        "signal": "SIGKILL", "verified": True, "escalation": svc_escalation,
                                        "elapsed_seconds": total_svc})

    # Final pre-dispatch inventory barrier across all declared exclusive services.
    try:
        final_inventory = list_owned_containers(backend, expected_labels)
    except (ValueError, OSError, json.JSONDecodeError, http.client.HTTPException):
        return emit_failure("container_inventory_unavailable", escalation=state["escalation"])
    for svc in services:
        for item in final_inventory:
            matches, conflict = container_matches_service(item, prefix, svc, expected_labels)
            if conflict:
                return emit_failure("ambiguous_service_identity", service=svc,
                                    container_id=str(item.get("Id", "unknown")), escalation=state["escalation"])
            if not matches:
                continue
            cid = item.get("Id")
            if not isinstance(cid, str) or not cid:
                return emit_failure("invalid_container_identity", service=svc, escalation=state["escalation"])
            try:
                info = backend.inspect("containers", cid)
            except (ValueError, OSError, json.JSONDecodeError, http.client.HTTPException):
                return emit_failure("container_inspect_failed", service=svc, container_id=cid,
                                    escalation=state["escalation"])
            exited, _ = container_exited_and_non_restartable(info)
            matches, conflict = container_matches_service(info, prefix, svc, expected_labels)
            if conflict or not matches or info.get("Id") != cid or not exited:
                return emit_failure("unexpected_active_container", service=svc, container_id=cid,
                                    escalation=state["escalation"])

    total_elapsed = round(monotonic() - overall_start, 3)
    state["status"] = "verified"
    state["reason"] = "verified"
    state["total_elapsed_seconds"] = total_elapsed
    state.pop("service", None)
    state.pop("container_id", None)
    state.pop("signal", None)
    durable_file(journal_path, json.dumps(state, sort_keys=True).encode())
    print(
        f"ROLLBACK_STOP_VERIFIED environment={environment} target={target} "
        f"services={','.join(services)} escalation={state['escalation']} elapsed={total_elapsed:.3f}s",
        file=sys.stderr,
    )
    return 0


def read_stop_status(journal_path: Path, uid: int | None = None) -> tuple[bool, dict]:
    """Return (unresolved, fields) for an exclusive-stop journal."""
    if uid is None:
        uid = os.getuid() if os.environ.get("CI_FLEET_DEPLOYER_TESTING") == "1" else 0
    try:
        data = decode(protected_file(journal_path, uid))
        unresolved = bool(data.get("signaled") is True and data.get("status") != "verified")
        return unresolved, data
    except (ValueError, KeyError, OSError, json.JSONDecodeError):
        return True, {"environment": "unknown", "target": "unknown", "service": "unknown",
                      "container_id": "unknown", "reason": "corrupted_stop_journal"}


def complete_rollback_readiness(store: Path, unit: str, uid: int) -> bool:
    require(bool(UNIT.fullmatch(unit)), "invalid adapter unit")
    journal = store / unit
    trusted_directory(journal, uid, 0o700)
    ready = journal / "rollback-ready.json"
    if not ready.exists():
        return False
    require(unit_inactive(unit) and not os.path.lexists(journal / "docker-uncertain"),
            "rollback adapter termination or mutation is unverified")
    intent_bytes = protected_file(ready, uid)
    drained = journal / "rollback-drained"
    require(drained.exists() and protected_file(drained, uid) == hashlib.sha256(intent_bytes).hexdigest().encode(),
            "rollback broker drain is unverified")
    intent = decode(intent_bytes)
    marker = Path(intent["marker"])
    require(marker.is_absolute() and marker.resolve() == marker, "rollback marker path is unsafe")
    values = parse_policy(protected_file(journal / "policy.conf", uid))
    if os.path.lexists(marker):
        require(protected_file(marker, uid) == b"committed\n", "rollback commit marker is unverified")
        record = decode(protected_file(journal / "rollback-verified.json", uid))
    else:
        deadline = time.monotonic() + 30
        backend = Backend(values["DOCKER_SOCKET"], 30)
        backend.deadline = deadline
        policy = Policy(values, backend, operation="rollback", exclusive_services=intent["services"])
        policy.rollback_incumbents = intent["incumbents"]
        while True:
            try:
                policy.verify_rollback_commit()
                break
            except RollbackHealthStarting:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                time.sleep(min(0.5, remaining))
        record = {
            "unit": unit, "operation": "rollback", "status": "completed", "exit_code": 0,
            "rollback_outcome": "committed", "rollback_marker": str(marker),
            "artifact_image": values["ARTIFACT_IMAGE"], "verified_containers": policy.verified_containers,
        }
        durable_file(journal / "rollback-verified.json", json.dumps(record).encode())
        durable_file(marker, b"committed\n")
    durable_file(journal / "status.json", json.dumps(record).encode())
    return True


def run_adapter(args) -> int:
    require(args.operation in ("validate", "health", "cleanup", "deploy", "rollback") and 1 <= args.seconds <= 2700, "invalid adapter operation or deadline")
    uid = os.getuid() if os.environ.get("CI_FLEET_DEPLOYER_TESTING") == "1" else 0
    values = parse_policy(protected_file(args.policy, uid))
    incumbent_values = parse_policy(protected_file(args.incumbent_policy, uid)) if getattr(args, "incumbent_policy", None) else None
    stop_policy = parse_rollback_stop_policy(values, incumbent_values) if args.operation == "rollback" else {"services": []}
    require(values.get("CREDENTIAL_PROVIDER") == "file", "materialize the external credential into an approved file first")
    credential = Path(values["CREDENTIAL_REF"])
    credential_bytes = protected_file(credential, uid)
    adapter_bytes = protected_file(args.adapter, uid, adapter_fd=True)
    require(hashlib.sha256(adapter_bytes).hexdigest() == values["ADAPTER_SHA256"], "adapter changed before dispatch")
    backend = Backend(values["DOCKER_SOCKET"], args.seconds)
    require(bool(UNIT.fullmatch(args.unit)), "invalid transient unit")
    journal = args.store / args.unit
    trusted_directory(args.store, uid, 0o700)
    journal.mkdir(mode=0o700)
    parent = os.open(args.store, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)
    durable_file(journal / "policy.conf", protected_file(args.policy, uid))
    for name in ("install-state.json", "active-policy.conf", "last-known-good.json", "last-known-good-policy.conf"):
        source = args.store.parent / name
        if source.exists():
            durable_file(journal / name, protected_file(source, uid))
    capture_deployed(args.store.parent, journal, uid)
    if args.request:
        durable_file(journal / "request.conf", protected_file(args.request, uid))
    durable_file(journal / "status.json", json.dumps({"unit": args.unit, "operation": args.operation,
                                                   "status": "inflight", "started_at": int(time.time())}).encode())
    trusted_directory(args.runtime, uid, 0o711)
    trusted_directory(args.credential_store, uid, 0o711)
    secret = materialize_credential(args.credential_store, values, credential_bytes, uid, backend.uid, backend.gid)
    with tempfile.TemporaryDirectory(prefix="adapter-", dir=args.runtime) as stage:
        stage = Path(stage)
        approved = stage / "adapter"
        approved.write_bytes(adapter_bytes)
        approved.chmod(0o600)
        protected_images = {values["ARTIFACT_IMAGE"]}
        for source in (args.store.parent / "active-policy.conf", args.store.parent / "last-known-good-policy.conf",
                       args.store.parent / "deployed/current/policy.conf"):
            if source.exists():
                retained = parse_policy(protected_file(source.resolve(), uid))
                protected_images.add(retained["ARTIFACT_IMAGE"])
        policy = Policy(values, backend, str(secret), args.marker, args.operation, journal / "docker-uncertain",
                        args.store / "artifact-images.json", protected_images, args.unit,
                        exclusive_services=stop_policy["services"])
        if policy.operation == "rollback" and policy.exclusive_services:
            policy.capture_rollback_incumbents()
            policy.rollback_ready = journal / "rollback-ready.json"
        proxy = stage / "docker.sock"
        with ProxyServer(str(proxy), handler(policy)) as server:
            proxy.chmod(0o666)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                command = sandbox_command(args.unit, args.seconds, proxy, args.policy, approved,
                                          credential, Path(__file__).resolve(), args.request, args.marker is not None, args.operation, backend.path, str(secret))
                result = subprocess.run(command, stdin=subprocess.DEVNULL, close_fds=True).returncode
            finally:
                server.shutdown()
                thread.join(timeout=5)
    require(not policy.uncertain, "Docker mutation completion is unverified")
    require(unit_inactive(args.unit), "adapter termination is unverified")
    require(args.operation != "rollback" or policy.rollback_outcome == "committed"
            or policy.rollback_outcome == "failed" and result != 0, "rollback outcome is unverified")
    if policy.rollback_ready is not None and policy.rollback_outcome == "committed":
        intent_bytes = protected_file(policy.rollback_ready, uid)
        durable_file(journal / "rollback-drained", hashlib.sha256(intent_bytes).hexdigest().encode())
        require(complete_rollback_readiness(args.store, args.unit, uid), "rollback readiness is unverified")
        return result
    status_payload: dict = {
        "unit": args.unit, "operation": args.operation, "status": "completed", "exit_code": result,
        "rollback_outcome": policy.rollback_outcome,
        "rollback_marker": str(args.marker) if args.marker is not None else None,
    }
    if args.operation == "rollback":
        status_payload["artifact_image"] = values["ARTIFACT_IMAGE"]
        status_payload["verified_containers"] = policy.verified_containers
    durable_file(journal / "status.json", json.dumps(status_payload).encode())
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    execute = sub.add_parser("execute")
    execute.add_argument("operation")
    check = sub.add_parser("check")
    check.add_argument("socket")
    consumption = sub.add_parser("consumption")
    consumption.add_argument("--store", type=Path, required=True)
    consumption.add_argument("--prepare", action="store_true")
    retire = sub.add_parser("journal-retire")
    retire.add_argument("--store", type=Path, required=True)
    retire.add_argument("unit")
    retire.add_argument("--inactive-health", action="store_true")
    complete = sub.add_parser("journal-complete")
    complete.add_argument("--store", type=Path, required=True)
    complete.add_argument("unit")
    readiness = sub.add_parser("rollback-readiness")
    readiness.add_argument("--store", type=Path, required=True)
    readiness.add_argument("unit")
    validate_stop = sub.add_parser("validate-stop-policy")
    validate_stop.add_argument("--policy", type=Path, required=True)
    validate_stop.add_argument("--incumbent-policy", type=Path)
    stop_excl = sub.add_parser("stop-exclusive")
    stop_excl.add_argument("--policy", type=Path, required=True)
    stop_excl.add_argument("--incumbent-policy", type=Path)
    stop_excl.add_argument("--journal", type=Path, required=True)
    stop_excl.add_argument("--reconcile", action="store_true")
    stop_excl.add_argument("--force-stop-timeout", action="store_true")
    stop_excl.add_argument("--audit-log", type=Path)
    stop_excl.add_argument("--seconds", type=int)
    stop_status = sub.add_parser("stop-status")
    stop_status.add_argument("--journal", type=Path, required=True)
    audit_cmd = sub.add_parser("append-audit")
    audit_cmd.add_argument("--audit-log", type=Path, required=True)
    audit_cmd.add_argument("--once", action="store_true")
    audit_cmd.add_argument("fields", nargs="+")
    run = sub.add_parser("run")
    run.add_argument("--policy", type=Path, required=True)
    run.add_argument("--incumbent-policy", type=Path)
    run.add_argument("--adapter", type=Path, required=True)
    run.add_argument("--store", type=Path, required=True)
    run.add_argument("--runtime", type=Path, required=True)
    run.add_argument("--credential-store", type=Path, required=True)
    run.add_argument("--unit", required=True)
    run.add_argument("--seconds", type=int, required=True)
    run.add_argument("--request", type=Path)
    run.add_argument("--marker", type=Path)
    run.add_argument("operation")
    args = parser.parse_args()
    try:
        uid = os.getuid() if os.environ.get("CI_FLEET_DEPLOYER_TESTING") == "1" else 0
        if args.command == "consumption":
            line = sys.stdin.buffer.read(4097).decode() if args.prepare else None
            consume_approval(args.store, line, uid)
            return 0
        if args.command == "journal-retire":
            retire_journal(args.store, args.unit, uid, inactive_health=args.inactive_health)
            return 0
        if args.command == "rollback-readiness":
            return 0 if complete_rollback_readiness(args.store, args.unit, uid) else 1
        if args.command == "journal-complete":
            return 0 if journal_complete(args.store, args.unit, uid) else 1
        if args.command == "validate-stop-policy":
            try:
                values = parse_policy(protected_file(args.policy, uid))
                inc = parse_policy(protected_file(args.incumbent_policy, uid)) if args.incumbent_policy else None
                parsed = parse_rollback_stop_policy(values, inc)
            except ValueError as error:
                print(f"ERROR: {error}", file=sys.stderr)
                return 1
            print(json.dumps(parsed, sort_keys=True))
            return 0
        if args.command == "stop-exclusive":
            return stop_exclusive_services(
                args.policy, args.journal,
                incumbent_policy_path=args.incumbent_policy,
                reconcile=args.reconcile,
                force_stop_timeout=args.force_stop_timeout,
                audit_log=args.audit_log,
                seconds=args.seconds,
                uid=uid,
            )
        if args.command == "stop-status":
            unresolved, data = read_stop_status(args.journal, uid)
            print(json.dumps({
                "unresolved": unresolved,
                "replacement_pending": data.get("signaled") is True and data.get("status") == "verified",
                "environment": data.get("environment", "unknown"),
                "target": data.get("target", "unknown"),
                "service": data.get("service", "none"),
                "container_id": data.get("container_id", "none"),
                "reason": data.get("reason", "unknown"),
                "escalation": data.get("escalation", "none"),
                "total_elapsed_seconds": data.get("total_elapsed_seconds", 0.0),
            }, sort_keys=True))
            return 0 if unresolved else 1
        if args.command == "append-audit":
            parsed_fields = {}
            for item in args.fields:
                key, sep, value = item.partition("=")
                require(bool(sep) and bool(key) and bool(value), "invalid audit field")
                parsed_fields[key] = value
            append_audit_record(args.audit_log, parsed_fields, uid, once=args.once)
            return 0
        if args.command == "execute":
            return execute_adapter(args.operation)
        if args.command == "check":
            Backend(args.socket)
            return 0
        return run_adapter(args)
    except (ValueError, KeyError, OSError, json.JSONDecodeError, http.client.HTTPException):
        print("ERROR: restricted adapter prerequisites or execution failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
