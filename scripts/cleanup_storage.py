#!/usr/bin/env python3
"""Bound storage on an explicitly admitted, disposable ordinary-CI daemon."""

import argparse
from datetime import datetime
import http.client
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import time
import urllib.request

import cleanup_networks as coordination
from desired_state import load_engine_capabilities, parse_env
from managed_image_identity import read_bindings

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


def retained_image_release(environment: Path, revision: str, *, current: bool) -> Path:
    install_root = coordination.host_path("/opt/ci-fleet")
    canonical = install_root / "releases" / revision
    pointer = environment.parent / "release-target"
    release = canonical
    captured = not current and (pointer.exists() or pointer.is_symlink())
    if captured:
        coordination.trusted_path(pointer, directory=False)
        raw = pointer.read_bytes()
        if not 2 <= len(raw) <= 4096 or raw.count(b"\n") != 1 or not raw.endswith(b"\n") or b"\0" in raw:
            raise ValueError("retained image source pointer is invalid")
        release = Path(os.fsdecode(raw[:-1]))
    elif current and not canonical.exists():
        for link in (install_root / "current", install_root / "manager/current"):
            if link.is_symlink():
                if link.lstat().st_uid != os.geteuid():
                    raise ValueError("retained image source pointer owner is invalid")
                release = Path(os.readlink(link))
                break
            if link.exists():
                raise ValueError("retained image source pointer is invalid")
        else:
            if (install_root / ".ci-fleet-engine-ref").exists():
                release = install_root
    if (not release.is_absolute() or str(release) != os.path.normpath(str(release))
            or (release != canonical and release != install_root
                and release.parent != install_root / "manager/releases")):
        raise ValueError("retained image source path is invalid")
    # Authenticate the captured context as well as the manifest, including ancestors.
    boundary = coordination.host_path("/")
    for directory in (release, *release.parents):
        coordination.trusted_path(directory, directory=True)
        if directory == boundary:
            break
    else:
        raise ValueError("retained image source is outside the host root")
    marker = release / ".ci-fleet-engine-ref"
    if captured or release != canonical or marker.exists() or marker.is_symlink():
        coordination.trusted_path(marker, directory=False)
        if marker.read_bytes() != (revision + "\n").encode("ascii"):
            raise ValueError("retained image source revision does not match the captured engine")
    return release


def protected_images(usage: dict | None = None) -> set[str]:
    current = coordination.host_path("/etc/ci-fleet/ci-fleet.env")
    checkpoints = coordination.host_path("/var/lib/ci-fleet/checkpoints")
    files = [current]
    if checkpoints.exists() or checkpoints.is_symlink():
        coordination.trusted_path(checkpoints, directory=True)
        for directory in checkpoints.iterdir():
            coordination.trusted_path(directory, directory=True)
            for name in ("ci-fleet.env", "image-env", "image-ids.env", "fresh-image-env", "fresh-image-ids"):
                path = directory / name
                if path.exists() or path.is_symlink():
                    files.append(path)
    protected = set()
    current_values = None
    portable_references = set()
    for path in files:
        coordination.trusted_path(path.parent, directory=True)
        coordination.trusted_path(path, directory=False)
        values = parse_env(path, allow_unknown=True)
        if path == current:
            current_values = values
        if path.name == "fresh-image-env":
            original = path.parent / "fresh-image-ids"
            coordination.trusted_path(original, directory=False)
            captured = parse_env(original, allow_unknown=True)
            if set(captured) != {"CI_FLEET_RUNNER_IMAGE_ID", "CI_FLEET_CONTROLLER_IMAGE_ID"} or any(
                value != "absent" and not re.fullmatch(r"sha256:[0-9a-f]{64}", value) for value in captured.values()
            ):
                raise ValueError("fresh checkpoint original image mappings are missing or invalid")
        for name in ("CI_FLEET_RUNNER_IMAGE", "CI_FLEET_CONTROLLER_IMAGE",
                     "CI_FLEET_RUNNER_IMAGE_DIGEST", "CI_FLEET_CONTROLLER_IMAGE_DIGEST",
                     "CI_FLEET_RUNNER_IMAGE_ID", "CI_FLEET_CONTROLLER_IMAGE_ID",
                     "CI_FLEET_CONTROLLER_LIVE_IMAGE_ID",
                     "CI_FLEET_RUNNER_RUNTIME_IMAGE_ID", "CI_FLEET_CONTROLLER_RUNTIME_IMAGE_ID"):
            if path.name == "fresh-image-env" and name.endswith(("_IMAGE_DIGEST", "_RUNTIME_IMAGE_ID")):
                continue  # Prospective policy is not an original captured image.
            reference = values.get(name)
            if reference and reference != "absent":
                protected.add(reference)
    # Resolve policy semantics from the retained runtime source, never digest syntax.
    for path in files:
        if path.name not in {"ci-fleet.env", "image-env"}:
            continue
        values = parse_env(path, allow_unknown=True)
        revision = values.get("CI_FLEET_ENGINE_REF", "")
        state = (coordination.host_path("/var/lib/ci-fleet/install-state.json")
                 if path == current else path.parent / "install-state.json")
        has_runtime = any(name.endswith("_RUNTIME_IMAGE_ID") for name in values)
        portable = has_runtime
        if revision:
            if not re.fullmatch(r"[0-9a-f]{40}", revision):
                raise ValueError("retained image engine is invalid")
            release = retained_image_release(path, revision, current=path == current)
            manifest = release / "engine-capabilities.json"
            if manifest.exists() or manifest.is_symlink():
                coordination.trusted_path(manifest, directory=False)
                portable = "portable_managed_image_ids" in load_engine_capabilities(manifest)
                if has_runtime and not portable:
                    raise ValueError("runtime binding conflicts with retained source capability")
        if portable:
            binding = read_bindings(state, values)
            for role in binding["roles"].values():
                references = {value for key in ("config_digest", "runtime_image_id", "manifest_digest")
                              if (value := role.get(key))}
                protected.update(references)
                portable_references.update(references)
    if not {"CI_FLEET_RUNNER_IMAGE", "CI_FLEET_CONTROLLER_IMAGE"} <= current_values.keys():
        raise ValueError("installed environment omitted rollback image references")
    if usage is not None:
        aliases = set()
        for image in usage["Images"]:
            aliases.update({image["Id"], *(image.get("RepoTags") or []), *(image.get("RepoDigests") or [])})
        opaque = {reference for reference in protected - portable_references
                  if re.fullmatch(r"sha256:[0-9a-f]{64}", reference)}
        if opaque - aliases:
            raise ValueError("opaque rollback image identities are unresolved in Docker accounting; storage removal deferred")
    return protected


def image_reason(image: dict, references: set[str], protected: set[str], now: int) -> str:
    identity = image["Id"]
    aliases = set(image.get("RepoTags") or []) | set(image.get("RepoDigests") or []) | {identity}
    # Before API v1.51, -1 means use the complete container inventory instead.
    count = image.get("Containers")
    if identity in references or type(count) is not int or count not in {-1, 0}:
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
            print(f"REPORT container {item['Id']} uncertain-ownership-or-unexpired")
            continue
        identity = item["Id"]
        if item.get("State") not in {"exited", "dead", "created"}:
            print(f"KEEP container {identity} active-or-unknown-state")
        else:
            print(f"{'REMOVE' if apply else 'WOULD_REMOVE'} container {identity} expired={expires}", flush=True)
            if apply:
                mutate("rm", identity, timeout=30)
    filters = []
    if apply:
        filters = ["--filter", f"label={PREFIX}managed=true", "--filter", f"label={PREFIX}expires-at"]
        if instance:
            filters += ["--filter", f"label={PREFIX}instance={instance}"]
    for name in coordination.docker("volume", "ls", "-q", *filters).splitlines():
        item = json.loads(coordination.docker("volume", "inspect", name))[0]
        labels = item.get("Labels") or {}
        expires = labels.get(f"{PREFIX}expires-at", "")
        owned = labels.get(f"{PREFIX}managed") == "true" and (not instance or labels.get(f"{PREFIX}instance") == instance)
        if not owned or not expires.isascii() or not expires.isdigit() or int(expires) > now:
            print(f"REPORT volume {name} uncertain-ownership-or-unexpired")
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
        # Leave most of health's 30-second dry-run timeout for Docker inventory.
        self.deadline = time.monotonic() + 5

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
            def deadline_expired(*_args):
                raise TimeoutError("run diagnostic deadline expired")
            previous_handler = signal.signal(signal.SIGALRM, deadline_expired)
            try:
                signal.setitimer(signal.ITIMER_REAL, min(5, remaining))
                with urllib.request.build_opener(NoRedirect()).open(request, timeout=min(5, remaining)) as response:
                    body = response.read(1024 * 1024 + 1)
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
                signal.signal(signal.SIGALRM, previous_handler)
            if len(body) > 1024 * 1024:
                raise ValueError("run response too large")
            result = json.loads(body)
            if isinstance(result, dict) and result.get("id") == int(run) and result.get("run_attempt") == int(attempt):
                status = "terminal-run-leftover" if result.get("status") == "completed" else "active-or-unknown-run"
        except (OSError, ValueError, http.client.HTTPException, subprocess.SubprocessError):
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


def cleanup_storage(*, apply: bool, usage: dict | None = None) -> None:
    if usage is None:
        usage = docker_usage()
    used = disk_percent()
    admission = os.environ.get("CI_FLEET_DOCKER_STORAGE_RETENTION", "")
    if admission not in {"", ADMISSION}:
        raise ValueError("unknown Docker storage retention admission")
    protected = protected_images(usage) if admission else set()
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
            aliases = [digest for digest in (image.get("RepoDigests") or []) if digest != "<none>@<none>"]
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
    diagnostic_usage = docker_usage()
    report_workloads(diagnostic_usage)

    def maintenance(apply):
        cleanup_expired(apply=apply, instance=args.instance, usage=docker_usage() if apply else diagnostic_usage)
        cleanup_storage(apply=apply, usage=None if apply else diagnostic_usage)

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
