#!/usr/bin/env python3
"""Fail when a tracked Markdown file links to a missing local path or heading."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$")
FENCE = re.compile(r"^\s{0,3}([`~]{3,})")
EXTERNAL_SCHEMES = {"http", "https", "mailto"}


def tracked_markdown() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z", "*.md"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [ROOT / item.decode() for item in result.stdout.split(b"\0") if item]


def anchor(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"[`*_~]", "", text).strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return re.sub(r" +", "-", text)


def prose_lines(text: str):
    fence_character = ""
    fence_length = 0
    for line_number, line in enumerate(text.splitlines(), 1):
        match = FENCE.match(line)
        if match:
            marker = match.group(1)
            if not fence_character:
                fence_character, fence_length = marker[0], len(marker)
            elif marker[0] == fence_character and len(marker) >= fence_length:
                fence_character, fence_length = "", 0
            continue
        if not fence_character:
            yield line_number, line


def anchors(path: Path) -> set[str]:
    found: set[str] = set()
    counts: dict[str, int] = {}
    for _, line in prose_lines(path.read_text(encoding="utf-8")):
        match = HEADING.match(line)
        if not match:
            continue
        heading = match.group(1)
        base = anchor(heading)
        count = counts.get(base, 0)
        found.add(base if count == 0 else f"{base}-{count}")
        counts[base] = count + 1
    return found


def destination(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("<") and raw.endswith(">"):
        return raw[1:-1]
    # Markdown permits an optional quoted title after a whitespace separator.
    return raw.split(maxsplit=1)[0]


def main() -> int:
    failures: list[str] = []
    markdown = tracked_markdown()
    known_anchors: dict[Path, set[str]] = {}

    for source in markdown:
        text = source.read_text(encoding="utf-8")
        for line_number, line in prose_lines(text):
            for match in LINK.finditer(line):
                raw = destination(match.group(1))
                parsed = urlsplit(raw)
                if parsed.scheme in EXTERNAL_SCHEMES or parsed.netloc:
                    continue

                relative = unquote(parsed.path)
                target = source if not relative else (source.parent / relative).resolve()
                try:
                    target.relative_to(ROOT)
                except ValueError:
                    failures.append(f"{source.relative_to(ROOT)}:{line_number}: link escapes repository: {raw}")
                    continue
                if target.is_dir():
                    target /= "README.md"
                if not target.exists():
                    failures.append(f"{source.relative_to(ROOT)}:{line_number}: missing target: {raw}")
                    continue
                if parsed.fragment and target.suffix.lower() == ".md":
                    expected = unquote(parsed.fragment).lower()
                    available = known_anchors.setdefault(target, anchors(target))
                    if expected not in available:
                        failures.append(f"{source.relative_to(ROOT)}:{line_number}: missing heading: {raw}")

    if failures:
        print("Markdown link validation failed:", file=sys.stderr)
        print("\n".join(f"- {failure}" for failure in failures), file=sys.stderr)
        return 1
    print(f"OK: {len(markdown)} tracked Markdown files have valid local links")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
