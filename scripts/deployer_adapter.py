#!/usr/bin/env python3
"""Constrain application adapters to one rootless Docker target."""

from __future__ import annotations

import argparse
import ctypes
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
IMAGE = re.compile(r"^[a-z0-9][a-z0-9.:-]*/[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}$")
UNIT = re.compile(r"^ci-fleet-deployer-adapter-[0-9]+-[0-9]+\.service$")


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


class Backend:
    def __init__(self, path: str, seconds: int = 120):
        self.deadline = time.monotonic() + seconds + 10
        require(bool(re.fullmatch(r"/[A-Za-z0-9._/-]+", path)) and os.path.realpath(path) == path, "Docker socket must be canonical")
        self.path = path
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

    def request(self, method: str, path: str, body: bytes = b"", headers: dict | None = None) -> tuple[int, bytes]:
        remaining = self.deadline - time.monotonic()
        require(remaining > 0, "Docker operation deadline elapsed")
        connection = UnixConnection(self.path, timeout=remaining)
        try:
            connection.connect()
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


class Policy:
    def __init__(self, values: dict[str, str], backend, credential: str = "", marker: Path | None = None, operation: str = "deploy", uncertain_marker: Path | None = None, artifact_store: Path | None = None, protected_images: set[str] | None = None, unit: str | None = None):
        self.values, self.backend, self.credential, self.marker = values, backend, credential, marker
        self.operation, self.uncertain_marker, self.uncertain = operation, uncertain_marker, False
        self.rollback_outcome = None
        self.unit = unit
        self.artifact_store = artifact_store
        self.protected_images = (protected_images or set()) | {values["ARTIFACT_IMAGE"]}
        self.image = values["ARTIFACT_IMAGE"]
        require(bool(IMAGE.fullmatch(self.image)), "approved artifact is not immutable")
        self.labels = {LABEL + "role": "deployer", LABEL + "identity": values["DEPLOYER_IDENTITY"],
                       LABEL + "environment": values["ENVIRONMENT"], LABEL + "target": values["TARGET_ID"]}
        self.prefix = "ci-fleet-deploy-" + values["DEPLOYER_IDENTITY"] + "-"

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
                and host.get("SecurityOpt") in (["no-new-privileges"], ["no-new-privileges:true"]),
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
            self.container(decode(body))
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
        self.owned(kind, identity)
        allowed = ((method == "GET" and action in (None, "json", "logs"))
                   or (method == "DELETE" and action is None)
                   or (method == "POST" and kind == "containers" and action in ("start", "stop", "restart", "kill", "wait")))
        require(allowed and not body and set(query) <= {"t", "signal", "force", "v", "stdout", "stderr", "tail"},
                "resource operation is outside the deployment contract")
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
                if self.command == "POST" and self.path == "/ci-fleet/rollback-commit":
                    require(executor_peer(self.connection, policy.unit), "rollback outcome requires the executor")
                    require(policy.operation == "rollback" and policy.marker is not None
                            and body in (b"committed", b"failed"), "rollback outcome is not authorized")
                    outcome = body.decode()
                    require(policy.rollback_outcome in (None, outcome), "rollback outcome conflicts")
                    if outcome == "committed":
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
                    mutation = self.command not in ("GET", "HEAD")
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


def retire_journal(store: Path, unit: str, uid: int = 0) -> None:
    require(journal_complete(store, unit, uid), "journal completion is unverified")
    shutil.rmtree(store / unit)
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


def run_adapter(args) -> int:
    require(args.operation in ("validate", "health", "cleanup", "deploy", "rollback") and 1 <= args.seconds <= 2700, "invalid adapter operation or deadline")
    uid = os.getuid() if os.environ.get("CI_FLEET_DEPLOYER_TESTING") == "1" else 0
    values = parse_policy(protected_file(args.policy, uid))
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
                        args.store / "artifact-images.json", protected_images, args.unit)
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
    durable_file(journal / "status.json", json.dumps({"unit": args.unit, "operation": args.operation,
                                                   "status": "completed", "exit_code": result,
                                                   "rollback_outcome": policy.rollback_outcome,
                                                   "rollback_marker": str(args.marker) if args.marker is not None else None}).encode())
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
    complete = sub.add_parser("journal-complete")
    complete.add_argument("--store", type=Path, required=True)
    complete.add_argument("unit")
    run = sub.add_parser("run")
    run.add_argument("--policy", type=Path, required=True)
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
        if args.command == "consumption":
            uid = os.getuid() if os.environ.get("CI_FLEET_DEPLOYER_TESTING") == "1" else 0
            line = sys.stdin.buffer.read(4097).decode() if args.prepare else None
            consume_approval(args.store, line, uid)
            return 0
        if args.command == "journal-retire":
            uid = os.getuid() if os.environ.get("CI_FLEET_DEPLOYER_TESTING") == "1" else 0
            retire_journal(args.store, args.unit, uid)
            return 0
        if args.command == "journal-complete":
            uid = os.getuid() if os.environ.get("CI_FLEET_DEPLOYER_TESTING") == "1" else 0
            return 0 if journal_complete(args.store, args.unit, uid) else 1
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
