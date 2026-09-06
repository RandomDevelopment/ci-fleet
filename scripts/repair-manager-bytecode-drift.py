#!/usr/bin/env python3
"""Repair a manager release changed only by Python bytecode caches."""

import argparse
import fcntl
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile


EXCLUDED = {".ci-fleet-engine-ref", ".ci-fleet-tree-sha256"}


def fail(message: str) -> "NoReturn":
    raise SystemExit(f"ERROR: {message}")


def release_digest(root: Path) -> str:
    digest = hashlib.sha256()

    def add(kind: bytes, relative: str, mode: int, payload: bytes = b"") -> None:
        digest.update(kind)
        digest.update(b"\0")
        digest.update(relative.encode("utf-8", "surrogateescape"))
        digest.update(b"\0")
        digest.update(f"{mode:o}".encode("ascii"))
        digest.update(b"\0")
        digest.update(payload)
        digest.update(b"\0")

    def visit(directory: Path) -> None:
        for entry in sorted(os.scandir(directory), key=lambda item: item.name):
            path = Path(entry.path)
            relative = os.path.relpath(path, root)
            if relative in EXCLUDED:
                continue
            metadata = entry.stat(follow_symlinks=False)
            mode = stat.S_IMODE(metadata.st_mode)
            if stat.S_ISDIR(metadata.st_mode):
                add(b"directory", relative, mode)
                visit(path)
            elif stat.S_ISREG(metadata.st_mode):
                add(b"file", relative, mode, hashlib.sha256(path.read_bytes()).digest())
            elif stat.S_ISLNK(metadata.st_mode):
                add(b"symlink", relative, mode, os.fsencode(os.readlink(path)))
            else:
                fail(f"unsupported release entry: {relative}")

    visit(root)
    return digest.hexdigest()


def cache_inventory(root: Path) -> tuple[tuple[str, tuple[tuple[str, int, int, str], ...]], ...]:
    caches = []
    for directory, names, _ in os.walk(root, topdown=True, followlinks=False):
        parent = Path(directory)
        for name in list(names):
            path = parent / name
            if name != "__pycache__":
                continue
            metadata = path.lstat()
            if not stat.S_ISDIR(metadata.st_mode):
                fail(f"unsafe cache path: {path.relative_to(root)}")
            entries = []
            for entry in sorted(os.scandir(path), key=lambda item: item.name):
                item = Path(entry.path)
                item_metadata = entry.stat(follow_symlinks=False)
                if not stat.S_ISREG(item_metadata.st_mode) or not entry.name.endswith(".pyc"):
                    fail(f"unsafe cache entry: {item.relative_to(root)}")
                entries.append(
                    (
                        str(item.relative_to(root)),
                        stat.S_IMODE(item_metadata.st_mode),
                        item_metadata.st_size,
                        hashlib.sha256(item.read_bytes()).hexdigest(),
                    )
                )
            if not entries:
                fail(f"empty cache directory is not repairable: {path.relative_to(root)}")
            caches.append((str(path.relative_to(root)), tuple(entries)))
            names.remove(name)
    return tuple(caches)


def remove_inventory(root: Path, inventory: tuple[tuple[str, tuple[tuple[str, int, int, str], ...]], ...]) -> None:
    for cache, entries in inventory:
        for relative, _, _, _ in entries:
            (root / relative).unlink()
        (root / cache).rmdir()


def resolve_release(current: Path) -> tuple[Path, str, str]:
    if not current.is_symlink():
        fail("manager current pointer is not a symlink")
    releases = (current.parent / "releases").resolve(strict=True)
    target = current.resolve(strict=True)
    if target.parent != releases:
        fail("manager current pointer target is invalid")
    marker = target / ".ci-fleet-engine-ref"
    stored = target / ".ci-fleet-tree-sha256"
    if marker.is_symlink() or stored.is_symlink() or not marker.is_file() or not stored.is_file():
        fail("manager release markers are invalid")
    manager_ref = marker.read_text(encoding="ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", manager_ref):
        fail("manager release marker is invalid")
    expected = stored.read_text(encoding="ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        fail("manager release digest marker is invalid")
    return target, manager_ref, expected


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("manager_current", nargs="?", default="/opt/ci-fleet/manager/current")
    parser.add_argument("--lock-file", default="/run/ci-fleet-installer.lock")
    args = parser.parse_args()

    lock_path = Path(args.lock_file)
    inherited_lock_fd = os.environ.get("CI_FLEET_INSTALLER_LOCK_FD")
    if inherited_lock_fd:
        if inherited_lock_fd != "9":
            fail("inherited installer lock must use file descriptor 9")
        try:
            if Path("/proc/self/fd/9").resolve(strict=True) != lock_path.resolve(strict=True):
                fail("inherited installer lock does not match the configured lock file")
        except OSError:
            fail("inherited installer lock does not match the configured lock file")
        lock_fd = 9
    else:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fail("inherited installer lock is unavailable" if inherited_lock_fd else "installer lock is held")

    current = Path(args.manager_current)
    target, manager_ref, expected = resolve_release(current)
    if release_digest(target) == expected:
        print(f"BYTECODE_REPAIR NO_CHANGE manager_ref={manager_ref}")
        return

    inventory = cache_inventory(target)
    if not inventory:
        fail("manager release drift is not Python bytecode cache drift")
    with tempfile.TemporaryDirectory(prefix=".bytecode-repair.", dir=target.parent.parent) as temporary:
        candidate = Path(temporary) / target.name
        shutil.copytree(target, candidate, symlinks=True, copy_function=shutil.copy2)
        remove_inventory(candidate, cache_inventory(candidate))
        if release_digest(candidate) != expected:
            fail("manager release has drift beyond Python bytecode caches")

    if cache_inventory(target) != inventory:
        fail("manager bytecode cache changed during verification")
    remove_inventory(target, inventory)
    if release_digest(target) != expected:
        fail("manager release digest did not recover")
    count = sum(len(entries) for _, entries in inventory)
    print(f"BYTECODE_REPAIR REPAIRED manager_ref={manager_ref} files={count}")


if __name__ == "__main__":
    main()
