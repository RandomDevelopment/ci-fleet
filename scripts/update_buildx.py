#!/usr/bin/env python3
"""Update the reviewed Buildx pins using Docker's signed Debian package index."""

import argparse
import hashlib
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import NamedTuple

from buildx_pin import DOCKERFILE, PACKAGE_VERSION, PIN_FIELDS, ROOT, SHA256, Pins, parse_pins, pin_pattern


REPO_URL = "https://download.docker.com/linux/debian"
SIGNING_KEY = "9DC858229FC7DD38854AE2D88D81803C0EBFCD88"
ARCHITECTURES = ("amd64", "arm64")


class Package(NamedTuple):
    version: str
    path: str
    size: int
    checksum: str


def package_path(version: str, arch: str) -> str:
    return f"dists/trixie/pool/stable/{arch}/docker-buildx-plugin_{version}_{arch}.deb"


def download(url: str, destination: Path) -> None:
    subprocess.run(
        ["curl", "-fsSLo", destination, "--proto", "=https", "--proto-redir", "=https", "--max-time", "120", url],
        check=True,
    )


def fetch_packages(scratch: Path) -> dict[str, Package]:
    key = scratch / "docker.asc"
    download(f"{REPO_URL}/gpg", key)
    sources = scratch / "sources.list"
    sources.write_text(f"deb [arch=amd64,arm64 signed-by={key},{SIGNING_KEY}] {REPO_URL} trixie stable\n")
    lists = scratch / "lists"
    lists.mkdir()
    empty = scratch / "empty"
    empty.mkdir()
    config = scratch / "apt.conf"
    config.write_text(
        f'Dir::Etc::parts "{empty}";\nDir::Etc::main "/dev/null";\nDir::Etc::sourceparts "{empty}";\n'
        f'Dir::Etc::sourcelist "{sources}";\nDir::State::lists "{lists}";\n'
        'Dir::State::status "/dev/null";\nDir::Cache::pkgcache "";\nDir::Cache::srcpkgcache "";\n'
    )
    env = {**os.environ, "APT_CONFIG": str(config)}
    subprocess.run(["apt-get", "update", "--error-on=any"], env=env, check=True, timeout=120)
    packages = {}
    for arch in ARCHITECTURES:
        record = subprocess.check_output(
            ["apt-cache", "--no-all-versions", "show", f"docker-buildx-plugin:{arch}"],
            env=env, text=True, timeout=30,
        )
        fields = dict(re.findall(r"(?m)^([A-Za-z0-9-]+): (.*)$", record))
        version = fields.get("Version", "")
        path = fields.get("Filename", "")
        size = fields.get("Size", "")
        checksum = fields.get("SHA256", "")
        if (
            len(record.strip().split("\n\n")) != 1
            or fields.get("Package") != "docker-buildx-plugin"
            or fields.get("Architecture") != arch
            or not PACKAGE_VERSION.fullmatch(version)
            or path != package_path(version, arch)
            or not re.fullmatch(r"[1-9][0-9]*", size)
            or not SHA256.fullmatch(checksum)
        ):
            raise ValueError(f"invalid Docker Buildx package metadata for {arch}")
        packages[arch] = Package(version, path, int(size), checksum)
    return packages


def update_pins(dockerfile: Path, packages: dict[str, Package], scratch: Path) -> tuple[Pins, bool]:
    amd64, arm64 = (packages[arch] for arch in ARCHITECTURES)
    if amd64.version != arm64.version:
        raise ValueError(f"Buildx versions differ for amd64 ({amd64.version}) and arm64 ({arm64.version})")
    if amd64.checksum == arm64.checksum:
        raise ValueError("Buildx package SHA-256 checksums unexpectedly match")
    new = Pins(amd64.version, amd64.checksum, arm64.checksum)
    text = dockerfile.read_text()
    current = parse_pins(text)
    if new.package_version == current.package_version:
        if new != current:
            raise ValueError("upstream Buildx package checksums changed for the pinned version")
        return new, False
    older = subprocess.run(["dpkg", "--compare-versions", new.package_version, "lt", current.package_version])
    if older.returncode != 1:
        raise ValueError(f"cannot upgrade Buildx from {current.package_version} to {new.package_version}")

    for arch, package in packages.items():
        destination = scratch / f"buildx-{arch}.deb"
        download(f"{REPO_URL}/{package.path}", destination)
        if destination.stat().st_size != package.size:
            raise ValueError(f"{arch} package size mismatch")
        with destination.open("rb") as bundle:
            if hashlib.file_digest(bundle, "sha256").hexdigest() != package.checksum:
                raise ValueError(f"{arch} package SHA-256 mismatch")

    for field, value in zip(PIN_FIELDS, new, strict=True):
        text = re.sub(pin_pattern(field), f"ARG {field}={value}", text)
    dockerfile.write_text(text)
    return new, True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, default=ROOT)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    try:
        with tempfile.TemporaryDirectory(prefix="ci-fleet-buildx-update-") as directory:
            scratch = Path(directory)
            # APT drops privileges for index downloads when run as root.
            scratch.chmod(0o755)
            pins, changed = update_pins(args.repository_root / DOCKERFILE, fetch_packages(scratch), scratch)
        print(f"Docker Buildx {pins.package_version}: {'updated' if changed else 'already current'}")
        if args.github_output is not None:
            with args.github_output.open("a") as output:
                output.write(f"changed={str(changed).lower()}\nversion={pins.package_version}\n")
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, f"error: {error}\n")


if __name__ == "__main__":
    main()
