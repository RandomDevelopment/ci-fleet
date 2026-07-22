#!/usr/bin/env python3
"""Fail when tracked files contain high-confidence credential material."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERN = re.compile(
    rb"BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY|"
    rb"github_pat_[A-Za-z0-9_]{20,}|"
    rb"gh[opusr]_[A-Za-z0-9]{20,}|"
    rb"AKIA[0-9A-Z]{16}|"
    rb"(?:postgres|mysql|mongodb(?:\+srv)?|redis)://[^\s/:]+:[^\s/@]+@"
)
assert all(PATTERN.search(b"BEGIN " + kind + b"PRIVATE KEY") for kind in (b"", b"RSA ", b"DSA ", b"EC ", b"OPENSSH ", b"ENCRYPTED "))
assert all(PATTERN.search(prefix + b"x" * 20) for prefix in (b"gho_", b"ghp_", b"ghr_", b"ghs_", b"ghu_"))
assert PATTERN.search(b"AKIA" + b"A" * 16)
assert PATTERN.search(b"mysql" + b"://fixture-user:***@example.invalid/database")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=ROOT)
    revision = parser.add_mutually_exclusive_group()
    revision.add_argument("--commit", help="immutable commit whose blobs should be scanned")
    revision.add_argument("--commit-range", help="immutable base..head range whose commit trees should be scanned")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repository = args.repository.resolve()
    git_root = Path(
        subprocess.run(
            ["git", "-C", str(repository), "rev-parse", "--show-toplevel"],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.strip()
    ).resolve()
    git_prefix = repository.relative_to(git_root).as_posix()
    if args.commit is not None and not re.fullmatch(r"[0-9a-f]{40}", args.commit):
        print("--commit must be a full lowercase commit SHA", file=sys.stderr)
        return 2
    if args.commit_range is not None and not re.fullmatch(r"[0-9a-f]{40}\.\.[0-9a-f]{40}", args.commit_range):
        print("--commit-range must be two full lowercase commit SHAs separated by ..", file=sys.stderr)
        return 2
    commits: list[str | None] = [args.commit]
    if args.commit_range:
        commits = subprocess.run(
            ["git", "-C", str(repository), "rev-list", "--reverse", args.commit_range],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.splitlines()
    findings: list[str] = []
    for commit in commits:
        listing = ["git", "-C", str(repository)]
        listing += ["ls-tree", "-rz", "--name-only", commit] if commit else ["ls-files", "-z"]
        tracked = subprocess.run(listing, check=True, stdout=subprocess.PIPE).stdout.split(b"\0")
        for raw in tracked:
            if not raw:
                continue
            relative = raw.decode("utf-8")
            object_path = relative if git_prefix == "." else f"{git_prefix}/{relative}"
            revision = f"{commit}:{object_path}" if commit else f":{object_path}"
            data = subprocess.run(
                ["git", "-C", str(repository), "cat-file", "blob", revision],
                check=True,
                stdout=subprocess.PIPE,
            ).stdout
            for match in PATTERN.finditer(data):
                line = data.count(b"\n", 0, match.start()) + 1
                prefix = f"{commit}:" if args.commit_range else ""
                findings.append(f"{prefix}{relative}:{line}")
    if findings:
        print("possible committed secret detected:", file=sys.stderr)
        print("\n".join(findings), file=sys.stderr)
        return 1
    print("OK: no high-confidence secret material in tracked files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
