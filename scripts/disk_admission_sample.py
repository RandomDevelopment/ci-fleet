#!/usr/bin/env python3
"""Narrow host-only disk measurement export; never performs Docker mutations."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time

sys.dont_write_bytecode = True

import endpoint_snapshot_store as store

OUTPUT = Path("/var/lib/ci-fleet/disk-admission")
BUDGET_SECONDS = 12
MAX_BYTES = 16384
ROLES = ("workspace", "docker", "spool")
INSTANCE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def require(condition: bool) -> None:
    if not condition:
        raise ValueError("disk_measurement_unavailable")


def prepare(path: Path, *, create: bool = True) -> int:
    """Pin ancestry; only installer preparation may create the private directory."""
    parent = store._open_directory(path.parent)
    try:
        if create:
            try:
                os.mkdir(path.name, 0o700, dir_fd=parent)
            except FileExistsError:
                pass
        descriptor = os.open(path.name, store.DIRECTORY_FLAGS, dir_fd=parent)
    finally:
        os.close(parent)
    try:
        meta = os.fstat(descriptor)
        require(meta.st_uid == store.OWNER_UID and stat.S_IMODE(meta.st_mode) == 0o700)
        store._no_acl(descriptor, "unsafe_path")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def lock_export(directory: int, *, removing: bool = False) -> None:
    # Sampling never overlaps. Removal may need the already-running old sampler
    # to finish after a successful downgrade; retain a bounded failure outcome.
    deadline = time.monotonic() + (20 if removing else 0)
    while True:
        try:
            fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(0.1, remaining))


def validate_entries(directory: int) -> None:
    for name in os.listdir(directory):
        require(name in {"measurement.json", "measurement.json.tmp"})
        meta = os.stat(name, dir_fd=directory, follow_symlinks=False)
        store._regular(meta, modes=(0o600,), max_bytes=MAX_BYTES, gid=0)
        fd = os.open(name, store.READ_FLAGS, dir_fd=directory)
        try:
            store._no_acl(fd, "unsafe_file")
        finally:
            os.close(fd)


def publish(directory: int, value: dict) -> None:
    data = (json.dumps(value, separators=(",", ":"), allow_nan=False) + "\n").encode()
    require(len(data) <= MAX_BYTES)
    validate_entries(directory)
    try:
        os.unlink("measurement.json.tmp", dir_fd=directory)
    except FileNotFoundError:
        pass
    fd = os.open("measurement.json.tmp", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace("measurement.json.tmp", "measurement.json", src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            os.unlink("measurement.json.tmp", dir_fd=directory)
        except FileNotFoundError:
            pass


def unknown(role: str) -> dict:
    return dict(roles=[role], observed_at=None, capacity_bytes=None, available_bytes=None,
                total_inodes=None, free_inodes=None, state="unknown", reason="measurement_unavailable")


def read_small(path: Path, limit: int = 1024 * 1024) -> str:
    with path.open("rb") as handle:
        value = handle.read(limit + 1)
    require(len(value) <= limit)
    return value.decode("utf-8")


def unescape_mount(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)


def backing_path(mountinfo: str, driver: str, docker_root: str, role_path: str) -> tuple[Path, Path]:
    """Only standard local overlay stores are admitted; never guess a root."""
    mounts = []
    for line in mountinfo.splitlines():
        left, right = line.split(" - ", 1)
        fields, details = left.split(), right.split()
        require(len(fields) >= 6 and len(details) >= 3)
        mountpoint = unescape_mount(fields[4])
        if role_path == mountpoint or role_path.startswith(mountpoint.rstrip("/") + "/"):
            mounts.append((mountpoint, details))
    require(bool(mounts))
    mountpoint, details = max(mounts, key=lambda item: len(item[0]))
    require(mountpoint == "/" and details[0] == "overlay")
    options = dict(option.split("=", 1) for option in details[2].split(",") if "=" in option)
    upper = Path(unescape_mount(options.get("upperdir", "")))
    require(upper.is_absolute() and ".." not in upper.parts)
    if driver == "overlay2":
        base = Path(docker_root) / "overlay2"
        relative = upper.relative_to(base)
        require(len(relative.parts) == 2 and relative.parts[-1] == "diff")
    elif driver == "overlayfs":
        # Docker's containerd snapshotter may live outside DockerRootDir. Obtain
        # its actual tree from kernel evidence, not a /var/lib/containerd guess.
        marker = "io.containerd.snapshotter.v1.overlayfs"
        require(upper.parts.count(marker) == 1)
        index = upper.parts.index(marker)
        require(upper.parts[index + 1] == "snapshots" and len(upper.parts[index:]) == 4
                and upper.parts[-1] == "fs" and upper.parts[-2].isdigit())
        base = Path(*upper.parts[:index + 2])
    else:
        raise ValueError("unsupported_storage_driver")
    return upper, base


def validate_host_mounts(mountinfo: str, driver: str, base: Path) -> None:
    """Allow normal overlay2 merged views, never overrides of writable backing."""
    for line in mountinfo.splitlines():
        left, right = line.split(" - ", 1)
        fields, details = left.split(), right.split()
        require(len(fields) >= 6 and len(details) >= 3)
        target = Path(unescape_mount(fields[4]))
        if base not in target.parents:
            continue
        relative = target.relative_to(base)
        require(driver == "overlay2" and len(relative.parts) == 2
                and relative.parts[1] == "merged" and fields[3] == "/"
                and details[0] == "overlay")
        options = dict(option.split("=", 1) for option in details[2].split(",") if "=" in option)
        require(unescape_mount(options.get("upperdir", "")) == str(base / relative.parts[0] / "diff"))


class Collector:
    def __init__(self) -> None:
        self.deadline = time.monotonic() + BUDGET_SECONDS

    def remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        require(remaining > 0)
        return remaining

    def docker(self, *arguments: str):
        # Fixed local socket, not an inherited remote Docker context. All errors
        # remain local and are reduced to controlled unknown values on export.
        result = subprocess.run(["docker", "--host", "unix:///var/run/docker.sock", *arguments],
                                capture_output=True, timeout=self.remaining(), check=False)
        require(result.returncode == 0 and len(result.stdout) <= 1024 * 1024)
        return json.loads(result.stdout)

    def inspect(self, service: str) -> dict:
        value = self.docker("inspect", "--format",
                            '{"id":{{json .Id}},"pid":{{json .State.Pid}},"running":{{json .State.Running}},'
                            '"started":{{json .State.StartedAt}},"labels":{{json .Config.Labels}},"mounts":{{json .Mounts}}}',
                            "ci-fleet-" + service + "-1")
        require(isinstance(value, dict) and isinstance(value.get("labels"), dict)
                and isinstance(value.get("mounts"), list))
        require(isinstance(value.get("id"), str) and bool(value["id"])
                and isinstance(value.get("started"), str) and bool(value["started"]))
        require(value["running"] is True and type(value["pid"]) is int and value["pid"] > 0)
        for mount in value["mounts"]:
            require(isinstance(mount, dict) and isinstance(mount.get("Destination"), str))
        require(value["labels"].get("com.docker.compose.project") == "ci-fleet"
                and value["labels"].get("com.docker.compose.service") == service)
        return value

    def backing(self, service: str, driver: str, docker_root: str, role_path: str) -> tuple[Path, Path]:
        before = self.inspect(service)
        process = Path("/proc") / str(before["pid"])
        first = read_small(process / "stat", 4096)
        mounts = read_small(process / "mountinfo")
        path, base = backing_path(mounts, driver, docker_root, role_path)
        # The standard runner and proxy images use their writable layers, with
        # no workspace/spool volume. Refuse an unexpected mounted role instead.
        for mount in before["mounts"]:
            destination = mount["Destination"].rstrip("/")
            require(not (role_path == destination or role_path.startswith(destination + "/")))
        last = read_small(process / "stat", 4096)
        require(first.rsplit(")", 1)[1].split()[19] == last.rsplit(")", 1)[1].split()[19])
        after = self.inspect(service)
        require((before["id"], before["pid"], before["started"]) == (after["id"], after["pid"], after["started"]))
        # Reject per-snapshot mount overrides: the standard global store must
        # place future runner writable layers on this same backing filesystem.
        host_mounts = read_small(Path("/proc/self/mountinfo"))
        validate_host_mounts(host_mounts, driver, base)
        require(os.stat(path).st_dev == os.stat(base).st_dev)
        self.remaining()
        return path, base

    def collect(self, instance: str) -> dict:
        boot = read_small(Path("/proc/sys/kernel/random/boot_id"), 128).strip()
        require(bool(re.fullmatch(r"[0-9a-f-]{36}", boot)))
        values = {role: unknown(role) for role in ROLES}
        paths = {}
        try:
            info = self.docker("info", "--format", '{"root":{{json .DockerRootDir}},"driver":{{json .Driver}}}')
            root, driver = info["root"], info["driver"]
            require(isinstance(root, str) and root.startswith("/") and ".." not in Path(root).parts)
            paths["docker"] = Path(root)
            # No runner is required at MIN=0. The controller demonstrates the
            # daemon's global writable-layer store; Scaler creates runner work
            # in that store without workspace mounts or image-declared volumes.
            volumes = self.docker("image", "inspect", "--format", "{{json .Config.Volumes}}", os.environ["CI_FLEET_RUNNER_RUNTIME_IMAGE_ID"])
            require(volumes in (None, {}))
            workspace, base = self.backing("controller", driver, root, "/home/runner/_work")
            spool, spool_base = self.backing("docker-socket-proxy", driver, root, "/tmp")
            require(base == spool_base)
            paths.update(workspace=workspace, spool=spool)
        except (OSError, ValueError, KeyError, TypeError, IndexError, subprocess.SubprocessError):
            pass
        groups = {}
        for role in ROLES:
            try:
                path = paths[role]
                self.remaining()
                descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
                try:
                    fs, meta = os.fstatvfs(descriptor), os.fstat(descriptor)
                finally:
                    os.close(descriptor)
                capacity, available = fs.f_blocks * fs.f_frsize, fs.f_bavail * fs.f_frsize
                require(0 < capacity < 2**63 and 0 <= available <= capacity
                        and 0 < fs.f_files < 2**63 and 0 <= fs.f_ffree <= fs.f_files)
                self.remaining()
                if meta.st_dev in groups:
                    groups[meta.st_dev]["roles"].append(role)
                else:
                    groups[meta.st_dev] = dict(roles=[role], observed_at=int(time.time()),
                        capacity_bytes=capacity, available_bytes=available, total_inodes=fs.f_files,
                        free_inodes=fs.f_ffree, state="measured", reason="measured")
                del values[role]
            except (OSError, ValueError, KeyError):
                pass
        return dict(schema_version=1, boot_id=boot, controller=instance,
                    filesystems=[*groups.values(), *values.values()])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    lifecycle = parser.add_mutually_exclusive_group()
    lifecycle.add_argument("--prepare", action="store_true")
    lifecycle.add_argument("--remove", action="store_true")
    args = parser.parse_args()
    descriptor = None
    try:
        if args.remove and not args.output.exists() and not args.output.is_symlink():
            return 0
        descriptor = prepare(args.output, create=args.prepare)
        # Existing directory preparation only validates protected metadata. It
        # neither changes entries nor needs the sampler's publication lock.
        if args.prepare and not args.remove:
            return 0
        lock_export(descriptor, removing=args.remove)
        if args.remove:
            # Validate the entire allowlist before removing either owned entry.
            validate_entries(descriptor)
            for name in ("measurement.json.tmp", "measurement.json"):
                try:
                    os.unlink(name, dir_fd=descriptor)
                except FileNotFoundError:
                    pass
            args.output.rmdir()
        elif not args.prepare:
            instance = os.environ.get("CI_FLEET_INSTANCE", "")
            require(bool(INSTANCE_PATTERN.fullmatch(instance)))
            value = Collector().collect(instance)
            publish(descriptor, value)
            if any(fs["state"] == "unknown" for fs in value["filesystems"]):
                print("DISK_ADMISSION_SAMPLE unknown")
                return 1
            print("DISK_ADMISSION_SAMPLE measured")
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        print("DISK_ADMISSION_SAMPLE unavailable")
        return 1
    finally:
        if descriptor is not None:
            os.close(descriptor)


if __name__ == "__main__":
    raise SystemExit(main())
