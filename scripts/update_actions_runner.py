#!/usr/bin/env python3
"""Update the pinned GitHub Actions runner release and archive checksums."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable


ROOT = Path(__file__).resolve().parents[1]
RELEASES_URL = "https://api.github.com/repos/actions/runner/releases?per_page=100"
VERSION_RE = re.compile(r"(?P<major>[1-9][0-9]*)\.(?P<minor>0|[1-9][0-9]*)\.(?P<patch>0|[1-9][0-9]*)")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
MAX_RELEASE_RESPONSE_BYTES = 10 * 1024 * 1024
ARCHIVE_MARKERS = ("run.sh", "bin/Runner.Listener")

DOCKER_FIELDS = {
    "version": (
        "ARG RUNNER_VERSION=",
        re.compile(r"^(?P<prefix>ARG RUNNER_VERSION=)(?P<value>[^\r\n]+)$", re.MULTILINE),
    ),
    "amd64": (
        "ARG RUNNER_SHA256_AMD64=",
        re.compile(r"^(?P<prefix>ARG RUNNER_SHA256_AMD64=)(?P<value>[^\r\n]+)$", re.MULTILINE),
    ),
    "arm64": (
        "ARG RUNNER_SHA256_ARM64=",
        re.compile(r"^(?P<prefix>ARG RUNNER_SHA256_ARM64=)(?P<value>[^\r\n]+)$", re.MULTILINE),
    ),
}
COMPOSE_VERSION_FIELD = (
    "CI_FLEET_RUNNER_VERSION:-",
    re.compile(
        r"^(?P<prefix>        RUNNER_VERSION: \$\{CI_FLEET_RUNNER_VERSION:-)"
        r"(?P<value>[^}\r\n]+)(?P<suffix>\})$",
        re.MULTILINE,
    ),
)


class UpdateError(RuntimeError):
    """Raised when release data or repository pins fail closed validation."""


@dataclass(frozen=True)
class Asset:
    architecture: str
    name: str
    url: str
    size: int


@dataclass(frozen=True)
class Release:
    version: str
    version_parts: tuple[int, int, int]
    assets: tuple[Asset, ...]


@dataclass(frozen=True)
class Pins:
    version: str
    amd64: str
    arm64: str


@dataclass(frozen=True)
class UpdateResult:
    old_version: str
    new_version: str
    changed: bool


def parse_version(value: str, *, tag: bool = False) -> tuple[int, int, int]:
    candidate = value[1:] if tag and value.startswith("v") else value
    if tag and not value.startswith("v"):
        raise UpdateError(f"release tag does not start with v: {value!r}")
    match = VERSION_RE.fullmatch(candidate)
    if match is None:
        kind = "release tag" if tag else "runner version"
        raise UpdateError(f"malformed {kind}: {value!r}")
    return tuple(int(match.group(part)) for part in ("major", "minor", "patch"))


def expected_asset_url(version: str, architecture: str) -> str:
    return (
        f"https://github.com/actions/runner/releases/download/v{version}/"
        f"actions-runner-linux-{architecture}-{version}.tar.gz"
    )


def _release_assets(release_data: dict[str, object], version: str) -> tuple[Asset, ...]:
    raw_assets = release_data.get("assets")
    if not isinstance(raw_assets, list):
        raise UpdateError("stable release assets are missing or malformed")

    parsed_assets: list[dict[str, object]] = []
    for raw_asset in raw_assets:
        if not isinstance(raw_asset, dict) or not isinstance(raw_asset.get("name"), str):
            raise UpdateError("stable release contains a malformed asset record")
        parsed_assets.append(raw_asset)

    assets: list[Asset] = []
    for architecture in ("x64", "arm64"):
        name = f"actions-runner-linux-{architecture}-{version}.tar.gz"
        matches = [asset for asset in parsed_assets if asset["name"] == name]
        if len(matches) != 1:
            raise UpdateError(f"expected exactly one release asset named {name}, found {len(matches)}")
        raw_asset = matches[0]
        url = raw_asset.get("browser_download_url")
        size = raw_asset.get("size")
        state = raw_asset.get("state")
        expected_url = expected_asset_url(version, architecture)
        if url != expected_url:
            raise UpdateError(f"release asset URL does not match the expected official URL for {name}")
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise UpdateError(f"release asset size is missing or malformed for {name}")
        if state != "uploaded":
            raise UpdateError(f"release asset is not in the uploaded state: {name}")
        assets.append(Asset(architecture=architecture, name=name, url=expected_url, size=size))
    return tuple(assets)


def select_latest_stable(payload: object) -> Release:
    if not isinstance(payload, list):
        raise UpdateError("GitHub releases response is not a list")

    stable: list[tuple[tuple[int, int, int], dict[str, object]]] = []
    for raw_release in payload:
        if not isinstance(raw_release, dict):
            raise UpdateError("GitHub releases response contains a malformed release")
        draft = raw_release.get("draft")
        prerelease = raw_release.get("prerelease")
        if not isinstance(draft, bool) or not isinstance(prerelease, bool):
            raise UpdateError("GitHub release draft or prerelease flag is malformed")
        if draft or prerelease:
            continue
        tag_name = raw_release.get("tag_name")
        if not isinstance(tag_name, str):
            raise UpdateError("stable release tag is missing or malformed")
        stable.append((parse_version(tag_name, tag=True), raw_release))

    if not stable:
        raise UpdateError("GitHub returned no stable actions/runner release")
    latest_parts = max(parts for parts, _ in stable)
    latest_matches = [release_data for parts, release_data in stable if parts == latest_parts]
    if len(latest_matches) != 1:
        raise UpdateError("GitHub returned duplicate stable releases for the latest version")

    latest = latest_matches[0]
    tag_name = latest["tag_name"]
    assert isinstance(tag_name, str)
    version = tag_name[1:]
    return Release(
        version=version,
        version_parts=latest_parts,
        assets=_release_assets(latest, version),
    )


def fetch_releases(opener: Callable[..., BinaryIO] = urllib.request.urlopen) -> object:
    request = urllib.request.Request(
        RELEASES_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "RandomDevelopment-ci-fleet-runner-updater",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with opener(request, timeout=30) as response:
        status = getattr(response, "status", None)
        if status != 200:
            raise UpdateError(f"GitHub releases request returned HTTP {status}")
        data = response.read(MAX_RELEASE_RESPONSE_BYTES + 1)
    if len(data) > MAX_RELEASE_RESPONSE_BYTES:
        raise UpdateError("GitHub releases response exceeded the size limit")
    try:
        return json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise UpdateError("GitHub releases response is not valid JSON") from error


def validate_archive(path: Path) -> None:
    try:
        with tarfile.open(path, mode="r:gz") as archive:
            members = {member.name.removeprefix("./"): member for member in archive.getmembers()}
    except (tarfile.TarError, OSError) as error:
        raise UpdateError(f"downloaded asset is not a valid gzip-compressed tar archive: {path.name}") from error
    missing = [name for name in ARCHIVE_MARKERS if name not in members or not members[name].isfile()]
    if missing:
        raise UpdateError(f"downloaded runner archive is missing required files: {', '.join(missing)}")


def download_and_hash(
    asset: Asset,
    destination: Path,
    opener: Callable[..., BinaryIO] = urllib.request.urlopen,
) -> str:
    request = urllib.request.Request(asset.url, headers={"User-Agent": "RandomDevelopment-ci-fleet-runner-updater"})
    digest = hashlib.sha256()
    downloaded = 0
    with opener(request, timeout=120) as response, destination.open("wb") as output:
        status = getattr(response, "status", None)
        if status != 200:
            raise UpdateError(f"runner archive request returned HTTP {status}: {asset.name}")
        final_url = urllib.parse.urlparse(response.geturl())
        hostname = final_url.hostname or ""
        if final_url.scheme != "https" or not (
            hostname == "github.com" or hostname.endswith(".githubusercontent.com")
        ):
            raise UpdateError(f"runner archive redirected to an unexpected host: {asset.name}")
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                declared_size = int(content_length)
            except ValueError as error:
                raise UpdateError(f"runner archive has a malformed Content-Length: {asset.name}") from error
            if declared_size != asset.size:
                raise UpdateError(f"runner archive Content-Length does not match release metadata: {asset.name}")
        while chunk := response.read(1024 * 1024):
            downloaded += len(chunk)
            if downloaded > asset.size:
                raise UpdateError(f"runner archive exceeds its declared size: {asset.name}")
            digest.update(chunk)
            output.write(chunk)
    if downloaded != asset.size:
        raise UpdateError(f"runner archive size does not match release metadata: {asset.name}")
    checksum = digest.hexdigest()
    if SHA256_RE.fullmatch(checksum) is None:
        raise UpdateError(f"calculated malformed SHA-256 for {asset.name}")
    validate_archive(destination)
    return checksum


def download_release_checksums(release: Release) -> dict[str, str]:
    checksums: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="ci-fleet-runner-update-") as temporary_directory:
        temporary_path = Path(temporary_directory)
        for asset in release.assets:
            checksums[asset.architecture] = download_and_hash(asset, temporary_path / asset.name)
    if set(checksums) != {"x64", "arm64"}:
        raise UpdateError("did not calculate both required runner archive checksums")
    if checksums["x64"] == checksums["arm64"]:
        raise UpdateError("x64 and arm64 runner archives unexpectedly have the same checksum")
    return checksums


def _read_field(text: str, token: str, pattern: re.Pattern[str], label: str) -> str:
    token_count = text.count(token)
    if token_count != 1:
        raise UpdateError(f"expected exactly one {label} token, found {token_count}")
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        raise UpdateError(f"expected exactly one structurally valid {label} field, found {len(matches)}")
    return matches[0].group("value")


def parse_pins(dockerfile: str, compose: str) -> Pins:
    version = _read_field(dockerfile, *DOCKER_FIELDS["version"], "Dockerfile runner version")
    amd64 = _read_field(dockerfile, *DOCKER_FIELDS["amd64"], "Dockerfile amd64 checksum")
    arm64 = _read_field(dockerfile, *DOCKER_FIELDS["arm64"], "Dockerfile arm64 checksum")
    compose_version = _read_field(compose, *COMPOSE_VERSION_FIELD, "Compose runner version")
    parse_version(version)
    if version != compose_version:
        raise UpdateError(
            f"existing runner versions are mismatched: Dockerfile has {version!r}, Compose has {compose_version!r}"
        )
    if SHA256_RE.fullmatch(amd64) is None or SHA256_RE.fullmatch(arm64) is None:
        raise UpdateError("existing runner archive checksum is malformed")
    if amd64 == arm64:
        raise UpdateError("existing x64 and arm64 runner checksums unexpectedly match")
    return Pins(version=version, amd64=amd64, arm64=arm64)


def _replace_field(text: str, pattern: re.Pattern[str], value: str, label: str) -> str:
    def replacement(match: re.Match[str]) -> str:
        return match.group("prefix") + value + (match.groupdict().get("suffix") or "")

    updated, count = pattern.subn(replacement, text)
    if count != 1:
        raise UpdateError(f"expected exactly one replacement for {label}, found {count}")
    return updated


def update_repository(root: Path, release: Release, checksums: dict[str, str]) -> UpdateResult:
    if set(checksums) != {"x64", "arm64"}:
        raise UpdateError("replacement checksums must contain exactly x64 and arm64")
    if any(SHA256_RE.fullmatch(checksum) is None for checksum in checksums.values()):
        raise UpdateError("replacement checksum is malformed")
    if checksums["x64"] == checksums["arm64"]:
        raise UpdateError("replacement x64 and arm64 checksums unexpectedly match")

    docker_path = root / "runner" / "Dockerfile"
    compose_path = root / "deploy" / "compose.yaml"
    dockerfile = docker_path.read_text()
    compose = compose_path.read_text()
    current = parse_pins(dockerfile, compose)
    if release.version_parts < parse_version(current.version):
        raise UpdateError(f"refusing to downgrade runner from {current.version} to {release.version}")

    updated_dockerfile = dockerfile
    for field, value in (
        ("version", release.version),
        ("amd64", checksums["x64"]),
        ("arm64", checksums["arm64"]),
    ):
        updated_dockerfile = _replace_field(
            updated_dockerfile,
            DOCKER_FIELDS[field][1],
            value,
            f"Dockerfile {field}",
        )
    updated_compose = _replace_field(
        compose,
        COMPOSE_VERSION_FIELD[1],
        release.version,
        "Compose runner version",
    )
    updated = parse_pins(updated_dockerfile, updated_compose)
    expected = Pins(version=release.version, amd64=checksums["x64"], arm64=checksums["arm64"])
    if updated != expected:
        raise UpdateError("post-update runner pins are not synchronized")

    changed = updated_dockerfile != dockerfile or updated_compose != compose
    if changed:
        docker_path.write_text(updated_dockerfile)
        compose_path.write_text(updated_compose)
    return UpdateResult(old_version=current.version, new_version=release.version, changed=changed)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, default=ROOT)
    parser.add_argument("--github-output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        release = select_latest_stable(fetch_releases())
        checksums = download_release_checksums(release)
        result = update_repository(args.repository_root.resolve(), release, checksums)
    except (OSError, UpdateError, urllib.error.URLError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    state = "updated" if result.changed else "already current"
    print(f"actions/runner {release.version}: {state}")
    if args.github_output is not None:
        with args.github_output.open("a") as output:
            output.write(f"changed={'true' if result.changed else 'false'}\n")
            output.write(f"version={release.version}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
