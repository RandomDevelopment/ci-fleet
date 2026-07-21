#!/usr/bin/env python3
"""Fail when tracked files contain high-confidence credential material."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = {
    "SECURITY.md",
    "docs/SECRETS.md",
    "scripts/scan_committed_secrets.py",
    "scripts/validate.sh",
    "templates/config-repository/scripts/scan_committed_secrets.py",
}
SCANNER_PATHS = {
    "scripts/scan_committed_secrets.py",
    "templates/config-repository/scripts/scan_committed_secrets.py",
}
PATTERN = re.compile(
    rb"BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY|"
    rb"github_pat_[A-Za-z0-9_]{20,}|"
    rb"gh[opusr]_[A-Za-z0-9]{20,}|"
    rb"AKIA[0-9A-Z]{16}|"
    rb"(?:postgres|mysql|mongodb(?:\+srv)?|redis)://[^\s/:]+:[^\s/@]+@"
)
assert all(PATTERN.search(prefix + b"x" * 20) for prefix in (b"gho_", b"ghp_", b"ghr_", b"ghs_", b"ghu_"))
assert PATTERN.search(b"AKIA" + b"A" * 16)
assert PATTERN.search(b"mysql://fixture-user:fixture-password@example.invalid/database")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=ROOT)
    parser.add_argument("--commit", help="immutable commit whose blobs should be scanned")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repository = args.repository.resolve()
    if args.commit is not None and not re.fullmatch(r"[0-9a-f]{40}", args.commit):
        print("--commit must be a full lowercase commit SHA", file=sys.stderr)
        return 2
    listing = ["git", "-C", str(repository)]
    listing += ["ls-tree", "-rz", "--name-only", args.commit] if args.commit else ["ls-files", "-z"]
    tracked = subprocess.run(
        listing,
        check=True,
        stdout=subprocess.PIPE,
    ).stdout.split(b"\0")
    findings: list[str] = []
    for raw in tracked:
        if not raw:
            continue
        relative = raw.decode("utf-8")
        if relative in SCANNER_PATHS or (args.commit is None and relative in EXCLUDED):
            continue
        if args.commit is None:
            data = (repository / relative).read_bytes()
        else:
            data = subprocess.run(
                ["git", "-C", str(repository), "cat-file", "blob", f"{args.commit}:{relative}"],
                check=True,
                stdout=subprocess.PIPE,
            ).stdout
        for match in PATTERN.finditer(data):
            line = data.count(b"\n", 0, match.start()) + 1
            findings.append(f"{relative}:{line}")
    if findings:
        print("possible committed secret detected:", file=sys.stderr)
        print("\n".join(findings), file=sys.stderr)
        return 1
    print("OK: no high-confidence secret material in tracked files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
