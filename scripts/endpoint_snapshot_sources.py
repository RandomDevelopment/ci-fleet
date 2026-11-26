#!/usr/bin/env python3
"""Collect protected worker evidence without changing its measurements."""

from __future__ import annotations

import io
import json
import math
import os
from pathlib import Path
import re
import selectors
import subprocess
import time
from typing import Any

import endpoint_snapshot_store as store
import health
import managed_image_identity as images
from desired_state import SAFE_IDENTIFIER


MAX_SOURCE_BYTES = 64 * 1024
CONTROLLER_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]*")
REVISION = re.compile(r"[0-9a-f]{40}")
CAPACITY_PERIOD = 7 * 24 * 60 * 60
CAPACITY_METRICS = frozenset({
    "host_cpu_percent", "host_memory_used_bytes", "host_swap_used_bytes",
    "host_disk_used_bytes_root", "host_disk_used_bytes_docker",
    "host_inode_used_root", "host_inode_used_docker",
    "runner_cpu_percent", "runner_memory_used_bytes", "runner_memory_limit_bytes",
})
CAPACITY_PATH = "var/lib/ci-fleet/capacity/samples.jsonl"
MAX_HISTORY_BYTES = 128 * 1024 * 1024
SOURCE_PATHS = {
    "installation": ("var/lib/ci-fleet/install-state.json", 0o600),
    "rendered": ("etc/ci-fleet/ci-fleet.env", 0o600),
    "health": ("var/lib/ci-fleet/health/latest.json", 0o644),
    "reconciliation": ("var/lib/ci-fleet/reconcile/state.json", 0o600),
}
ERROR_CODES = {
    "missing": "not_found",
    "unsafe_path": "source_unsafe",
    "unsafe_file": "source_unsafe",
    "too_large": "source_too_large",
    "invalid_json": "source_invalid",
    "invalid_argument": "source_invalid",
    "io": "source_unavailable",
}


class SourceError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _source_error(error: store.StoreError) -> str:
    if error.code in {
        "not_found", "source_unsafe", "source_too_large", "source_invalid",
        "source_unavailable", "source_timeout", "unsupported",
    }:
        return error.code
    return ERROR_CODES.get(error.code, "source_unavailable")


def _rendered_env(value: bytes) -> dict[str, str]:
    try:
        text = value.decode("utf-8")
    except UnicodeError:
        raise SourceError("source_invalid") from None
    if "\r" in text or "\0" in text:
        raise SourceError("source_invalid")
    result = {}
    for line in text.split("\n"):
        if not line or line.startswith("#"):
            continue
        name, separator, setting = line.partition("=")
        if not separator or not ENV_NAME.fullmatch(name) or name in result:
            raise SourceError("source_invalid")
        result[name] = setting
    return result


def _protected_document(root: Path, name: str) -> dict:
    relative, mode = SOURCE_PATHS[name]
    path = root / relative
    if name == "rendered":
        return _rendered_env(store.read_bytes(path, modes=(mode,), max_bytes=MAX_SOURCE_BYTES))
    limit = images.MAX_STATE if name == "installation" else MAX_SOURCE_BYTES
    return store.read_json(path, modes=(mode,), max_bytes=limit)


def _docker_status(*, timeout_seconds: float = 5.0) -> dict[str, Any]:
    if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 5):
        raise SourceError("source_invalid")
    deadline = time.monotonic() + timeout_seconds
    # Leave time to kill and reap a failed reader within the same deadline.
    read_deadline = deadline - min(0.1, timeout_seconds / 10)
    process = None
    selector = None
    try:
        process = subprocess.Popen(
            ["docker", "--host", "unix:///var/run/docker.sock", "exec",
             "ci-fleet-controller-1", "cat", "/run/ci-fleet/status.json"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin"}, close_fds=True,
        )
        if process.stdout is None:
            raise SourceError("source_unavailable")
        descriptor = process.stdout.fileno()
        os.set_blocking(descriptor, False)
        selector = selectors.DefaultSelector()
        selector.register(descriptor, selectors.EVENT_READ)
        value = bytearray()
        while True:
            remaining = read_deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise SourceError("source_timeout")
            try:
                block = os.read(descriptor, min(65536, MAX_SOURCE_BYTES + 1 - len(value)))
            except BlockingIOError:
                continue
            if not block:
                break
            value.extend(block)
            if len(value) > MAX_SOURCE_BYTES:
                raise SourceError("source_too_large")
        remaining = read_deadline - time.monotonic()
        if remaining <= 0:
            raise SourceError("source_timeout")
        if process.wait(timeout=remaining) != 0:
            raise SourceError("source_unavailable")
        result = store.decode_json(bytes(value))
        if time.monotonic() > read_deadline:
            raise SourceError("source_timeout")
        return result
    except SourceError:
        raise
    except subprocess.TimeoutExpired:
        raise SourceError("source_timeout") from None
    except store.StoreError as error:
        raise SourceError(_source_error(error)) from None
    except (OSError, ValueError):
        raise SourceError("source_unavailable") from None
    finally:
        if selector is not None:
            selector.close()
        if process is not None:
            if process.stdout is not None:
                process.stdout.close()
            if process.poll() is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=max(0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    pass


def _boot(root: Path) -> dict:
    raw = store.read_bytes(root / "proc/stat", modes=(0o444,), max_bytes=1024 * 1024)
    times = re.findall(rb"(?m)^btime ([1-9][0-9]{0,18})$", raw)
    if len(times) != 1:
        raise SourceError("source_invalid")
    return {"boot_at": int(times[0])}


def _selection(root: Path, directory: str) -> dict:
    base = root / directory
    pointer = base / "current"
    target = store.read_selection(pointer)
    release = Path(target)
    revision = release.name
    if target != str(base / "releases" / revision) or not REVISION.fullmatch(revision):
        raise SourceError("source_unsafe")
    marker = store.read_bytes(release / ".ci-fleet-engine-ref", modes=(0o644,), max_bytes=41)
    if marker != f"{revision}\n".encode() or store.read_selection(pointer) != target:
        raise SourceError("revision_unverified")
    return {"revision": revision, "observed_at": int(time.time())}


def _enrollment_identity(root: Path, rendered: dict) -> dict:
    path = rendered.get("CI_FLEET_GITHUB_APP_PRIVATE_KEY_FILE")
    client = rendered.get("CI_FLEET_GITHUB_APP_CLIENT_ID")
    installation = rendered.get("CI_FLEET_GITHUB_APP_INSTALLATION_ID")
    if not any((path, client, installation)):
        raise SourceError("unsupported")
    if not all((path, client, installation)):
        raise SourceError("source_invalid")
    path = Path(path)
    if (not path.is_absolute() or path.anchor != "/" or ".." in path.parts
            or not re.fullmatch(r"[A-Za-z0-9_./-]+", str(path))
            or not SAFE_IDENTIFIER.fullmatch(client)
            or not re.fullmatch(r"[1-9][0-9]*", installation)):
        raise SourceError("source_invalid")
    try:
        store.check_file(root / path.relative_to("/"), modes=(0o400, 0o600), max_bytes=MAX_SOURCE_BYTES)
    except store.StoreError as error:
        if error.code != "not_found":
            raise
        return {"state": "absent", "observed_at": int(time.time())}
    return {"state": "present", "observed_at": int(time.time())}


def _image_verification(installation: dict, rendered: dict, timeout_seconds: float) -> dict:
    if "managed_image_bindings" not in installation:
        raise SourceError("unsupported")
    binding = installation["managed_image_bindings"]
    if (not isinstance(binding, dict) or binding.get("engine") != installation["engine_ref"]
            or binding.get("architecture") != installation.get("architecture")):
        raise SourceError("revision_unverified")
    if not all(rendered.get(f"CI_FLEET_{role}_RUNTIME_IMAGE_ID") for role in ("CONTROLLER", "RUNNER")):
        raise SourceError("revision_unverified")
    deadline = time.monotonic() + timeout_seconds
    try:
        images.validate_bindings(binding, rendered, client=images.Docker(deadline=deadline))
    except images.IdentityError as error:
        code = "source_timeout" if time.monotonic() >= deadline else "source_unavailable"
        raise SourceError(code) from error
    if time.monotonic() >= deadline:
        raise SourceError("source_timeout")
    return {"observed_at": int(time.time()), "revision": binding["engine"], "architecture": binding["architecture"]}


def capacity_aggregate(history: bytes, pool: str, now: int) -> tuple[int, dict]:
    if len(history) > MAX_HISTORY_BYTES:
        raise SourceError("source_too_large")
    deadline = time.monotonic() + 15
    metrics: dict[str, list] = {}
    sample_count = runner_count = latest = 0
    for index, line in enumerate(io.BytesIO(history)):
        if time.monotonic() >= deadline:
            raise SourceError("source_timeout")
        if index >= health.CAPACITY_COMPACT_SAMPLES:
            raise SourceError("source_too_large")
        if not line.endswith(b"\n"):
            break  # An append may be in progress.
        try:
            sample = json.loads(line.decode("utf-8"), object_pairs_hook=store._unique_object)
            if not isinstance(sample, dict) or sample.get("pool") != pool:
                continue
            # Check the record against its own time before classifying future evidence.
            observed = sample["timestamp"]
            parsed = health._sample_values(sample, observed)
            if parsed is None or parsed[2] == 0:
                continue
        except (KeyError, TypeError, ValueError, OverflowError, RecursionError):
            continue
        if observed > now:
            raise SourceError("clock_uncertain")
        if observed < now - CAPACITY_PERIOD or observed <= 0:
            continue
        latest = max(latest, observed)
        sample_count += 1
        _, values, count = parsed
        runner_count += count
        for name in CAPACITY_METRICS & values.keys():
            metrics.setdefault(name, []).extend(values[name])
    if not sample_count:
        raise SourceError("not_found")
    aggregate = {"samples": sample_count, "runner_observations": runner_count,
                 "metrics": {name: health._percentiles(values) for name, values in metrics.items()}}
    if time.monotonic() >= deadline:
        raise SourceError("source_timeout")
    return latest, aggregate


def collect_documents(controller: str, *, root: Path = Path("/"),
                      timeout_seconds: float = 5.0) -> tuple[dict[str, dict | None], dict[str, str]]:
    documents: dict[str, dict | None] = {name: None for name in (*SOURCE_PATHS, "controller")}
    errors: dict[str, str] = {}

    def collect(name, read):
        try:
            documents[name] = read()
        except store.StoreError as error:
            errors[name] = _source_error(error)
        except SourceError as error:
            errors[name] = error.code
        except (OSError, ValueError, KeyError, TypeError, OverflowError):
            errors[name] = "source_unavailable"

    for name in SOURCE_PATHS:
        collect(name, lambda: _protected_document(root, name))
    collect("boot", lambda: _boot(root))
    installation = documents["installation"]
    if (not isinstance(controller, str) or not CONTROLLER_ID.fullmatch(controller)
            or installation is None or type(installation.get("schema_version")) is not int
            or installation.get("schema_version") != 1 or installation.get("controller") != controller):
        errors["controller"] = "unsupported"
        return documents, errors
    collect("controller", lambda: _docker_status(timeout_seconds=timeout_seconds))
    collect("runtime_revision", lambda: _selection(root, "opt/ci-fleet"))
    collect("manager_revision", lambda: _selection(root, "opt/ci-fleet/manager"))
    collect("capacity", lambda: {"history": store.read_bytes(
        root / CAPACITY_PATH, max_bytes=MAX_HISTORY_BYTES, directory_mode=0o700)})
    rendered = documents["rendered"]
    if (rendered and rendered.get("CI_FLEET_INSTANCE") == controller
            and rendered.get("CI_FLEET_CONFIG_REF") == installation.get("config_ref")
            and rendered.get("CI_FLEET_ENGINE_REF") == installation.get("engine_ref")):
        if any(key.startswith("CI_FLEET_GITHUB_APP_") for key in rendered):
            collect("enrollment_identity", lambda: _enrollment_identity(root, rendered))
        if "managed_image_bindings" in installation:
            collect("image_verification", lambda: _image_verification(installation, rendered, timeout_seconds))
    return documents, errors
