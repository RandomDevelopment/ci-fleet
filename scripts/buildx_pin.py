#!/usr/bin/env python3
"""Read the reviewed Buildx package pins and check the installed client."""

import argparse
from pathlib import Path
import re
from typing import NamedTuple


ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = Path("runner/Dockerfile")
PACKAGE_VERSION = re.compile(r"(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){2}-[1-9][0-9]*~debian\.13~trixie")
SHA256 = re.compile(r"[0-9a-f]{64}")
PIN_FIELDS = {"BUILDX_VERSION": PACKAGE_VERSION, "BUILDX_SHA256_AMD64": SHA256, "BUILDX_SHA256_ARM64": SHA256}


class Pins(NamedTuple):
    package_version: str
    amd64: str
    arm64: str

    @property
    def client_version(self) -> str:
        return "v" + self.package_version.split("-", 1)[0]


def pin_pattern(field: str) -> str:
    return rf"(?m)^[ \t]*ARG[ \t]+{field}(?:=(.*))?$"


def parse_pins(dockerfile: str) -> Pins:
    values = []
    for field, pattern in PIN_FIELDS.items():
        matches = re.findall(pin_pattern(field), dockerfile)
        if len(matches) != 1 or not pattern.fullmatch(matches[0]):
            raise ValueError(f"expected exactly one valid {field} in {DOCKERFILE}")
        values.append(matches[0])
    pins = Pins(*values)
    if pins.amd64 == pins.arm64:
        raise ValueError("Buildx architecture checksums unexpectedly match")
    return pins


def require_buildx(output: str, root: Path) -> None:
    expected = parse_pins((root / DOCKERFILE).read_text()).client_version
    fields = output.split()
    if len(fields) < 2 or fields[1] != expected:
        raise ValueError(f"reviewed Docker Buildx {expected} is required")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", metavar="VERSION_OUTPUT")
    mode.add_argument("--version", action="store_true")
    args = parser.parse_args()
    try:
        if args.check is not None:
            require_buildx(args.check, ROOT)
        else:
            pins = parse_pins((ROOT / DOCKERFILE).read_text())
            print(pins.client_version if args.version else " ".join(pins))
    except (OSError, ValueError) as error:
        parser.exit(1, f"error: {error}\n")


if __name__ == "__main__":
    main()
