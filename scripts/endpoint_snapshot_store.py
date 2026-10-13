#!/usr/bin/env python3
"""Protected local snapshot reads and atomic endpoint publication."""

from __future__ import annotations

import errno
import fcntl
import json
import math
import os
from pathlib import Path
import stat
import time
from typing import Any


OWNER_UID = 0
INITIAL_GID = 0
MAX_ENDPOINT_BYTES = 64 * 1024
MAX_JOB_BYTES = 4 * 1024 * 1024
MAX_EXPORT_BYTES = 16 * 1024 * 1024
MAX_JSON_DEPTH = 32
READ_TIMEOUT_SECONDS = 2.0
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
EXPORT_NAMES = {"endpoint.json", "endpoint.json.tmp", "jobs.json", "jobs.json.tmp"}


class StoreError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise StoreError(code)


def _os_error(error: OSError) -> StoreError:
    if error.errno == errno.ENOENT:
        return StoreError("not_found")
    if error.errno in {errno.ELOOP, errno.ENOTDIR, errno.EISDIR}:
        return StoreError("unsafe_path")
    return StoreError("io")


def _directory(metadata: os.stat_result, *, sticky_tmp: bool = False) -> None:
    _require(stat.S_ISDIR(metadata.st_mode) and metadata.st_uid in {0, OWNER_UID}, "unsafe_path")
    writable = metadata.st_mode & 0o022
    _require(not writable or sticky_tmp and metadata.st_uid == 0 and bool(metadata.st_mode & stat.S_ISVTX),
             "unsafe_path")


def _open_directory(path: Path) -> int:
    _require(path.is_absolute() and ".." not in path.parts, "unsafe_path")
    descriptor = os.open("/", DIRECTORY_FLAGS)
    try:
        _directory(os.fstat(descriptor))
        current = Path("/")
        for name in path.parts[1:]:
            child = os.open(name, DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            current /= name
            _directory(os.fstat(descriptor), sticky_tmp=current == Path("/tmp"))
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _regular(metadata: os.stat_result, *, modes: tuple[int, ...], max_bytes: int,
             gid: int | None = None) -> None:
    _require(stat.S_ISREG(metadata.st_mode) and metadata.st_uid == OWNER_UID
             and metadata.st_nlink == 1 and stat.S_IMODE(metadata.st_mode) in modes
             and (gid is None or metadata.st_gid == gid), "unsafe_file")
    _require(metadata.st_size <= max_bytes, "too_large")


def _read_at(directory_fd: int, name: str, *, modes: tuple[int, ...], max_bytes: int,
             gid: int | None = None) -> bytes:
    before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    _regular(before, modes=modes, max_bytes=max_bytes, gid=gid)
    descriptor = os.open(name, READ_FLAGS, dir_fd=directory_fd)
    try:
        actual = os.fstat(descriptor)
        _regular(actual, modes=modes, max_bytes=max_bytes, gid=gid)
        _require((before.st_dev, before.st_ino) == (actual.st_dev, actual.st_ino), "unsafe_file")
        deadline = time.monotonic() + READ_TIMEOUT_SECONDS
        value = bytearray()
        while True:
            _require(time.monotonic() <= deadline, "io")
            block = os.read(descriptor, min(65536, max_bytes + 1 - len(value)))
            if not block:
                return bytes(value)
            value.extend(block)
            _require(len(value) <= max_bytes, "too_large")
    finally:
        os.close(descriptor)


def read_bytes(path: Path, *, modes: tuple[int, ...] = (0o600,), max_bytes: int = MAX_ENDPOINT_BYTES,
               gid: int | None = None) -> bytes:
    _require(type(max_bytes) is int and max_bytes > 0, "invalid_argument")
    directory_fd = None
    try:
        directory_fd = _open_directory(path.parent)
        return _read_at(directory_fd, path.name, modes=modes, max_bytes=max_bytes, gid=gid)
    except OSError as error:
        raise _os_error(error) from None
    finally:
        if directory_fd is not None:
            os.close(directory_fd)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        _require(key not in value, "invalid_json")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    raise StoreError("invalid_json")


def _finite_float(value: str) -> float:
    result = float(value)
    _require(math.isfinite(result), "invalid_json")
    return result


def decode_json(value: bytes, *, max_bytes: int = MAX_ENDPOINT_BYTES) -> dict[str, Any]:
    _require(type(max_bytes) is int and max_bytes > 0, "invalid_argument")
    _require(isinstance(value, bytes), "invalid_json")
    _require(len(value) <= max_bytes, "too_large")
    try:
        result = json.loads(value.decode("utf-8"), object_pairs_hook=_unique_object,
                            parse_constant=_reject_constant, parse_float=_finite_float)
    except (UnicodeError, ValueError, RecursionError):
        raise StoreError("invalid_json") from None
    _require(isinstance(result, dict), "invalid_json")
    pending = [(result, 1)]
    while pending:
        item, depth = pending.pop()
        _require(depth <= MAX_JSON_DEPTH, "invalid_json")
        children = item.values() if isinstance(item, dict) else item if isinstance(item, list) else ()
        pending.extend((child, depth + 1) for child in children if isinstance(child, (dict, list)))
    return result


def read_json(path: Path, *, modes: tuple[int, ...] = (0o600,), max_bytes: int = MAX_ENDPOINT_BYTES,
              gid: int | None = None) -> dict[str, Any]:
    return decode_json(read_bytes(path, modes=modes, max_bytes=max_bytes, gid=gid), max_bytes=max_bytes)


def _open_exports(base: Path, observer_gid: int) -> int:
    _require(type(observer_gid) is int and 0 <= observer_gid < 2**32 - 1, "invalid_argument")
    base_fd = _open_directory(base)
    exports_fd = None
    try:
        metadata = os.fstat(base_fd)
        _require(metadata.st_uid == OWNER_UID and metadata.st_gid == observer_gid
                 and stat.S_IMODE(metadata.st_mode) == 0o750, "unsafe_path")
        exports_fd = os.open("exports", DIRECTORY_FLAGS, dir_fd=base_fd)
        metadata = os.fstat(exports_fd)
        _require(metadata.st_uid == OWNER_UID and metadata.st_gid == observer_gid
                 and stat.S_IMODE(metadata.st_mode) == 0o750, "unsafe_path")
        return exports_fd
    except BaseException:
        if exports_fd is not None:
            os.close(exports_fd)
        raise
    finally:
        os.close(base_fd)


def _validate_exports(directory_fd: int, observer_gid: int) -> tuple[list[str], int]:
    temporary = []
    total = 0
    with os.scandir(directory_fd) as entries:
        for count, entry in enumerate(entries, 1):
            _require(count <= len(EXPORT_NAMES) and entry.name in EXPORT_NAMES, "unsafe_file")
            metadata = entry.stat(follow_symlinks=False)
            limit = MAX_ENDPOINT_BYTES if entry.name.startswith("endpoint.") else MAX_JOB_BYTES
            if entry.name.endswith(".tmp"):
                _regular(metadata, modes=(0o600, 0o640), max_bytes=limit)
                _require(metadata.st_gid in {INITIAL_GID, observer_gid}
                         if stat.S_IMODE(metadata.st_mode) == 0o600 else metadata.st_gid == observer_gid,
                         "unsafe_file")
                temporary.append(entry.name)
            else:
                _regular(metadata, modes=(0o640,), max_bytes=limit, gid=observer_gid)
            total += metadata.st_size
            _require(total <= MAX_EXPORT_BYTES, "too_large")
    return temporary, total


def publish_endpoint(base: Path, snapshot: dict[str, Any], observer_gid: int) -> None:
    _require(isinstance(snapshot, dict), "invalid_json")
    try:
        value = (json.dumps(snapshot, separators=(",", ":"), sort_keys=True, ensure_ascii=False,
                            allow_nan=False) + "\n").encode("utf-8")
    except (UnicodeError, TypeError, ValueError, RecursionError):
        raise StoreError("invalid_json") from None
    _require(len(value) <= MAX_ENDPOINT_BYTES, "too_large")
    directory_fd = None
    temporary_fd = None
    try:
        directory_fd = _open_exports(base, observer_gid)
        try:
            fcntl.flock(directory_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise StoreError("busy") from None
        temporary, _ = _validate_exports(directory_fd, observer_gid)
        for name in temporary:
            os.unlink(name, dir_fd=directory_fd)
        _, total = _validate_exports(directory_fd, observer_gid)
        _require(total + len(value) <= MAX_EXPORT_BYTES, "too_large")
        temporary_fd = os.open("endpoint.json.tmp", os.O_CREAT | os.O_EXCL | os.O_WRONLY
                               | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600, dir_fd=directory_fd)
        os.fchmod(temporary_fd, 0o600)
        metadata = os.fstat(temporary_fd)
        _regular(metadata, modes=(0o600,), max_bytes=MAX_ENDPOINT_BYTES, gid=INITIAL_GID)
        offset = 0
        while offset < len(value):
            written = os.write(temporary_fd, value[offset:])
            _require(written > 0, "io")
            offset += written
        os.fchown(temporary_fd, -1, observer_gid)
        os.fchmod(temporary_fd, 0o640)
        os.fsync(temporary_fd)
        os.close(temporary_fd)
        temporary_fd = None
        os.replace("endpoint.json.tmp", "endpoint.json", src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    except OSError as error:
        raise _os_error(error) from None
    finally:
        if temporary_fd is not None:
            os.close(temporary_fd)
        if directory_fd is not None:
            os.close(directory_fd)


def read_endpoint(base: Path, observer_gid: int) -> dict[str, Any]:
    directory_fd = None
    try:
        directory_fd = _open_exports(base, observer_gid)
        _validate_exports(directory_fd, observer_gid)
        return decode_json(_read_at(directory_fd, "endpoint.json", modes=(0o640,),
                                   max_bytes=MAX_ENDPOINT_BYTES, gid=observer_gid))
    except OSError as error:
        raise _os_error(error) from None
    finally:
        if directory_fd is not None:
            os.close(directory_fd)
