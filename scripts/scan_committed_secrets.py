#!/usr/bin/env python3
"""Fail when tracked files contain high-confidence credential material."""

from __future__ import annotations

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
}
PATTERN = re.compile(
    rb"BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY|"
    rb"github_pat_[A-Za-z0-9_]{20,}|"
    rb"ghp_[A-Za-z0-9]{20,}"
)


def main() -> int:
    tracked = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"],
        check=True,
        stdout=subprocess.PIPE,
    ).stdout.split(b"\0")
    findings: list[str] = []
    for raw in tracked:
        if not raw:
            continue
        relative = raw.decode("utf-8")
        if relative in EXCLUDED or relative.endswith(".example"):
            continue
        data = (ROOT / relative).read_bytes()
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
