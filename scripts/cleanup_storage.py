#!/usr/bin/env python3
"""Bound storage on an explicitly admitted, disposable ordinary-CI daemon."""

import argparse
from datetime import datetime
import http.client
import json
import os
import re
import socket
import subprocess
import time
import urllib.request

import cleanup_networks as coordination
from desired_state import parse_env

PREFIX = coordination.LABEL_PREFIX
ADMISSION = "exclusive-fleet-daemon-v1"
AGE_SECONDS = 7 * 24 * 60 * 60
START_PERCENT = 70
TARGET_PERCENT = 65
CACHE_KEEP_BYTES = 10 * 1024**3
MAX_IMAGES = 50
PASS_SECONDS = 120


def docker_usage() -> dict:
    connection = http.client.HTTPConnection("localhost", timeout=30)
    connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.sock.settimeout(30)
    try:
        connection.sock.connect(str(coordination.host_path("/var/run/docker.sock")))
        connection.request("GET", "/system/df?verbose=true")
        response = connection.getresponse()
        body = response.read(64 * 1024**2 + 1)
        if response.status != 200 or len(body) > 64 * 1024**2:
            raise ValueError("Docker storage accounting unavailable or too large")
        value = json.loads(body)
        for field, modern in (("Images", "ImageUsage"), ("Containers", "ContainerUsage"), ("BuildCache", "BuildCacheUsage")):
            # API 1.52+ puts detailed resource lists inside usage objects.
            if field not in value and isinstance(value.get(modern), dict) and "Items" in value[modern]:
                value[field] = value[modern]["Items"]
            if field not in value or (value[field] is not None and not isinstance(value[field], list)):
                raise ValueError("Docker storage accounting omitted resource lists")
            value[field] = value[field] or []
        return value
    finally:
        connection.close()


def disk_percent() -> int:
    root = coordination.docker("info", "--format", "{{.DockerRootDir}}").strip()
    if not root.startswith("/"):
        raise ValueError("Docker root directory is invalid")
    filesystem = os.statvfs(root)
    used = filesystem.f_blocks - filesystem.f_bfree
    total = used + filesystem.f_bavail
    if total <= 0:
        raise ValueError("Docker filesystem has no usable capacity")
    return (used * 100 + total - 1) // total


def protected_images() -> set[str]:
    current = coordination.host_path("/etc/ci-fleet/ci-fleet.env")
    checkpoints = coordination.host_path("/var/lib/ci-fleet/checkpoints")
    files = [current]
    if checkpoints.exists() or checkpoints.is_symlink():
        coordination.trusted_path(checkpoints, directory=True)
        for directory in checkpoints.iterdir():
            coordination.trusted_path(directory, directory=True)
            for name in ("ci-fleet.env", "image-ids.env"):
                path = directory / name
                if path.exists() or path.is_symlink():
                    files.append(path)
    protected = set()
    current_values = None
    for path in files:
        coordination.trusted_path(path.parent, directory=True)
        coordination.trusted_path(path, directory=False)
        values = parse_env(path, allow_unknown=True)
        if path == current:
            current_values = values
        for name in ("CI_FLEET_RUNNER_IMAGE", "CI_FLEET_CONTROLLER_IMAGE",
                     "CI_FLEET_RUNNER_IMAGE_ID", "CI_FLEET_CONTROLLER_IMAGE_ID",
                     "CI_FLEET_CONTROLLER_LIVE_IMAGE_ID"):
            reference = values.get(name)
            if reference and reference != "absent":
                protected.add(reference)
    if not {"CI_FLEET_RUNNER_IMAGE", "CI_FLEET_CONTROLLER_IMAGE"} <= current_values.keys():
        raise ValueError("installed environment omitted rollback image references")
    return protected


def image_reason(image: dict, references: set[str], protected: set[str], now: int) -> str:
    identity = image["Id"]
    aliases = set(image.get("RepoTags") or []) | set(image.get("RepoDigests") or []) | {identity}
    if identity in references or image.get("Containers", -1) != 0:
        return "container-reference-or-unknown-use"
    if aliases & protected:
        return "installed-or-checkpoint-release"
    if (image.get("Labels") or {}).get(f"{PREFIX}retention") == "keep":
        return "explicit-keep"
    created = image.get("Created")
    if type(created) is not int or not 0 < created <= now - AGE_SECONDS:
        return "recent-or-unknown-age"
    return "eligible"


def cache_reason(record: dict, now: int) -> str:
    if record.get("InUse") is not False:
        return "active-or-unknown-use"
    try:
        timestamp = datetime.fromisoformat(record["LastUsedAt"])
        if timestamp.tzinfo is None or timestamp.timestamp() <= 0:
            return "unknown-last-use"
        if timestamp.timestamp() > now - AGE_SECONDS:
            return "recent-last-use"
    except (KeyError, ValueError, TypeError, OverflowError):
        return "unknown-last-use"
    return "eligible-subject-to-cache-reserve"


def mutate(*args: str, timeout: float) -> None:
    marker = coordination.removal_marker(timeout=timeout)
    try:
        process = subprocess.Popen(["docker", *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except OSError:
        marker.unlink()
        coordination.sync_directory(marker.parent)
        raise ValueError("Docker maintenance could not start; no request dispatched") from None
    try:
        with process:
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                raise
            if process.returncode:
                raise subprocess.CalledProcessError(process.returncode, process.args, stdout, stderr)
    except subprocess.CalledProcessError as error:
        if "Error response from daemon:" not in (error.stderr or ""):
            raise ValueError("Docker maintenance result uncertain; marker retained") from None
        marker.unlink()
        coordination.sync_directory(marker.parent)
        raise ValueError("Docker rejected maintenance; no forced removal attempted") from None
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("Docker maintenance result uncertain; marker retained") from None
    else:
        marker.unlink()
        coordination.sync_directory(marker.parent)


def cleanup_expired(*, apply: bool, instance: str, usage: dict) -> None:
    now = int(time.time())
    for item in usage["Containers"]:
        labels = item.get("Labels") or {}
        expires = labels.get(f"{PREFIX}expires-at", "")
        owned = labels.get(f"{PREFIX}managed") == "true" and (not instance or labels.get(f"{PREFIX}instance") == instance)
        if not owned or not expires.isascii() or not expires.isdigit() or int(expires) > now:
            print(f"KEEP container {item['Id']} uncertain-ownership-or-unexpired")
            continue
        identity = item["Id"]
        if item.get("State") not in {"exited", "dead", "created"}:
            print(f"KEEP container {identity} active-or-unknown-state")
        else:
            print(f"{'REMOVE' if apply else 'WOULD_REMOVE'} container {identity} expired={expires}", flush=True)
            if apply:
                mutate("rm", identity, timeout=30)
    for name in coordination.docker("volume", "ls", "-q").splitlines():
        item = json.loads(coordination.docker("volume", "inspect", name))[0]
        labels = item.get("Labels") or {}
        expires = labels.get(f"{PREFIX}expires-at", "")
        owned = labels.get(f"{PREFIX}managed") == "true" and (not instance or labels.get(f"{PREFIX}instance") == instance)
        if not owned or not expires.isascii() or not expires.isdigit() or int(expires) > now:
            print(f"KEEP volume {name} uncertain-ownership-or-unexpired")
            continue
        if coordination.docker("ps", "-aq", "--filter", f"volume={name}").strip():
            print(f"KEEP volume {name} container-reference")
            continue
        print(f"{'REMOVE' if apply else 'WOULD_REMOVE'} volume {name} expired={expires}", flush=True)
        if apply:
            mutate("volume", "rm", name, timeout=30)


class RunStatus:
    """Read-only diagnostics. Run labels never authorize a removal."""

    def __init__(self):
        self.results = {}
        self.token = None
        self.deadline = time.monotonic() + 30

    def get(self, repository: str, run: str, attempt: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or not re.fullmatch(r"[1-9][0-9]*", run) or not re.fullmatch(r"[1-9][0-9]*", attempt):
            return "unknown-missing-run-identity"
        key = (repository, run, attempt)
        if key in self.results:
            return self.results[key]
        remaining = self.deadline - time.monotonic()
        if len(self.results) >= 20 or remaining <= 0:
            return "unknown-lookup-budget"
        status = "unknown-api-unavailable"
        try:
            if self.token is None:
                result = subprocess.run([str(coordination.host_path("/opt/ci-fleet/current/scripts/github-app-token.sh")),
                                         "--env-file", str(coordination.host_path("/etc/ci-fleet/ci-fleet.env"))],
                                        check=True, capture_output=True, text=True, timeout=min(10, remaining))
                self.token = result.stdout.strip()
                if not self.token or "\n" in self.token:
                    raise ValueError("token unavailable")
            request = urllib.request.Request(
                f"https://api.github.com/repos/{repository}/actions/runs/{run}/attempts/{attempt}",
                headers={"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json"})
            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *_args, **_kwargs):
                    return None
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                return "unknown-lookup-budget"
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=min(5, remaining)) as response:
                body = response.read(1024 * 1024 + 1)
            if len(body) > 1024 * 1024:
                raise ValueError("run response too large")
            result = json.loads(body)
            if isinstance(result, dict) and result.get("id") == int(run) and result.get("run_attempt") == int(attempt):
                status = "terminal-run-leftover" if result.get("status") == "completed" else "active-or-unknown-run"
        except (OSError, ValueError, subprocess.SubprocessError):
            self.token = self.token or ""
        self.results[key] = status
        return status


def report_workloads(usage: dict) -> None:
    runs = RunStatus()
    for container in usage["Containers"]:
        labels = container.get("Labels") or {}
        if labels.get("com.docker.compose.project") == "ci-fleet":
            continue
        if not labels.get("com.docker.compose.project") and not labels.get(f"{PREFIX}run-id"):
            continue
        status = runs.get(labels.get(f"{PREFIX}repository", ""), labels.get(f"{PREFIX}run-id", ""), labels.get(f"{PREFIX}run-attempt", ""))
        print(f"REPORT container {container['Id']} state={container.get('State', 'unknown')} {status}; no run-based removal")


def cleanup_storage(*, apply: bool) -> None:
    usage = docker_usage()
    used = disk_percent()
    admission = os.environ.get("CI_FLEET_DOCKER_STORAGE_RETENTION", "")
    if admission not in {"", ADMISSION}:
        raise ValueError("unknown Docker storage retention admission")
    protected = protected_images() if admission else set()
    references = {item["ImageID"] for item in usage["Containers"]}
    now = int(time.time())
    eligible = []
    print(f"HEADROOM Docker used={used}% trigger={START_PERCENT}% target={TARGET_PERCENT}% age=7d cache-reserve={CACHE_KEEP_BYTES}B image-limit={MAX_IMAGES}")
    for image in usage["Images"]:
        reason = image_reason(image, references, protected, now) if admission else "no-reviewed-exclusive-daemon-admission"
        if reason == "eligible":
            eligible.append(image)
        print(f"{'ELIGIBLE' if reason == 'eligible' else 'KEEP'} image {image['Id']} {reason}")
    for record in usage["BuildCache"]:
        reason = cache_reason(record, now) if admission else "no-reviewed-exclusive-daemon-admission"
        print(f"{'ELIGIBLE' if reason.startswith('eligible') else 'KEEP'} build-cache {record['ID']} {reason}")
    if not apply or not admission or used < START_PERCENT:
        print("KEEP storage dry-run-or-no-admission-or-below-trigger")
        return
    if any((item.get("Labels") or {}).get(coordination.PROXY_PROTOCOL_LABEL) != "v2"
           for item in usage["Containers"] if item.get("State") in {"running", "restarting", "paused"}):
        print("DEFER storage cleanup old-or-uncoordinated-proxy")
        return
    deadline = time.monotonic() + PASS_SECONDS
    for image in sorted(eligible, key=lambda item: item["Created"])[:MAX_IMAGES]:
        if disk_percent() <= TARGET_PERCENT or time.monotonic() >= deadline:
            break
        identity = image["Id"]
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", identity):
            raise ValueError("invalid Docker image identity")
        aliases = [tag for tag in (image.get("RepoTags") or []) if tag != "<none>:<none>"]
        if not aliases:
            aliases = image.get("RepoDigests") or []
        if any(not re.fullmatch(r"[A-Za-z0-9._/:@\[\]-]+", tag) or tag.startswith("-") for tag in aliases):
            raise ValueError("invalid Docker image reference")
        mutate("image", "rm", "--no-prune", "--", *(aliases or [identity]), timeout=max(0.1, deadline - time.monotonic()))
        print(f"REMOVE image {identity}", flush=True)
    cache_reasons = [cache_reason(record, now) for record in usage["BuildCache"]]
    cache_unknown = any(reason in {"unknown-last-use", "active-or-unknown-use"} and record.get("InUse") is not True
                        for record, reason in zip(usage["BuildCache"], cache_reasons))
    if disk_percent() > TARGET_PERCENT and time.monotonic() < deadline and not cache_unknown and "eligible-subject-to-cache-reserve" in cache_reasons:
        # Engine GC skips in-use cache; the admitted daemon owns the entire default builder cache.
        mutate("builder", "prune", "--all", "--force", "--filter", "until=168h", "--keep-storage", str(CACHE_KEEP_BYTES),
               timeout=max(0.1, deadline - time.monotonic()))
        print("REMOVE eligible default-builder cache subject to age and reserve", flush=True)
    after = disk_percent()
    print(f"HEADROOM Docker after={after}% target={TARGET_PERCENT}%")
    if after > TARGET_PERCENT:
        print("DEFER storage headroom-insufficient; retain protected resources and investigate growth")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--instance", default=os.environ.get("CI_FLEET_INSTANCE", ""))
    args = parser.parse_args()
    report_workloads(docker_usage())

    def maintenance(apply):
        cleanup_expired(apply=apply, instance=args.instance, usage=docker_usage())
        cleanup_storage(apply=apply)

    coordination.cleanup_networks(apply=args.apply, instance=args.instance, maintenance=maintenance)
    if not args.apply:
        print("DRY_RUN no changes made; pass --apply to remove eligible resources")


if __name__ == "__main__":
    try:
        main()
    except coordination.MaintenanceDeferred as error:
        print(f"DEFER {error}")
    except (KeyError, TypeError, ValueError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(f"ERROR fleet cleanup failed: {error}") from error
