#!/usr/bin/env python3
"""Collect protected worker evidence without changing its measurements."""

from __future__ import annotations

import math
import os
from pathlib import Path
import re
import selectors
import subprocess
import time
from typing import Any

import endpoint_snapshot_store as store


MAX_SOURCE_BYTES = 64 * 1024
CONTROLLER_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]*")
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


def collect_documents(controller: str, *, root: Path = Path("/"),
                      timeout_seconds: float = 5.0) -> tuple[dict[str, dict | None], dict[str, str]]:
    documents: dict[str, dict | None] = {name: None for name in (*SOURCE_PATHS, "controller")}
    errors: dict[str, str] = {}
    for name, (relative, mode) in SOURCE_PATHS.items():
        try:
            path = root / relative
            if name == "rendered":
                documents[name] = _rendered_env(store.read_bytes(
                    path, modes=(mode,), max_bytes=MAX_SOURCE_BYTES))
            else:
                documents[name] = store.read_json(path, modes=(mode,), max_bytes=MAX_SOURCE_BYTES)
        except store.StoreError as error:
            errors[name] = _source_error(error)
        except SourceError as error:
            errors[name] = error.code
        except (OSError, ValueError):
            errors[name] = "source_unavailable"
    installation = documents["installation"]
    if (not isinstance(controller, str) or not CONTROLLER_ID.fullmatch(controller)
            or installation is None or type(installation.get("schema_version")) is not int
            or installation.get("schema_version") != 1 or installation.get("controller") != controller):
        errors["controller"] = "unsupported"
        return documents, errors
    try:
        documents["controller"] = _docker_status(timeout_seconds=timeout_seconds)
    except SourceError as error:
        errors["controller"] = error.code
    except (OSError, ValueError):
        errors["controller"] = "source_unavailable"
    return documents, errors
