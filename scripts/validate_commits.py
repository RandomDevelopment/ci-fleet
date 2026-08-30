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
import tempfile
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

# A scope is optional and nested in parentheses. docs/CONTRIBUTING.md requires
# lowercase ASCII scope components; allow hyphens, underscores, and digits
# inside a component but never uppercase letters or spaces.
SCOPE = r"[a-z0-9_-]+(?:/[a-z0-9_-]+)*"
SUBJECT = r"^(.{1,100})$"
FOOTER_TOKEN = r"[A-Z][A-Z0-9_]+"
FOOTER_VALUE = r"[^\n]+"
# "BREAKING CHANGE:" and "BREAKING-CHANGE:" must be exactly uppercase (CC 1.0.0).
BREAKING_HEADER = "BREAKING CHANGE:"
BREAKING_HEADER_ALT = "BREAKING-CHANGE:"
# Trailers use the "Token: value" form.
FOOTER_LINE = re.compile(rf"^{FOOTER_TOKEN}: {FOOTER_VALUE}$")

# Full conventional-commit header regex (not multiline; applied per message).
# Requires exactly one space after the colon and a description beginning with
# a lowercase letter, per CC 1.0.0 and docs/CONTRIBUTING.md.
CONVENTIONAL_HEADER = re.compile(
    r"^(" + "|".join(sorted(ALLOWED_TYPES)) + r")(?:\(" + SCOPE + r"\))?" + r"(!)?: [a-z].*$",
    re.UNICODE,
)

# Git trailers (e.g. "Reviewed-by: ...", "Signed-off-by: ...") and the
# BREAKING CHANGE / BREAKING-CHANGE trailer. Trailers are optional.
#
# Trailer tokens follow git's own grammar (trailing-attrs): a token of three
# or more alphanumerics with an inner hyphen permitted, followed by ": ".
# This accepts conventional trailers like "Reviewed-by" and "Co-authored-by"
# that the uppercase-only FOOTER_TOKEN class would reject, so a footer block
# beginning with them is still recognized as footers by has_breaking_change().
TRAILER_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*[A-Za-z0-9]")
TRAILER_RE = re.compile(
    r"^(?:" + TRAILER_TOKEN_RE.pattern + r"(?:: | #)" + FOOTER_VALUE + r"|"
    + re.escape(BREAKING_HEADER) + r" " + FOOTER_VALUE + r"|"
    + re.escape(BREAKING_HEADER_ALT) + r" " + FOOTER_VALUE + r")$"
)

# Git-generated plumbing commits are exempt: they are produced by git itself,
# not authored per the contract, and existing base-branch commits are never
# re-checked here.
#
# A real merge is verified by parent count (see is_true_merge_commit); the
# subject prefix alone only exempts git's own generated revert form
# `Revert "<sha>"`, where <sha> is a full 40-hex commit id. Any other
# "Revert ..." shape (including `Revert "<subject>"` with arbitrary text) is
# ordinary authored content and must use the approved `revert:` type.
MERGE_RE = re.compile(r"^Merge ", re.IGNORECASE)
# Git's own generated revert subject is `Revert "<original subject>"` with a
# body containing `This reverts commit <40-hex sha>.` (see git-revert(1) and
# the revert instruction in docs/CONTRIBUTING.md). Exempt only that exact
# subject/body pair: the body line carries the proof, so an authored
# `Revert "..."` subject without it still goes through normal validation and
# must use the approved `revert:` type.
REVERT_SUBJECT_RE = re.compile(r'^Revert ".+"$')
# The generated proof line ends with a period for ordinary reverts; for
# merge reverts (`git revert -m 1`) it ends with ", reversing" and is
# followed by a "changes made to <N>." continuation line.
REVERT_BODY_PROOF_RE = re.compile(
    r"^This reverts commit ([0-9a-fA-F]{40})(?:\.$|, reversing$)"
)

def is_true_merge_commit(sha: str, workspace: str = ".") -> bool:
    """Return True if the commit is a true merge commit (has 2+ parents)."""
    result = subprocess.run(
        ["git", "-C", workspace, "rev-list", "--parents", "-n", "1", sha],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    if result.returncode != 0:
        return False
    parts = result.stdout.strip().split()
    # First part is the commit SHA, rest are parents
    return len(parts) >= 3  # commit SHA + at least 2 parents

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
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-((?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


def is_semver(value: str) -> bool:
    """Return True when `value` is a valid SemVer 2.0.0 string."""
    return SEMVER_RE.fullmatch(value) is not None


def parse_version(value: str) -> tuple[int, int, int] | None:
    """Return (major, minor, patch) for a valid SemVer string, else None."""
    match = SEMVER_RE.fullmatch(value)
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


def referenced_revert_commit(message: str) -> str | None:
    """Return the 40-hex sha from git's generated revert proof line, if any.

    Matches both the ordinary form (`...<sha>.`) and the merge-revert form
    (`...<sha>, reversing`).
    """
    match = REVERT_BODY_PROOF_RE.match
    for line in message.splitlines()[1:]:
        found = match(line)
        if found:
            return found.group(1)
    return None


def referenced_commit_exists(sha: str, workspace: str = ".") -> bool:
    """Return True when `sha` resolves to a commit object in `workspace`."""
    result = subprocess.run(
        ["git", "-C", workspace, "cat-file", "-e", sha + "^{commit}"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    return result.returncode == 0


def reverses_commit(revert_sha: str, referenced_sha: str, workspace: str = ".") -> bool:
    """Return True when `revert_sha` applies the inverse of `referenced_sha`."""
    def parents(sha: str) -> list[str]:
        result = subprocess.run(
            ["git", "-C", workspace, "rev-list", "--parents", "-n", "1", sha],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        return result.stdout.strip().split()[1:] if result.returncode == 0 else []

    revert_parents = parents(revert_sha)
    referenced_parents = parents(referenced_sha)
    if len(revert_parents) != 1 or not referenced_parents:
        return False

    target = subprocess.run(
        ["git", "-C", workspace, "rev-parse", revert_sha + "^{tree}"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ).stdout.strip()
    for referenced_parent in referenced_parents:
        patch = subprocess.run(
            ["git", "-C", workspace, "diff", "--binary", referenced_parent, referenced_sha],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        with tempfile.NamedTemporaryFile() as index:
            env = {**os.environ, "GIT_INDEX_FILE": index.name}
            read_tree = subprocess.run(
                ["git", "-C", workspace, "read-tree", revert_parents[0]],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
            )
            applied = subprocess.run(
                ["git", "-C", workspace, "apply", "--cached", "--reverse"],
                input=patch.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
            )
            if read_tree.returncode != 0 or applied.returncode != 0:
                continue
            tree = subprocess.run(
                ["git", "-C", workspace, "write-tree"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
            )
            if tree.returncode == 0 and tree.stdout.strip() == target:
                return True
    return False


def is_git_generated_revert(
    message: str, *, sha: str | None = None, workspace: str = ".",
    verify_reference: bool = False,
) -> bool:
    """Return True only for git's own generated revert form.

    Requires the `Revert "<original subject>"` subject AND the
    `This reverts commit <sha>.` proof line in the body, matching what
    `git revert` produces (docs/CONTRIBUTING.md instructs its use).

    With verify_reference=True the referenced commit must actually exist in
    the repository; this closes the fabricated-reference bypass in range
    validation where a workspace is always available.
    """
    lines = message.splitlines()
    if not lines or not REVERT_SUBJECT_RE.match(lines[0]):
        return False
    if not any(REVERT_BODY_PROOF_RE.match(line) for line in lines[1:]):
        return False
    if verify_reference:
        referenced = referenced_revert_commit(message)
        # An all-zero reference is never a real commit object.
        if (
            referenced is None
            or set(referenced) == {"0"}
            or not referenced_commit_exists(referenced, workspace)
            or sha is None
            or not reverses_commit(sha, referenced, workspace)
        ):
            return False
    return True


def bump_kind(message: str) -> str | None:
    """Classify a single conventional commit for SemVer bump selection.

    Returns "MAJOR", "MINOR", or "PATCH", or None when the commit is not a
    conventional change (e.g. a merge or refactor-only commit carries no bump).
    """
    header = message.splitlines()[0] if message else ""
    if is_git_generated_revert(message):
        return None
    if not is_conventional_header(header):
        return None
    # Check for explicit breaking marker "!" after type/scope (e.g., "feat!: ...")
    match = CONVENTIONAL_HEADER.match(header)
    if match and match.group(2):  # group(2) is the breaking marker "!"
        return "MAJOR"
    if has_breaking_change(message):
        return "MAJOR"
    if header.split(":", 1)[0].split("(")[0] == "feat":
        return "MINOR"
    return "PATCH"


def suggest_bump(messages: Iterable[str]) -> str:
    """Recommend a SemVer bump from a list of conventional commit messages."""
    kinds = []
    for message in messages:
        k = bump_kind(message)
        if k:
            kinds.append(k)
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
    """Detect a BREAKING CHANGE or BREAKING-CHANGE footer (case-sensitive per spec)."""
    if not message:
        return False
    lines = message.splitlines()
    # The footer block is separated from the subject/body by a blank line and
    # begins with a trailer ("Token: value"); footer values may span
    # continuation lines until the next trailer. A "BREAKING CHANGE:"-shaped
    # line glued to body text without that separator is body prose, not a
    # footer.
    try:
        last_blank = len(lines) - 1 - lines[::-1].index("")
    except ValueError:
        return False
    footer = lines[last_blank + 1:]
    if not footer or not TRAILER_RE.match(footer[0]):
        return False
    return any(
        line.startswith(BREAKING_HEADER) or line.startswith(BREAKING_HEADER_ALT)
        for line in footer
        if TRAILER_RE.match(line)
    )


def validate_message(message: str, *, skip_merge: bool = True, sha: str | None = None, workspace: str = ".") -> list[str]:
    """Validate one commit message against Conventional Commits 1.0.0.

    Returns a list of human-readable error strings (empty == valid).
    """
    errors: list[str] = []
    if not message or not message.strip():
        errors.append("message is empty")
        return errors

    lines = message.splitlines()
    header = lines[0]

    if skip_merge:
        if is_git_generated_revert(
            message, sha=sha, workspace=workspace, verify_reference=bool(sha),
        ):
            # Only git's own generated revert form (subject + body proof line)
            # is exempt; anything else must be conventional. When a sha is
            # available (range validation), the referenced commit must exist,
            # so an authored commit cannot forge the proof with a fabricated
            # reference.
            return errors
        if MERGE_RE.match(header):
            # "Merge " prefix alone is not proof: a single-parent commit can be
            # named anything. Only true merges (2+ parents, verified via sha)
            # are exempt. Without a sha we cannot verify, so do not exempt.
            if sha and is_true_merge_commit(sha, workspace):
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
    elif len(lines) > 2 and lines[2].strip() == "":
        # lines[1] is blank and so is lines[2]: more than one separator line.
        errors.append("exactly one blank line must separate the header from the body")

    return errors


def validate_title(title: str) -> list[str]:
    """Validate a PR title (treated as a single conventional subject)."""
    if not title:
        return ["PR title is empty"]
    if REVERT_SUBJECT_RE.match(title) or MERGE_RE.match(title):
        return ["PR title must be a conventional commit subject, not a merge/plumbing title"]
    if not is_conventional_header(title):
        return [f"PR title is not conventional: '{title}'"]
    return []


def is_ancestor(commit: str, ancestor: str, workspace: str = ".") -> bool:
    """Return True when `commit` is reachable from `ancestor` (or equal to it)."""
    result = subprocess.run(
        ["git", "-C", workspace, "merge-base", "--is-ancestor", commit, ancestor],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    return result.returncode == 0


def validate_version(value: str) -> list[str]:
    """Validate a SemVer 2.0.0 version string (optional leading 'v')."""
    if is_semver(value):
        return []
    return [f"not a valid SemVer 2.0.0 version: '{value}'"]


def check_required_bump(
    version: str, base: str, head: str, workspace: str = ".",
) -> list[str]:
    """Reject `version` when it does not implement the required SemVer bump.

    The required bump is computed from the conventional classification of the
    release range base..head (docs/CONTRIBUTING.md release gate: "the SemVer
    bump matches the Conventional Commits classification"). With no prior
    release tag, any version above 0.0.0 satisfies the pre-1.0 gate.
    ponytail: compares only major.minor.patch; prerelease/build metadata of
    the candidate is ignored, upgrade if tag-vs-range metadata ever matters.
    """
    parsed = parse_version(version)
    if parsed is None:
        return []
    prior = latest_release_tag(workspace, base)
    if prior is None:
        return []
    required = suggest_bump(msg for _, msg in commit_messages(base, head, workspace=workspace))
    candidate = parse_version(version)
    assert candidate is not None and prior is not None
    old_major, old_minor, old_patch = prior
    new_major, new_minor, new_patch = candidate

    def _bumped(level: str) -> bool:
        """True when the candidate implements exactly `level` over prior."""
        if level == "MAJOR":
            return new_major > old_major and (new_minor, new_patch) == (0, 0)
        if level == "MINOR":
            # MINOR keeps major and increases minor (0.y.z included).
            return new_major == old_major and new_minor > old_minor and new_patch == 0
        # PATCH: same major.minor, higher patch.
        return (new_major, new_minor) == (old_major, old_minor) and new_patch > old_patch

    satisfied = _bumped(required)
    if not satisfied:
        return [
            f"version '{version}' does not implement the required {required} "
            f"bump over released '{prior[0]}.{prior[1]}.{prior[2]}' for range "
            f"{base}..{head}"
        ]
    return []


def git_revision_list(base: str | None, head: str, *, workspace: str = ".") -> list[str]:
    """Return commit SHAs in the range base..head (shallow-clone safe).

    Falls back to single-commit resolution when git range semantics are not
    available (shallow clones, single-commit histories).
    For workflow_dispatch (empty base), validate only the HEAD commit.
    """
    if base and base.strip():
        spec = f"{base}..{head}"
    else:
        # Empty base (workflow_dispatch): validate only HEAD
        spec = f"{head}^..{head}"
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
) -> list[tuple[str, str]]:
    """Return the full commit messages for every commit in base..head.

    Returns a list of (sha, message) tuples.

    Falls back to resolving the head SHA alone when the range is empty
    (shallow clones, single-commit histories).
    """
    commits = git_revision_list(base, head, workspace=workspace)
    return [(sha, commit_message(workspace, sha)) for sha in commits]


def latest_release_tag(workspace: str = ".", main_ref: str = "origin/main") -> tuple[int, int, int] | None:
    """Return the highest released stable SemVer reachable from main_ref.

    Stable means no prerelease component (prereleases are branch-local and
    never releases per docs/CONTRIBUTING.md). Returns None when no release
    tag exists yet.
    """
    result = subprocess.run(
        ["git", "-C", workspace, "tag", "--list", "--merged", main_ref],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    if result.returncode != 0:
        return None
    best: tuple[tuple[int, int, int], str] | None = None
    for tag in result.stdout.split():
        match = SEMVER_RE.match(tag)
        if match is not None and match.group(4) is None:
            parsed = parse_version(tag)
            assert parsed is not None
            key = (parsed, tag)
            if best is None or key[0] > best[0]:
                best = (parsed, tag)
    return best[0] if best else None


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
        "--tag-commit", default=os.environ.get("TAG_COMMIT"),
        help="commit the version tag points at; with --version, a stable "
        "(non-prerelease) tag must point into the main line (MAIN_REF, "
        "default 'origin/main')",
    )
    parser.add_argument(
        "--main-ref", default=os.environ.get("MAIN_REF", "origin/main"),
        help="ref representing the main line for stable-tag reachability",
    )
    parser.add_argument(
        "--suggest-bump", action="store_true",
        help="print the recommended SemVer bump from the base..head range",
    )
    args = parser.parse_args()

    failures: list[str] = []

    if args.version is not None:
        failures.extend(validate_version(args.version))
        if not failures and args.tag_commit is not None:
            match = SEMVER_RE.match(args.version)
            assert match is not None
            on_main = is_ancestor(args.tag_commit, args.main_ref)
            if match.group(4) is not None:
                # docs/CONTRIBUTING.md reserves prerelease identifiers for
                # branch-local tags; they are never main-sourced.
                if on_main:
                    failures.append(
                        f"prerelease tag '{args.version}' points at a commit "
                        f"reachable from '{args.main_ref}'; prereleases are "
                        "branch-local only (docs/CONTRIBUTING.md)"
                    )
            elif not on_main:
                # Stable tags must point into the main line.
                failures.append(
                    f"stable tag '{args.version}' points at a commit that is "
                    f"not reachable from '{args.main_ref}'; stable versions "
                    "are tagged on main (prereleases may stay branch-local)"
                )
        if (
            not failures
            and args.tag_commit is not None
            and args.base
            and args.head
        ):
            failures.extend(check_required_bump(args.version, args.base, args.head))
        if failures:
            for failure in failures:
                print(f"version: {failure}", file=sys.stderr)
            return 1
        print(f"OK: '{args.version}' is a valid SemVer 2.0.0 version")
        return 0

    if args.message is not None:
        message = sys.stdin.read() if args.message == "-" else open(args.message, encoding="utf-8").read()
        failures.extend(validate_message(message))
        if failures:
            for failure in failures:
                print(f"commit: {failure}", file=sys.stderr)
            return 1
        print("OK: conventional commit message")
        return 0

    if args.pr_title is not None:
        failures.extend(validate_title(args.pr_title))
        if failures:
            for failure in failures:
                print(f"pr title: {failure}", file=sys.stderr)
            return 1
        print("OK: conventional PR title")
        return 0

    if args.suggest_bump:
        messages = [msg for _, msg in commit_messages(args.base, args.head)]
        print(f"bump: {suggest_bump(messages)}")
        return 0

    # Default: validate every commit in base..head.
    commits = commit_messages(args.base, args.head)
    if not commits:
        print("no commits found to validate; pass --base/--head or --message", file=sys.stderr)
        return 1

    for sha, message in commits:
        errors = validate_message(message, sha=sha, workspace=".")
        for error in errors:
            print(f"{sha[:8]}: {error}", file=sys.stderr)
            failures.append(error)

    if failures:
        return 1
    print(f"OK: {len(commits)} commits conform to Conventional Commits 1.0.0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
