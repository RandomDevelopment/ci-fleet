#!/usr/bin/env python3
"""Conventional Commits 1.0.0 + Semantic Versioning 2.0.0 enforcement for ci-fleet.

This validator covers the contributor-facing commit/PR-title contract. It is
intentionally dependency-free (stdlib only) and deterministic so it can run in
any environment that has Python 3.7+.

Responsibilities:
  * validate one or more commit messages against Conventional Commits 1.0.0
  * validate a PR title as a single conventional commit subject
  * validate SemVer 2.0.0 version strings (with optional leading "v")
  * suggest the next SemVer bump from a commit range (release gate helper)

It does NOT write or publish versions. The project is pre-1.0 (0.y.z); see
docs/CONTRIBUTING.md for the release gate and the meaning of 0.y.z.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from typing import Iterable

# ---------------------------------------------------------------------------
# Conventional Commits 1.0.0 grammar
#
#   <type>[optional scope][!]: <description>
#
#   body?  (blank line separator)
#   footer*  (token: value, or "BREAKING CHANGE: ...")
#
# The project-approved type set (lowercase, as used across the existing history):
ALLOWED_TYPES = frozenset({
    "build",
    "chore",
    "ci",
    "docs",
    "feat",
    "fix",
    "perf",
    "refactor",
    "revert",
    "style",
    "test",
})

# A scope is optional and nested in parentheses. Keep the character class broad
# but disallow parentheses/newlines to avoid structural ambiguity.
SCOPE = r"[a-zA-Z0-9_ -]+"
SUBJECT = r"^(.{1,100})$"
FOOTER_TOKEN = r"[A-Z][A-Z0-9_]+"
FOOTER_VALUE = r"[^\n]+"
# "BREAKING CHANGE:" must be exactly uppercase (CC 1.0.0).
BREAKING_HEADER = "BREAKING CHANGE:"
# Trailers use the "Token: value" form.
FOOTER_LINE = re.compile(rf"^{FOOTER_TOKEN}: {FOOTER_VALUE}$")

# Full conventional-commit header regex (not multiline; applied per message).
CONVENTIONAL_HEADER = re.compile(
    r"^(" + "|".join(sorted(ALLOWED_TYPES)) + r")(?:\((" + SCOPE + r"\))|" + r")(!)?: .+$",
    re.UNICODE,
)

# Git trailers (e.g. "Reviewed-by: ...", "Signed-off-by: ...") and the
# BREAKING CHANGE trailer. Trailers are optional.
TRAILER_RE = re.compile(
    r"^(?:" + FOOTER_TOKEN + r": " + FOOTER_VALUE + r"|" + re.escape(BREAKING_HEADER) + r" " + FOOTER_VALUE + r")$"
)

# Merge and pure-git commits are exempt: they are generated, not authored per
# the contract, and existing base-branch commits are never re-checked here.
MERGE_RE = re.compile(r"^Merge ", re.IGNORECASE)
# A commit with no body/footer that is purely a git plumbing line.
PLUMBING_RE = re.compile(
    r"^(?:Merge (?:pull request|remote-tracking branch|branch)|"
    r"Revert \"|chore\(deps\):)",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Semantic Versioning 2.0.0
#
#   <major>.<minor>.<patch>[-<prerelease>][+<build>]
#   major, minor, patch are non-negative integers without leading zeroes.
#   prerelease: dot-separated identifiers of [0-9A-Za-z-].
#   build:      dot-separated identifiers of [0-9A-Za-z-].
#
# An optional leading "v" is permitted as a tag decoration; the version payload
# itself must be valid SemVer.
SEMVER_RE = re.compile(
    r"^v?"
    r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


def is_semver(value: str) -> bool:
    """Return True when `value` is a valid SemVer 2.0.0 string."""
    return SEMVER_RE.match(value) is not None


def parse_version(value: str) -> tuple[int, int, int] | None:
    """Return (major, minor, patch) for a valid SemVer string, else None."""
    match = SEMVER_RE.match(value)
    if match is None:
        return None
    major, minor, patch = match.group(1), match.group(2), match.group(3)
    return int(major), int(minor), int(patch)


def is_zero_major(version: str) -> bool:
    """0.y.z means initial development with an unstable API."""
    parsed = parse_version(version)
    if parsed is None:
        return False
    return parsed[0] == 0


def bump_kind(message: str) -> str | None:
    """Classify a single conventional commit for SemVer bump selection.

    Returns "MAJOR", "MINOR", or "PATCH", or None when the commit is not a
    conventional change (e.g. a merge or refactor-only commit carries no bump).
    """
    header = message.splitlines()[0] if message else ""
    if PLUMBING_RE.match(header):
        return None
    if not is_conventional_header(header):
        return None
    if has_breaking_change(message) or "!" in header:
        return "MAJOR"
    if header.split(":", 1)[0].split("(")[0] == "feat":
        return "MINOR"
    return "PATCH"


def suggest_bump(messages: Iterable[str]) -> str:
    """Recommend a SemVer bump from a list of conventional commit messages."""
    kinds = [k for message in messages if (k := bump_kind(message))]
    if not kinds:
        return "PATCH"
    if "MAJOR" in kinds:
        return "MAJOR"
    if "MINOR" in kinds:
        return "MINOR"
    return "PATCH"


def is_conventional_header(header: str) -> bool:
    """Validate a single commit subject line against the conventional grammar."""
    if not header or len(header) > 100:
        return False
    return bool(CONVENTIONAL_HEADER.match(header))


def has_breaking_change(message: str) -> bool:
    """Detect a BREAKING CHANGE footer (case-sensitive per spec)."""
    if not message:
        return False
    lines = message.splitlines()
    in_footer = False
    for line in lines[1:]:
        if line.strip() == "":
            in_footer = True
            continue
        if in_footer:
            trailer = TRAILER_RE.match(line)
            if trailer and line.startswith(BREAKING_HEADER):
                return True
    return False


def validate_message(message: str, *, skip_merge: bool = True) -> list[str]:
    """Validate one commit message against Conventional Commits 1.0.0.

    Returns a list of human-readable error strings (empty == valid).
    """
    errors: list[str] = []
    if not message or not message.strip():
        errors.append("message is empty")
        return errors

    lines = message.splitlines()
    header = lines[0]

    if skip_merge and (MERGE_RE.match(header) or PLUMBING_RE.match(header)):
        return errors

    if not header or not is_conventional_header(header):
        errors.append(
            f"header is not conventional: '{header}'. "
            f"Expected '<type>[scope][!]: <description>' from {sorted(ALLOWED_TYPES)}"
        )
        return errors

    # Body, if present, must follow the header after exactly one blank line.
    if len(lines) > 1 and lines[1].strip() != "":
        errors.append("header must be followed by a blank line before the body")

    return errors


def validate_title(title: str) -> list[str]:
    """Validate a PR title (treated as a single conventional subject)."""
    if not title:
        return ["PR title is empty"]
    if PLUMBING_RE.match(title) or MERGE_RE.match(title):
        return ["PR title must be a conventional commit subject, not a merge/plumbing title"]
    if not is_conventional_header(title):
        return [f"PR title is not conventional: '{title}'"]
    return []


def validate_version(value: str) -> list[str]:
    """Validate a SemVer 2.0.0 version string (optional leading 'v')."""
    if is_semver(value):
        return []
    return [f"not a valid SemVer 2.0.0 version: '{value}'"]


def git_revision_list(base: str | None, head: str, *, workspace: str = ".") -> list[str]:
    """Return commit SHAs in the range base..head (shallow-clone safe).

    Falls back to single-commit resolution when git range semantics are not
    available (shallow clones, single-commit histories).
    """
    if base:
        spec = f"{base}..{head}"
    else:
        spec = head
    result = subprocess.run(
        ["git", "-C", workspace, "rev-list", "--reverse", spec],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    commits = [line for line in result.stdout.splitlines() if line.strip()]
    if not commits:
        # Shallow clone or range resolved to nothing: resolve the head alone.
        resolved = subprocess.run(
            ["git", "-C", workspace, "rev-parse", head],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        sha = resolved.stdout.strip()
        if sha and re.fullmatch(r"[0-9a-f]{40}", sha):
            commits = [sha]
    return commits


def commit_message(workspace: str, sha: str) -> str:
    """Return the raw commit message for `sha` (subject on line 0)."""
    result = subprocess.run(
        ["git", "-C", workspace, "log", "-1", "--format=%B", sha],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    # git log --format=%B ends with a newline; strip the trailing newline.
    return result.stdout.rstrip("\n")


def commit_messages(
    base: str | None,
    head: str,
    *,
    workspace: str = ".",
) -> list[str]:
    """Return the full commit messages for every commit in base..head.

    Falls back to resolving the head SHA alone when the range is empty
    (shallow clones, single-commit histories).
    """
    commits = git_revision_list(base, head, workspace=workspace)
    return [commit_message(workspace, sha) for sha in commits]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base", default=os.environ.get("BASE_SHA"),
        help="base SHA or ref (the commits already on the target branch). "
        "Commits in base..head are validated; base commits are not re-checked.",
    )
    parser.add_argument(
        "--head", default=os.environ.get("HEAD_SHA", "HEAD"),
        help="head SHA or ref to validate (default: HEAD).",
    )
    parser.add_argument(
        "--pr-title", default=os.environ.get("PR_TITLE"),
        help="validate a PR title as a single conventional commit subject",
    )
    parser.add_argument(
        "--message", default=None,
        help="validate a single explicit commit message (read from file when '-' is given)",
    )
    parser.add_argument(
        "--version", default=None,
        help="validate a SemVer 2.0.0 version string (optional leading v)",
    )
    parser.add_argument(
        "--suggest-bump", action="store_true",
        help="print the recommended SemVer bump from the base..head range",
    )
    args = parser.parse_args()

    failures: list[str] = []

    if args.version:
        failures.extend(validate_version(args.version))
        if failures:
            for failure in failures:
                print(f"version: {failure}", file=sys.stderr)
            return 1
        print(f"OK: '{args.version}' is a valid SemVer 2.0.0 version")
        return 0

    if args.message:
        message = sys.stdin.read() if args.message == "-" else open(args.message, encoding="utf-8").read()
        failures.extend(validate_message(message))
        if failures:
            for failure in failures:
                print(f"commit: {failure}", file=sys.stderr)
            return 1
        print("OK: conventional commit message")
        return 0

    if args.pr_title:
        failures.extend(validate_title(args.pr_title))
        if failures:
            for failure in failures:
                print(f"pr title: {failure}", file=sys.stderr)
            return 1
        print("OK: conventional PR title")
        return 0

    if args.suggest_bump:
        messages = commit_messages(args.base, args.head)
        print(f"bump: {suggest_bump(messages)}")
        return 0

    # Default: validate every commit in base..head.
    commits = git_revision_list(args.base, args.head)
    if not commits:
        print("no commits found to validate; pass --base/--head or --message", file=sys.stderr)
        return 1

    for sha in commits:
        message = commit_message(".", sha)
        errors = validate_message(message)
        for error in errors:
            print(f"{sha[:8]}: {error}", file=sys.stderr)
            failures.append(error)

    if failures:
        return 1
    print(f"OK: {len(commits)} commits conform to Conventional Commits 1.0.0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
