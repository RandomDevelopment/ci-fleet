#!/usr/bin/env python3
"""Constrain application adapters to one rootless Docker target."""

from __future__ import annotations

import argparse
import grp
import hashlib
import http.client
import http.server
import json
import os
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


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, *, rootless: bool = True):
        super().__init__("localhost", timeout=120)
        self.path, self.rootless, self.peer_uid = path, rootless, None

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)
        pid, uid, gid = struct.unpack("3i", self.sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if self.rootless:
            require(uid > 0 and gid > 0, "Docker daemon must run without host-root authority")
            metadata = os.lstat(self.path)
            require(stat.S_ISSOCK(metadata.st_mode) and metadata.st_uid == uid,
                    "rootless Docker socket owner does not match its peer")
            process = {}
            for line in Path(f"/proc/{pid}/status").read_text().splitlines():
                key, _, value = line.partition(":")
                process[key] = value.split()
            require(all(int(value) == uid for value in process["Uid"]), "Docker peer retains another user identity")
            groups = {int(value) for value in process["Groups"]} | {gid}
            for name in ("sudo", "wheel", "docker"):
                try:
                    require(grp.getgrnam(name).gr_gid not in groups, "Docker peer has a privileged group")
                except KeyError:
                    pass
            require(0 not in groups, "Docker peer belongs to the root group")
            raw = Path("/run/docker.sock")
            if raw.exists():
                raw_info = raw.stat()
                require(raw_info.st_uid != 0 or (not raw_info.st_mode & 0o002
                        and (not raw_info.st_mode & 0o020 or raw_info.st_gid not in groups)),
                        "Docker peer can access the host-root daemon")
        self.peer_uid = uid


class Backend:
    def __init__(self, path: str):
        require(bool(re.fullmatch(r"/[A-Za-z0-9._/-]+", path)) and os.path.realpath(path) == path, "Docker socket must be canonical")
        self.path = path
        connection = UnixConnection(path)
        try:
            connection.request("GET", "/info")
            response = connection.getresponse()
            value = decode(response.read(MAX_REQUEST + 1))
            require(response.status == 200 and "name=rootless" in value.get("SecurityOptions", []),
                    "a dedicated rootless Docker daemon is required")
            self.uid = connection.peer_uid
        finally:
            connection.close()

    def request(self, method: str, path: str, body: bytes = b"", headers: dict | None = None) -> tuple[int, bytes]:
        connection = UnixConnection(self.path)
        try:
            connection.connect()
            require(connection.peer_uid == self.uid, "Docker daemon identity changed")
            connection.request(method, path, body, headers or {})
            response = connection.getresponse()
            result = response.read(MAX_RESPONSE + 1)
            require(len(result) <= MAX_RESPONSE, "Docker response exceeded the bound")
            return response.status, result
        finally:
            connection.close()

    def inspect(self, kind: str, name: str) -> dict:
        suffix = "/json" if kind == "containers" else ""
        status, body = self.request("GET", f"/{kind}/{urllib.parse.quote(name, safe='')}{suffix}")
        require(status == 200, "owned Docker resource could not be inspected")
        return decode(body)


class Policy:
    def __init__(self, values: dict[str, str], backend, credential: str = "", marker: Path | None = None, operation: str = "deploy"):
        self.values, self.backend, self.credential, self.marker = values, backend, credential, marker
        self.operation = operation
        self.image = values["ARTIFACT_IMAGE"]
        require(bool(IMAGE.fullmatch(self.image)), "approved artifact is not immutable")
        self.labels = {LABEL + "role": "deployer", LABEL + "identity": values["DEPLOYER_IDENTITY"],
                       LABEL + "environment": values["ENVIRONMENT"], LABEL + "target": values["TARGET_ID"]}
        self.prefix = "ci-fleet-deploy-" + values["DEPLOYER_IDENTITY"] + "-"

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
                     "RestartPolicy", "AutoRemove", "Init", "Tmpfs"}
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


def handler(policy: Policy):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def dispatch(self):
            try:
                require(not self.headers.get("Transfer-Encoding"), "chunked requests are forbidden")
                length = int(self.headers.get("Content-Length", "0"))
                require(0 <= length <= MAX_REQUEST, "request exceeded the bound")
                body = self.rfile.read(length)
                if self.command == "POST" and self.path == "/ci-fleet/rollback-commit":
                    require(policy.marker is not None and body == b"committed", "rollback marker is not authorized")
                    durable_file(policy.marker, b"committed\n")
                    status, response = 204, b""
                else:
                    path, body = policy.authorize(self.command, self.path, body)
                    headers = {"Content-Type": "application/json"}
                    if path.startswith("/images/create") and self.headers.get("X-Registry-Auth"):
                        headers["X-Registry-Auth"] = self.headers["X-Registry-Auth"]
                    status, response = policy.backend.request(self.command, path, body, headers)
            except (ValueError, KeyError, TypeError, AttributeError, OSError, json.JSONDecodeError, http.client.HTTPException):
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


def execute_adapter(operation: str) -> int:
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
    status = subprocess.run([str(adapter), operation], env=environment, close_fds=True).returncode
    if "CI_FLEET_DEPLOYER_ROLLBACK_COMMIT" in environment and commit.exists():
        require(stat.S_ISREG(commit.lstat().st_mode) and not commit.is_symlink(), "unsafe rollback marker")
        connection = UnixConnection(str(runtime / "docker.sock"), rootless=False)
        try:
            connection.request("POST", "/ci-fleet/rollback-commit", b"committed")
            require(connection.getresponse().status == 204, "rollback marker publication failed")
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
    backend = Backend(values["DOCKER_SOCKET"])
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
    with tempfile.TemporaryDirectory(prefix="adapter-", dir=args.runtime) as stage:
        stage = Path(stage)
        approved = stage / "adapter"
        approved.write_bytes(adapter_bytes)
        approved.chmod(0o600)
        with tempfile.TemporaryDirectory(prefix="credential-", dir=args.runtime) as secret_stage:
            secret_stage = Path(secret_stage)
            os.chown(secret_stage, backend.uid, -1)
            secret = secret_stage / "application"
            secret.write_bytes(credential_bytes)
            secret.chmod(0o400)
            os.chown(secret, backend.uid, -1)
            policy = Policy(values, backend, str(secret), args.marker, args.operation)
            proxy = stage / "docker.sock"
            with ProxyServer(str(proxy), handler(policy)) as server:
                proxy.chmod(0o666)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    command = sandbox_command(args.unit, args.seconds, proxy, args.policy, approved,
                                              credential, Path(__file__).resolve(), args.request, args.marker is not None, args.operation, backend.path, str(secret))
                    result = subprocess.run(command, close_fds=True).returncode
                finally:
                    server.shutdown()
                    thread.join(timeout=5)
    require(unit_inactive(args.unit), "adapter termination is unverified")
    durable_file(journal / "status.json", json.dumps({"unit": args.unit, "operation": args.operation,
                                                   "status": "completed", "exit_code": result}).encode())
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    execute = sub.add_parser("execute")
    execute.add_argument("operation")
    check = sub.add_parser("check")
    check.add_argument("socket")
    run = sub.add_parser("run")
    run.add_argument("--policy", type=Path, required=True)
    run.add_argument("--adapter", type=Path, required=True)
    run.add_argument("--store", type=Path, required=True)
    run.add_argument("--runtime", type=Path, required=True)
    run.add_argument("--unit", required=True)
    run.add_argument("--seconds", type=int, required=True)
    run.add_argument("--request", type=Path)
    run.add_argument("--marker", type=Path)
    run.add_argument("operation")
    args = parser.parse_args()
    try:
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
