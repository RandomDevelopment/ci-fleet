#!/usr/bin/env python3
"""Bind reviewed config digests to immutable local Docker image IDs.

Only portable_managed_image_ids runtime releases may call prepare. The caller
owns release authentication, locks, host admission and publication. This module
writes candidate scratch files, never installed state, tags or containers.

read_bindings returns {schema_version, mode, engine, architecture, daemon, roles}.
roles has exactly controller and runner. Each record has config_digest,
runtime_image_id, manifest_digest (None for classic), config_size,
source_revision, os, architecture, kind, root_descriptor, and descriptors.
Descriptors are base64 ORIGINAL root/manifest bytes, keyed by digest. Raw config
and its Env are never persisted. protected_references returns C/R/M for cleanup;
these protection identities are not interchangeable launch references.

Warm records trust the protected performed proof and Docker's content-addressed
store. They cannot detect hostile root/store replacement with copied identities.
Containerd producer describe needs two exports because inspect does not expose C.
The first discovers C while retaining only planned R/M; the second proves C.
One 180-second transport deadline covers the complete prepare pair, or describe.
ponytail: fixed native size/time bounds require disposable-store measurements
before host acceptance; regular-tar-only native compatibility is unmeasured.
"""

import argparse
import base64
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import platform
import re
import socket
import stat
import sys
import tarfile
import tempfile
import time
from urllib.parse import quote, urlencode

from desired_state import parse_env, render_env

CAPABILITY = "portable_managed_image_ids"
CHUNK = 128 * 1024
MAX_OBJECT = 1024 * 1024
MAX_METADATA = 8 * 1024 * 1024
MAX_STATE = 16 * 1024 * 1024
MAX_MEMBERS = 4096
MAX_STREAM = 8 * 1024**3
MAX_DEPTH = 32
MAX_PADDING = 1024 * 1024
DEADLINE_SECONDS = 180
SOCKET_PATH = "/var/run/docker.sock"
API = "/v1.52"
REVISION_LABEL = "org.opencontainers.image.revision"
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")
INDEX_TYPES = {"application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json"}
MANIFEST_TYPES = {"application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json"}
CONFIG_TYPES = {"application/vnd.oci.image.config.v1+json", "application/vnd.docker.container.image.v1+json"}
LAYER_TYPES = {"application/vnd.oci.image.layer.v1.tar", "application/vnd.oci.image.layer.v1.tar+gzip",
               "application/vnd.oci.image.layer.v1.tar+zstd", "application/vnd.docker.image.rootfs.diff.tar",
               "application/vnd.docker.image.rootfs.diff.tar.gzip"}
ROLES = ("controller", "runner")


class IdentityError(ValueError):
    """Safe operator-facing failure; never include raw Docker/config contents."""


def require(condition, message):
    if not condition:
        raise IdentityError(message)


def digest(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def valid_digest(value):
    require(isinstance(value, str) and DIGEST.fullmatch(value), "invalid full image digest")
    return value


def validate_context(engine, architecture):
    require(isinstance(engine, str) and SHA.fullmatch(engine) and engine != "0" * 40, "engine must be a nonzero full commit SHA")
    require(isinstance(architecture, str) and architecture in {"amd64", "arm64"}, "unsupported image architecture")


def json_value(raw, limit=MAX_OBJECT):
    require(len(raw) <= limit, "JSON object exceeds size limit")
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate JSON key")
            result[key] = value
        return result
    def constant(_):
        raise IdentityError("nonfinite JSON number")
    def finite_float(text):
        value = float(text)
        require(math.isfinite(value), "nonfinite JSON number")
        return value
    # Bound nesting before the decoder can allocate a deeply nested object.
    depth = 0
    quoted = escaped = False
    for byte in raw:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
        elif byte == 34:
            quoted = True
        elif byte in (91, 123):
            depth += 1
            require(depth <= MAX_DEPTH, "JSON nesting exceeds limit")
        elif byte in (93, 125):
            depth -= 1
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant, parse_float=finite_float)
    except (UnicodeError, ValueError, RecursionError) as error:
        raise IdentityError("invalid or duplicate JSON") from error


def _check_protection(metadata, directory=False, private=False):
    require(metadata.st_uid == 0 and not metadata.st_mode & 0o022, "protected input is not root owned or is writable by others")
    require(stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode), "protected input has wrong file type")
    if private:
        require(not metadata.st_mode & 0o077, "binding state must be root private")


def protected_read(path, *, private=False, limit=MAX_STATE):
    """Open every component with dirfd/O_NOFOLLOW, then bound descriptor reads."""
    path = Path(path)
    require(".." not in path.parts, "noncanonical protected path")
    path = Path(os.path.abspath(path))
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _check_protection(os.fstat(directory), directory=True)
        for part in path.parts[1:-1]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = following
            _check_protection(os.fstat(directory), directory=True)
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            before = os.fstat(descriptor)
            _check_protection(before, private=private)
            require(before.st_size <= limit, "protected input exceeds size limit")
            raw = bytearray()
            while len(raw) <= limit:
                chunk = os.read(descriptor, min(CHUNK, limit + 1 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
            after = os.fstat(descriptor)
            require(len(raw) <= limit and len(raw) == before.st_size, "protected input changed or exceeds size limit")
            require((before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
                    (after.st_size, after.st_mtime_ns, after.st_ctime_ns), "protected input changed during read")
            return bytes(raw)
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)


class SocketReader:
    def __init__(self, sock, deadline, *, export_startup=False):
        self.sock, self.deadline, self.buffer = sock, deadline, bytearray()
        self.export_startup = export_startup

    def receive(self):
        remaining = self.deadline - time.monotonic()
        require(remaining > 0, "Docker request deadline exceeded")
        self.sock.settimeout(remaining if self.export_startup else min(30, remaining))
        chunk = self.sock.recv(CHUNK)
        self.export_startup = False
        return chunk

    def read(self, count):
        require(0 < count <= CHUNK, "invalid transport read size")
        if not self.buffer:
            self.buffer.extend(self.receive())
        result = bytes(self.buffer[:count])
        del self.buffer[:count]
        return result

    def exact(self, count):
        result = bytearray()
        while len(result) < count:
            chunk = self.read(min(CHUNK, count - len(result)))
            require(chunk, "truncated HTTP response")
            result.extend(chunk)
        return bytes(result)

    def line(self):
        while b"\r\n" not in self.buffer:
            require(len(self.buffer) < 8192, "HTTP line exceeds limit")
            chunk = self.receive()
            require(chunk, "truncated HTTP headers")
            self.buffer.extend(chunk)
        end = self.buffer.index(b"\r\n")
        require(end < 8192, "HTTP line exceeds limit")
        result = bytes(self.buffer[:end])
        del self.buffer[:end + 2]
        return result


class HTTPBody:
    """Decode HTTP framing and require actual socket EOF after framed bodies."""
    def __init__(self, reader):
        self.reader = reader
        require(re.fullmatch(rb"HTTP/1\.[01] 200(?: [\x20-\x7e]*)?", reader.line()), "Docker HTTP request failed")
        headers, total = {}, 0
        while True:
            line = reader.line()
            total += len(line) + 2
            require(total <= 32768, "HTTP headers exceed limit")
            if not line:
                break
            require(re.fullmatch(rb"[A-Za-z0-9-]+:[\t\x20-\x7e]*", line), "invalid HTTP header")
            key, value = line.split(b":", 1)
            key = key.lower()
            require(key not in headers, "duplicate HTTP header")
            headers[key] = value.strip().lower()
        transfer, length = headers.get(b"transfer-encoding"), headers.get(b"content-length")
        require(not (transfer and length), "ambiguous HTTP framing")
        require(transfer in (None, b"chunked"), "unsupported HTTP transfer encoding")
        require(headers.get(b"content-encoding", b"identity") == b"identity", "unsupported HTTP content encoding")
        require(length is None or re.fullmatch(rb"[0-9]+", length), "invalid HTTP length")
        self.chunked = transfer is not None
        self.remaining = int(length) if length is not None else None
        require(self.remaining is None or self.remaining <= MAX_STREAM, "HTTP response exceeds stream limit")
        self.chunk_remaining, self.done = 0, False

    def finish(self):
        require(not self.reader.read(1), "trailing data after HTTP response")
        self.done = True
        return b""

    def read(self, count):
        require(0 < count <= CHUNK, "invalid HTTP body read size")
        if self.done:
            return b""
        if self.chunked:
            if not self.chunk_remaining:
                line = self.reader.line()
                require(re.fullmatch(rb"[0-9a-fA-F]{1,16}", line), "invalid HTTP chunk framing")
                self.chunk_remaining = int(line, 16)
                require(self.chunk_remaining <= MAX_STREAM, "HTTP chunk exceeds limit")
                if not self.chunk_remaining:
                    require(not self.reader.line(), "HTTP trailers are unsupported")
                    return self.finish()
            result = self.reader.read(min(count, self.chunk_remaining))
            require(result, "truncated HTTP chunk")
            self.chunk_remaining -= len(result)
            if not self.chunk_remaining:
                require(self.reader.exact(2) == b"\r\n", "invalid HTTP chunk terminator")
            return result
        if self.remaining == 0:
            return self.finish()
        result = self.reader.read(count if self.remaining is None else min(count, self.remaining))
        if self.remaining is not None:
            require(result, "truncated HTTP content length")
            self.remaining -= len(result)
        elif not result:
            self.done = True
        return result


class Docker:
    def __init__(self, deadline=None):
        self.deadline = deadline or time.monotonic() + DEADLINE_SECONDS

    @contextmanager
    def response(self, path):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            remaining = self.deadline - time.monotonic()
            require(remaining > 0, "Docker request deadline exceeded")
            sock.settimeout(min(30, remaining))
            sock.connect(SOCKET_PATH)
            sock.sendall(("GET " + API + path + " HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n").encode("ascii"))
            yield HTTPBody(SocketReader(sock, self.deadline, export_startup=path.startswith("/images/get?")))

    def get(self, path):
        with self.response(path) as body:
            result = bytearray()
            while True:
                chunk = body.read(CHUNK)
                if not chunk:
                    return json_value(bytes(result))
                result.extend(chunk)
                require(len(result) <= MAX_OBJECT, "Docker metadata exceeds limit")

    def inspect(self, reference):
        require(isinstance(reference, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,511}", reference), "invalid image reference")
        return self.get("/images/" + quote(reference, safe="") + "/json?manifests=true")

    def identity(self):
        info, version = self.get("/info"), self.get("/version")
        require(isinstance(info, dict) and isinstance(version, dict), "invalid Docker daemon metadata")
        fields = {"id": info.get("ID"), "root": info.get("DockerRootDir"), "driver": info.get("Driver"),
                  "server_version": info.get("ServerVersion"), "version": version.get("Version"),
                  "api_version": version.get("ApiVersion"), "git_commit": version.get("GitCommit")}
        require(all(isinstance(value, str) and 0 < len(value) <= 4096 for value in fields.values()), "Docker identity is incomplete")
        require(info.get("OSType") == version.get("Os") == "linux", "Docker daemon is not Linux")
        status = info.get("DriverStatus")
        require(isinstance(status, list) and len(status) <= 64 and all(
            isinstance(row, list) and len(row) == 2 and all(isinstance(v, str) and len(v) <= 4096 for v in row) for row in status), "invalid Docker store identity")
        components = version.get("Components")
        require(isinstance(components, list) and len(components) <= 64, "Docker software identity is incomplete")
        software = []
        for item in components:
            require(isinstance(item, dict) and isinstance(item.get("Name"), str) and isinstance(item.get("Version"), str), "invalid Docker component identity")
            software.append({"name": item["Name"], "version": item["Version"], "details": item.get("Details", {})})
        fields.update(driver_status=sorted(status), components=software)
        return validate_daemon(fields)

    @contextmanager
    def export(self, runtime):
        valid_digest(runtime)
        with self.response("/images/get?" + urlencode({"names": runtime})) as body:
            yield body


def descriptor(value, types):
    require(isinstance(value, dict), "invalid image descriptor")
    require(isinstance(value.get("mediaType"), str) and value["mediaType"] in types, "unsupported descriptor type")
    valid_digest(value.get("digest"))
    size = value.get("size")
    require(type(size) is int and 0 < size <= (MAX_OBJECT if types != LAYER_TYPES else MAX_STREAM), "invalid descriptor size")
    require(not value.get("urls") and not value.get("data") and not value.get("artifactType"), "unsupported external or embedded descriptor")
    return {"mediaType": value["mediaType"], "digest": value["digest"], "size": size}


def check_platform(value, architecture):
    require(isinstance(value, dict) and value.get("os") == "linux" and value.get("architecture") == architecture,
            "image platform differs from reviewed Linux architecture")
    require(isinstance(value.get("variant", ""), str) and value.get("variant", "") in ({"", "v8"} if architecture == "arm64" else {""}) and
            not value.get("os.version") and not value.get("os.features") and not value.get("features"), "unsupported image platform variant")


def inspect_plan(value, runtime, engine, architecture, *, classic=False):
    require(isinstance(value, dict) and value.get("Id") == runtime, "full runtime image ID did not resolve to itself")
    check_platform({"os": value.get("Os"), "architecture": value.get("Architecture"), "variant": value.get("Variant", "")}, architecture)
    config = value.get("Config")
    require(isinstance(config, dict) and isinstance(config.get("Labels"), dict) and config["Labels"].get(REVISION_LABEL) == engine,
            "image inspect source revision differs from reviewed engine")
    if classic:
        require(not value.get("Descriptor") and not value.get("Manifests"), "classic proof cannot replace a manifest-root proof")
        return None, None
    root = descriptor(value.get("Descriptor"), INDEX_TYPES | MANIFEST_TYPES)
    require(root["digest"] == runtime, "inspect root descriptor differs from runtime ID")
    summaries = value.get("Manifests")
    require(isinstance(summaries, list) and len(summaries) == 1, "inspect must report exactly one runnable manifest")
    selected = summaries[0]
    require(isinstance(selected, dict) and selected.get("Kind") == "image" and selected.get("Available") is True,
            "selected manifest content is unavailable or unsupported")
    manifest = descriptor(selected.get("Descriptor"), MANIFEST_TYPES)
    require(selected.get("ID") == manifest["digest"], "inspect manifest identity is contradictory")
    data = selected.get("ImageData")
    require(isinstance(data, dict), "inspect omitted selected platform")
    check_platform(data.get("Platform"), architecture)
    if "platform" in selected["Descriptor"]:
        check_platform(selected["Descriptor"]["platform"], architecture)
    if root["mediaType"] in MANIFEST_TYPES:
        require(root == manifest, "direct manifest root differs from selected manifest")
    return root, manifest


def blob_path(identity):
    return "blobs/sha256/" + valid_digest(identity)[7:]


def safe_name(name, directory=False):
    require(isinstance(name, str), "invalid tar path")
    if directory and name.endswith("/"):
        name = name[:-1]
    require(name and len(name) <= 255 and re.fullmatch(r"[A-Za-z0-9._/-]+", name) and
            not name.startswith("/") and all(part not in {"", ".", ".."} for part in name.split("/")), "unsafe tar member path")
    require(str(PurePosixPath(name)) == name, "noncanonical tar path")
    return name


class Archive:
    def __init__(self, stream):
        self.stream, self.total = stream, 0

    def read(self, count):
        result = self.stream.read(min(count, CHUNK))
        require(isinstance(result, bytes) and len(result) <= min(count, CHUNK), "invalid export transport read")
        self.total += len(result)
        require(self.total <= MAX_STREAM, "export exceeds byte limit")
        return result

    def exact(self, count):
        result = bytearray()
        while len(result) < count:
            chunk = self.read(count - len(result))
            require(chunk, "truncated tar export")
            result.extend(chunk)
        return bytes(result)

    def scan(self, planned, classic=False, metadata_budget=None):
        retained, records, names = {}, {}, set()
        retained_bytes = 0
        metadata_budget = MAX_METADATA if metadata_budget is None else min(MAX_METADATA, metadata_budget)
        while True:
            header = self.exact(512)
            if header == bytes(512):
                require(self.exact(512) == bytes(512), "tar requires two zero end blocks")
                padding = 0
                while True:
                    chunk = self.read(CHUNK)
                    if not chunk:
                        require(padding % 512 == 0, "partial tar final padding")
                        return retained, records
                    padding += len(chunk)
                    require(padding <= MAX_PADDING and not any(chunk), "invalid trailing tar data or excessive padding")
            try:
                info = tarfile.TarInfo.frombuf(header, "utf-8", "strict")
            except (tarfile.TarError, UnicodeError, ValueError) as error:
                raise IdentityError("invalid tar header or checksum") from error
            require(header[156:157] in (b"0", b"\0", b"5") and not info.linkname and not info.sparse, "unsupported tar link, special or extension member")
            name = safe_name(info.name, info.isdir())
            require(name not in names and len(names) < MAX_MEMBERS, "duplicate tar member or member limit exceeded")
            require(not any(str(parent) in records for parent in PurePosixPath(name).parents if str(parent) != "."), "tar file/directory collision")
            require(not any(existing.startswith(name + "/") for existing in names) or info.isdir(), "tar file/directory collision")
            names.add(name)
            require(type(info.size) is int and 0 <= info.size <= MAX_STREAM - self.total, "tar member size exceeds limit")
            if info.isdir():
                require(info.size == 0 and (name in {"blobs", "blobs/sha256"} or classic and re.fullmatch(r"[0-9a-f]{64}", name)), "unsupported tar directory")
                continue
            blob = re.fullmatch(r"blobs/sha256/([0-9a-f]{64})", name)
            compatibility = name in {"index.json", "manifest.json", "oci-layout", "repositories"}
            classic_file = classic and (re.fullmatch(r"[0-9a-f]{64}\.json", name) or re.fullmatch(r"[0-9a-f]{64}/(?:layer.tar|json|VERSION)", name))
            require(blob or compatibility or classic_file, "unsupported tar member")
            keep = name in planned or compatibility
            if keep or classic_file and not name.endswith("/layer.tar"):
                require(info.size <= MAX_OBJECT, "metadata object exceeds limit")
            if keep:
                require(retained_bytes + info.size <= metadata_budget, "retained metadata exceeds limit")
                retained_bytes += info.size
            hasher, raw, remaining = hashlib.sha256(), bytearray(), info.size
            while remaining:
                chunk = self.exact(min(CHUNK, remaining))
                hasher.update(chunk)
                if keep:
                    raw.extend(chunk)
                remaining -= len(chunk)
            identity = "sha256:" + hasher.hexdigest()
            if blob:
                require(identity == "sha256:" + blob[1], "digest-addressed blob hash mismatch")
            records[name] = (identity, info.size)
            if keep:
                retained[name] = bytes(raw)
                if compatibility:
                    json_value(retained[name])
            require(not any(self.exact((-info.size) % 512)), "nonzero tar member padding")


def json_document(raw, media_types):
    value = json_value(raw)
    require(isinstance(value, dict) and type(value.get("schemaVersion")) is int and value["schemaVersion"] == 2,
            "invalid image metadata schema")
    require(isinstance(value.get("mediaType"), str) and value["mediaType"] in media_types and not value.get("artifactType") and not value.get("subject"), "unsupported image metadata type")
    return value


def authenticate_graph(root, manifest, raw, records, architecture):
    def object_for(desc, types):
        path = blob_path(desc["digest"])
        require(path in raw and records.get(path) == (desc["digest"], desc["size"]), "missing or size-mismatched original descriptor blob")
        obj = json_document(raw[path], types)
        require(obj["mediaType"] == desc["mediaType"], "descriptor media type differs from original bytes")
        return obj
    original = object_for(root, INDEX_TYPES | MANIFEST_TYPES)
    if root["mediaType"] in INDEX_TYPES:
        children = original.get("manifests")
        require(isinstance(children, list) and len(children) == 1, "original index must contain exactly one runnable manifest")
        require(descriptor(children[0], MANIFEST_TYPES) == manifest, "original root does not bind selected manifest")
        check_platform(children[0].get("platform"), architecture)
    else:
        require(root == manifest, "root and selected manifest differ")
    image = object_for(manifest, MANIFEST_TYPES)
    config = descriptor(image.get("config"), CONFIG_TYPES)
    layers = image.get("layers")
    require(isinstance(layers, list) and len(layers) <= MAX_MEMBERS, "invalid manifest layer list")
    for item in layers:
        layer = descriptor(item, LAYER_TYPES)
        require(records.get(blob_path(layer["digest"])) == (layer["digest"], layer["size"]), "selected layer closure is missing or size mismatched")
    return config


def verify_config(raw, expected, engine, architecture):
    require(digest(raw) == expected, "original config differs from reviewed policy")
    value = json_value(raw)
    require(isinstance(value, dict), "invalid original image config")
    check_platform(value, architecture)
    config = value.get("config")
    require(isinstance(config, dict) and isinstance(config.get("Labels"), dict) and config["Labels"].get(REVISION_LABEL) == engine,
            "original config source revision differs from reviewed engine")
    rootfs = value.get("rootfs")
    require(isinstance(rootfs, dict) and rootfs.get("type") == "layers" and isinstance(rootfs.get("diff_ids"), list), "invalid config rootfs")
    require(len(rootfs["diff_ids"]) <= MAX_MEMBERS, "too many config layers")
    for identity in rootfs["diff_ids"]:
        valid_digest(identity)
    return rootfs["diff_ids"]


def prove_export(stream, runtime, expected, engine, architecture, root, manifest, *, discovery=False, metadata_budget=None):
    classic = root is None
    require(not classic or expected == runtime, "classic runtime ID must equal expected config")
    planned = {blob_path(identity) for identity in (runtime, expected) if identity is not None}
    if classic:
        planned.add(runtime[7:] + ".json")
    else:
        planned.add(blob_path(manifest["digest"]))
    raw, records = Archive(stream).scan(planned, classic, metadata_budget)
    descriptors = {}
    if classic:
        mappings = json_value(raw.get("manifest.json", b""))
        require(isinstance(mappings, list) and len(mappings) == 1 and isinstance(mappings[0], dict), "classic save needs one Docker image mapping")
        mapping = mappings[0]
        config_path = mapping.get("Config")
        require(isinstance(config_path, str) and config_path in {blob_path(runtime), runtime[7:] + ".json"} and config_path in raw, "classic mapping does not identify original config")
        config_raw = raw[config_path]
        layers = mapping.get("Layers")
        require(isinstance(layers, list) and len(layers) <= MAX_MEMBERS, "invalid classic layer mapping")
        diff_ids = verify_config(config_raw, expected, engine, architecture)
        require(len(layers) == len(diff_ids), "classic layer count differs from config rootfs")
        for path, identity in zip(layers, diff_ids):
            safe_name(path)
            require(re.fullmatch(r"(?:[0-9a-f]{64}/layer.tar|blobs/sha256/[0-9a-f]{64})", path) and
                    path in records and records[path][0] == identity, "classic uncompressed layer differs from config diff ID")
        config_size, selected = len(config_raw), None
    else:
        wrapper = json_document(raw.get("index.json", b""), INDEX_TYPES)
        children = wrapper.get("manifests")
        require(isinstance(children, list) and len(children) == 1 and descriptor(children[0], INDEX_TYPES | MANIFEST_TYPES) == root,
                "export wrapper does not identify requested original root")
        config = authenticate_graph(root, manifest, raw, records, architecture)
        if discovery:
            return config["digest"]
        require(config["digest"] == expected, "selected manifest binds a different config")
        config_path = blob_path(expected)
        require(config_path in raw and records[config_path] == (expected, config["size"]), "original config is missing or size mismatched")
        diff_ids = verify_config(raw[config_path], expected, engine, architecture)
        image = json_value(raw[blob_path(manifest["digest"])])
        require(len(diff_ids) == len(image["layers"]), "manifest and config layer counts differ")
        config_size, selected = config["size"], manifest["digest"]
        for identity in {runtime, selected}:
            descriptors[identity] = base64.b64encode(raw[blob_path(identity)]).decode("ascii")
    return {"kind": "classic-config" if classic else "manifest-root", "config_digest": expected,
            "runtime_image_id": runtime, "manifest_digest": selected, "config_size": config_size,
            "source_revision": engine, "os": "linux", "architecture": architecture,
            "root_descriptor": root, "descriptors": descriptors}


def cold_proof(client, reference, expected, engine, architecture, *, metadata_budget=None):
    selected = client.inspect(reference)
    require(isinstance(selected, dict), "invalid image inspect response")
    runtime = valid_digest(selected.get("Id"))
    # Backend identity, not coincidental C=R, selects the permitted representation.
    classic = not selected.get("Descriptor") and not selected.get("Manifests")
    root, manifest = inspect_plan(client.inspect(runtime), runtime, engine, architecture, classic=classic)
    if expected is None:
        if classic:
            expected = runtime
        else:
            with client.export(runtime) as stream:
                expected = prove_export(stream, runtime, None, engine, architecture, root, manifest, discovery=True)
    valid_digest(expected)
    require(not classic or expected == runtime, "classic runtime ID must equal expected config")
    with client.export(runtime) as stream:
        result = prove_export(stream, runtime, expected, engine, architecture, root, manifest, metadata_budget=metadata_budget)
    require(inspect_plan(client.inspect(runtime), runtime, engine, architecture, classic=classic) == (root, manifest), "image metadata changed during proof")
    return result


ROLE_FIELDS = {"kind", "config_digest", "runtime_image_id", "manifest_digest", "config_size", "source_revision",
               "os", "architecture", "root_descriptor", "descriptors"}
DAEMON_FIELDS = {"id", "root", "driver", "server_version", "version", "api_version", "git_commit", "driver_status", "components"}


def validate_daemon(daemon):
    require(isinstance(daemon, dict) and set(daemon) == DAEMON_FIELDS, "invalid daemon binding")
    def text(value):
        return isinstance(value, str) and 0 < len(value) <= 4096 and not any(ord(c) < 32 for c in value)
    require(all(text(daemon[name]) for name in DAEMON_FIELDS - {"driver_status", "components"}), "invalid daemon identity fields")
    require(daemon["root"].startswith("/") and os.path.normpath(daemon["root"]) == daemon["root"] and
            daemon["version"] == daemon["server_version"], "invalid daemon store/version identity")
    status, components = daemon["driver_status"], daemon["components"]
    require(isinstance(status, list) and len(status) <= 64 and all(isinstance(row, list) and len(row) == 2 and
            all(text(value) for value in row) for row in status), "invalid daemon driver status")
    require(len({row[0] for row in status}) == len(status) and status == sorted(status), "ambiguous daemon driver status")
    require(isinstance(components, list) and 0 < len(components) <= 64, "invalid daemon component list")
    names = set()
    for component in components:
        require(isinstance(component, dict) and set(component) == {"name", "version", "details"} and
                text(component["name"]) and text(component["version"]), "invalid daemon component")
        require(component["name"] not in names, "duplicate daemon component")
        names.add(component["name"])
        details = component["details"]
        require(isinstance(details, dict) and len(details) <= 64 and all(text(key) and text(value) for key, value in details.items()), "invalid daemon component details")
    require(len(json.dumps(daemon).encode()) <= MAX_OBJECT, "daemon binding exceeds limit")
    return daemon


def validate_role(role, engine, architecture):
    require(isinstance(role, dict) and set(role) == ROLE_FIELDS, "invalid managed-image role record")
    config, runtime = valid_digest(role["config_digest"]), valid_digest(role["runtime_image_id"])
    require(role["source_revision"] == engine and role["os"] == "linux" and role["architecture"] == architecture, "binding source/platform is stale")
    require(type(role["config_size"]) is int and 0 < role["config_size"] <= MAX_OBJECT, "invalid bound config size")
    small = role["descriptors"]
    require(isinstance(small, dict), "invalid retained descriptor map")
    if role["kind"] == "classic-config":
        require(runtime == config and role["manifest_digest"] is None and role["root_descriptor"] is None and not small,
                "invalid classic binding")
        return None, None
    require(role["kind"] == "manifest-root", "unknown managed-image binding mode")
    root = descriptor(role["root_descriptor"], INDEX_TYPES | MANIFEST_TYPES)
    require(role["root_descriptor"] == root, "unexpected root descriptor fields")
    manifest_id = valid_digest(role["manifest_digest"])
    require(root["digest"] == runtime and set(small) == {runtime, manifest_id}, "incomplete original descriptor proof")
    raw, records = {}, {}
    for identity, encoded in small.items():
        require(isinstance(encoded, str) and len(encoded) <= (MAX_OBJECT + 2) // 3 * 4, "retained descriptor exceeds limit")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as error:
            raise IdentityError("invalid retained descriptor encoding") from error
        require(len(content) <= MAX_OBJECT and digest(content) == identity, "retained descriptor hash mismatch")
        require(base64.b64encode(content).decode("ascii") == encoded, "noncanonical retained descriptor encoding")
        raw[blob_path(identity)] = content
        records[blob_path(identity)] = (identity, len(content))
    image = json_document(raw[blob_path(manifest_id)], MANIFEST_TYPES)
    manifest = {"digest": manifest_id, "size": len(raw[blob_path(manifest_id)]), "mediaType": image["mediaType"]}
    # Revalidate descriptor closure. Layer bytes were checked in the cold proof;
    # warm checks trust that protected result and require daemon availability.
    layers = image.get("layers")
    require(isinstance(layers, list) and len(layers) <= MAX_MEMBERS, "invalid cached layer list")
    for item in layers:
        layer = descriptor(item, LAYER_TYPES)
        records[blob_path(layer["digest"])] = (layer["digest"], layer["size"])
    config_desc = authenticate_graph(root, manifest, raw, records, architecture)
    require(config_desc["digest"] == config and config_desc["size"] == role["config_size"], "cached descriptor chain differs from config binding")
    return root, manifest


def validate_bindings(binding, values=None, *, client=None):
    require(isinstance(binding, dict) and set(binding) == {"schema_version", "mode", "engine", "architecture", "daemon", "roles"}, "invalid managed-image binding object")
    require(type(binding["schema_version"]) is int and binding["schema_version"] == 1 and binding["mode"] == CAPABILITY, "unsupported managed-image binding version or mode")
    engine, architecture = binding["engine"], binding["architecture"]
    validate_context(engine, architecture)
    daemon = validate_daemon(binding["daemon"])
    require(isinstance(binding["roles"], dict) and set(binding["roles"]) == set(ROLES), "both managed-image roles are required")
    if values is not None:
        require(values.get("CI_FLEET_ENGINE_REF") == engine, "binding engine differs from policy")
    retained = 0
    for name in ROLES:
        role = binding["roles"][name]
        root, manifest = validate_role(role, engine, architecture)
        retained += sum(len(encoded) * 3 // 4 for encoded in role["descriptors"].values())
        if values is not None:
            prefix = "CI_FLEET_" + name.upper()
            require(values.get(prefix + "_IMAGE_DIGEST") == role["config_digest"], "binding config differs from policy")
            require(prefix + "_RUNTIME_IMAGE_ID" not in values or values[prefix + "_RUNTIME_IMAGE_ID"] == role["runtime_image_id"], "rendered runtime ID differs from binding")
        if client is not None:
            require(inspect_plan(client.inspect(role["runtime_image_id"]), role["runtime_image_id"], engine, architecture,
                                 classic=role["kind"] == "classic-config") == (root, manifest), "warm inspect differs from authenticated binding")
    require(retained <= MAX_METADATA, "retained binding metadata exceeds limit")
    if client is not None:
        require(client.identity() == daemon, "Docker daemon/store/software binding changed")
    return binding


def read_bindings(path, values=None, *, check_daemon=True):
    """Read protected install/checkpoint JSON; validate both roles, never export.

    values may be an already parsed environment mapping. Omit only when the
    caller has independently authenticated the checkpoint's engine/policy.
    check_daemon=False validates stored content only, for offline examination.
    """
    metadata = json_value(protected_read(path, private=True), MAX_STATE)
    require(isinstance(metadata, dict) and "managed_image_bindings" in metadata, "managed-image binding is missing")
    binding = validate_bindings(metadata["managed_image_bindings"], values, client=Docker() if check_daemon else None)
    if "engine_ref" in metadata:
        require(metadata["engine_ref"] == binding["engine"], "outer metadata engine differs from binding")
    if "architecture" in metadata:
        require(metadata["architecture"] == binding["architecture"], "outer metadata architecture differs from binding")
    return binding


def protected_references(binding):
    """Return authenticated C/R/M identities to protect from storage cleanup."""
    validate_bindings(binding)
    return {role[field] for role in binding["roles"].values()
            for field in ("config_digest", "runtime_image_id", "manifest_digest") if role[field] is not None}


def _publish_scratch(environment, metadata, values, document):
    """Stage both private files before replacement; caller supplies scratch only.

    Two paths cannot be crash-atomically renamed as a pair. No verification or
    serialization failure changes either, and replacement failures restore the
    originals. Both remain scratch until the caller's installation transaction.
    """
    payloads = [(Path(environment), render_env(values).encode()),
                (Path(metadata), (json.dumps(document, sort_keys=True, indent=2) + "\n").encode())]
    require(payloads[0][0].absolute() != payloads[1][0].absolute(), "environment and metadata must be separate scratch files")
    require(len(payloads[1][1]) <= MAX_STATE, "candidate metadata exceeds limit")
    staged, originals, replaced = [], [], []
    try:
        for path, raw in payloads:
            original = protected_read(path)
            backup_fd, backup = tempfile.mkstemp(prefix="." + path.name + ".original.", dir=path.parent)
            originals.append((path, backup))
            with os.fdopen(backup_fd, "wb") as handle:
                os.fchmod(handle.fileno(), stat.S_IMODE(path.stat(follow_symlinks=False).st_mode))
                handle.write(original)
                handle.flush()
                os.fsync(handle.fileno())
            fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
            staged.append((path, temporary))
            with os.fdopen(fd, "wb") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
        for path, temporary in staged:
            os.replace(temporary, path)
            replaced.append(path)
    except OSError:
        for path, backup in reversed(originals):
            if path in replaced:
                os.replace(backup, path)
        raise
    finally:
        for _, temporary in staged + originals:
            Path(temporary).unlink(missing_ok=True)


class EnvironmentInput:
    """Give the existing environment parser already protected, bounded bytes."""
    def __init__(self, raw):
        self.raw = raw

    def read_text(self, *, encoding):
        return self.raw.decode(encoding)

    def __str__(self):
        return "candidate environment"


def prepare(args):
    env_raw = protected_read(args.environment)
    values = parse_env(EnvironmentInput(env_raw), allow_unknown=True)
    metadata = json_value(protected_read(args.metadata), MAX_STATE)
    require(isinstance(metadata, dict), "candidate metadata must be an object")
    architecture = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine())
    engine = values.get("CI_FLEET_ENGINE_REF")
    validate_context(engine, architecture)
    require(metadata.get("architecture") == architecture and metadata.get("engine_ref") == engine, "candidate metadata source/platform differs from environment or host")
    release = json_value(protected_read(Path(args.release) / "engine-capabilities.json", limit=MAX_OBJECT))
    require(isinstance(release, dict) and set(release) == {"schema_version", "capabilities"} and type(release["schema_version"]) is int and release["schema_version"] == 1 and
            isinstance(release["capabilities"], dict) and all(type(flag) is bool for flag in release["capabilities"].values()) and
            release["capabilities"].get(CAPABILITY) is True, "runtime release does not declare portable managed-image identity")
    for name in ROLES:
        valid_digest(values.get("CI_FLEET_" + name.upper() + "_IMAGE_DIGEST"))
    client = Docker()
    daemon = client.identity()
    cached = []
    if args.state is not None:
        try:
            cached.append(read_bindings(args.state, check_daemon=False))
        except (IdentityError, OSError):
            if args.check_only:
                raise
    if "managed_image_bindings" in metadata:
        try:
            cached.append(validate_bindings(metadata["managed_image_bindings"]))
        except IdentityError:
            if args.check_only:
                raise
    roles = {}
    for name in ROLES:
        expected = values["CI_FLEET_" + name.upper() + "_IMAGE_DIGEST"]
        override = getattr(args, name + "_reference", None)
        retained = []
        for previous in cached:
            if override is not None or previous["engine"] != engine or previous["architecture"] != architecture:
                continue
            role = previous["roles"][name]
            if role["config_digest"] != expected:
                continue
            retained.append(role["runtime_image_id"])
            if previous["daemon"] != daemon:
                continue
            try:
                root, manifest = validate_role(role, engine, architecture)
                require(inspect_plan(client.inspect(role["runtime_image_id"]), role["runtime_image_id"], engine, architecture,
                                     classic=role["kind"] == "classic-config") == (root, manifest), "retained runtime identity is stale")
                roles[name] = role
                break
            except (IdentityError, OSError):
                continue
        if name not in roles:
            require(not args.check_only, "check-only requires valid retained binding for both roles")
            canonical = values.get("CI_FLEET_" + name.upper() + "_IMAGE") or "ci-fleet-" + name + ":" + engine[:12]
            references = [override] if override is not None else list(dict.fromkeys(retained + [canonical]))
            metadata_budget = MAX_METADATA - sum(len(encoded) * 3 // 4 for role in roles.values() for encoded in role["descriptors"].values())
            for index, reference in enumerate(references):
                try:
                    roles[name] = cold_proof(client, reference, expected, engine, architecture, metadata_budget=metadata_budget)
                    break
                except (IdentityError, OSError):
                    if index == len(references) - 1:
                        raise
    binding = {"schema_version": 1, "mode": CAPABILITY, "engine": engine,
               "architecture": architecture, "daemon": daemon, "roles": roles}
    validate_bindings(binding, values if args.check_only else None, client=client)
    metadata["managed_image_bindings"] = binding
    for name in ROLES:
        values["CI_FLEET_" + name.upper() + "_RUNTIME_IMAGE_ID"] = roles[name]["runtime_image_id"]
    _publish_scratch(args.environment, args.metadata, values, metadata)


def describe(args):
    validate_context(args.engine, args.architecture)
    client = Docker()
    daemon = client.identity()
    proof = cold_proof(client, args.reference, None, args.engine, args.architecture)
    require(client.identity() == daemon, "Docker identity changed during producer proof")
    # Intentionally no binding schema or mode: this is not host authorization.
    print(json.dumps({"purpose": "producer-description-only", "engine": args.engine,
                      "architecture": args.architecture, **proof}, sort_keys=True))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    candidate = sub.add_parser("prepare")
    for name in ("environment", "metadata", "release"):
        candidate.add_argument("--" + name, type=Path, required=True)
    candidate.add_argument("--state", type=Path)
    candidate.add_argument("--runner-reference")
    candidate.add_argument("--controller-reference")
    candidate.add_argument("--check-only", action="store_true")
    candidate.set_defaults(function=prepare)
    producer = sub.add_parser("describe")
    producer.add_argument("--reference", required=True)
    producer.add_argument("--engine", required=True)
    producer.add_argument("--architecture", choices=("amd64", "arm64"), required=True)
    producer.set_defaults(function=describe)
    args = parser.parse_args(argv)
    try:
        args.function(args)
    except (IdentityError, OSError, ValueError) as error:
        print("managed image identity verification failed: " + (str(error) if isinstance(error, IdentityError) else "input or Docker transport unavailable"), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
