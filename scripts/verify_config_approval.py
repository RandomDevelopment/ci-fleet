#!/usr/bin/env python3
"""Verify GitHub review and check evidence using an independently provisioned policy."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
import re
import signal
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

SHA = re.compile(r"^(?!0{40}$)[0-9a-f]{40}$")
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
POLICY_KEYS = {"schema_version", "configuration_repository_id", "branch", "workflow_id", "required_jobs", "reviewer_ids"}


class Rejected(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise Rejected(message)


def positive_id(value: Any) -> bool:
    return type(value) is int and value > 0


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON keys")
        result[key] = value
    return result


def read_policy(path: Path, owner: int = 0) -> dict[str, Any]:
    # The caller supplies only the fixed host path, never candidate repository data.
    for parent in path.parents:
        metadata = parent.lstat()
        require(stat.S_ISDIR(metadata.st_mode) and metadata.st_uid in {0, owner} and not metadata.st_mode & 0o022,
                "approval policy parent is not trusted")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, encoding="utf-8") as handle:
        metadata = os.fstat(handle.fileno())
        require(stat.S_ISREG(metadata.st_mode) and metadata.st_uid == owner and stat.S_IMODE(metadata.st_mode) == 0o600,
                "approval policy must be a root-owned regular file with mode 0600")
        require(metadata.st_size <= 16384, "approval policy exceeds size limit")
        policy = json.load(handle, object_pairs_hook=unique_object)
    require(isinstance(policy, dict) and set(policy) == POLICY_KEYS, "invalid approval policy keys")
    require(type(policy["schema_version"]) is int and policy["schema_version"] == 1, "unsupported approval policy version")
    for key in ("configuration_repository_id", "workflow_id"):
        require(positive_id(policy[key]), "approval policy IDs must be positive integers")
    branch = policy["branch"]
    require(isinstance(branch, str) and bool(re.fullmatch(r"[A-Za-z0-9_-]+", branch)), "unsupported approval policy branch")
    reviewers = policy["reviewer_ids"]
    require(isinstance(reviewers, list) and 0 < len(reviewers) <= 20 and all(positive_id(item) for item in reviewers),
            "approval policy requires reviewer IDs")
    require(len(set(reviewers)) == len(reviewers), "duplicate reviewer IDs")
    jobs = policy["required_jobs"]
    require(isinstance(jobs, list) and 0 < len(jobs) <= 20
            and all(isinstance(item, str) and 0 < len(item) <= 200 for item in jobs), "approval policy requires job names")
    require(len(set(jobs)) == len(jobs), "duplicate required jobs")
    marker = path.with_name("config-review-required")
    descriptor = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, encoding="utf-8") as handle:
        metadata = os.fstat(handle.fileno())
        require(stat.S_ISREG(metadata.st_mode) and metadata.st_uid == owner and stat.S_IMODE(metadata.st_mode) == 0o600
                and metadata.st_size == 2 and handle.read(3) == "1\n", "approval activation marker is invalid")
    return policy


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: Any, msg: Any, headers: Any, newurl: Any) -> None:
        raise Rejected("GitHub API redirect rejected")


class GitHub:
    def __init__(self, token: str):
        require(bool(token) and "\n" not in token and "\r" not in token, "installation token is unavailable")
        self.token = token
        self.opener = urllib.request.build_opener(NoRedirect())
        self.deadline = time.monotonic() + 120

    def get(self, path: str) -> Any:
        require(path.startswith("/repos/") and not path.startswith("//"), "invalid GitHub API path")
        request = urllib.request.Request("https://api.github.com" + path, headers={
            "Authorization": "Bearer " + self.token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        try:
            remaining = self.deadline - time.monotonic()
            require(remaining > 0, "GitHub API evidence deadline reached")
            with self.opener.open(request, timeout=min(15, remaining)) as response:
                data = response.read(4 * 1024 * 1024 + 1)
            require(len(data) <= 4 * 1024 * 1024, "GitHub API response exceeds size limit")
            return json.loads(data, object_pairs_hook=unique_object)
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise Rejected("GitHub API evidence unavailable or invalid") from error

    def pages(self, path: str, field: str | None = None) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for page in range(1, 21):
            query = "&" if "?" in path else "?"
            result = self.get(f"{path}{query}per_page=100&page={page}")
            if field is not None:
                require(isinstance(result, dict), "invalid GitHub API collection")
                result = result.get(field)
            require(isinstance(result, list) and len(result) <= 100 and all(isinstance(item, dict) for item in result),
                    "invalid GitHub API collection")
            items.extend(result)
            if len(result) < 100:
                return items
        raise Rejected("GitHub API pagination limit reached")


def run_order(run: dict[str, Any]) -> tuple[datetime, int]:
    timestamps = [run.get("created_at"), run.get("run_started_at")]
    if run.get("status") != "completed":
        timestamps.append(run.get("updated_at"))
    parsed = []
    for timestamp in timestamps:
        require(isinstance(timestamp, str), "workflow attempt timestamp is unavailable")
        value = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        require(value.tzinfo is not None, "workflow attempt timestamp lacks timezone")
        parsed.append(value)
    return max(parsed), run["id"]


def verify(api: GitHub, policy: dict[str, Any], repository: str, commit: str, tree: str) -> None:
    require(bool(REPOSITORY.fullmatch(repository)) and bool(SHA.fullmatch(commit)) and bool(SHA.fullmatch(tree)),
            "approval requires a repository, full commit SHA, and local tree SHA")
    prefix = f"/repos/{repository}"
    metadata = api.get(prefix)
    require(metadata.get("id") == policy["configuration_repository_id"] and metadata.get("full_name", "").lower() == repository.lower()
            and metadata.get("default_branch") == policy["branch"], "configuration repository does not match trusted policy")
    candidate = api.get(f"{prefix}/git/commits/{commit}")
    require(candidate.get("sha") == commit and candidate.get("tree", {}).get("sha") == tree,
            "local candidate does not match GitHub commit tree")
    associated = api.pages(f"{prefix}/commits/{commit}/pulls")
    matches = [pr for pr in associated if pr.get("merge_commit_sha") == commit and pr.get("merged_at")]
    require(len(matches) == 1 and positive_id(matches[0].get("number")), "candidate must be the unique merged PR result")
    number = matches[0]["number"]
    pr = api.get(f"{prefix}/pulls/{number}")
    require(pr.get("merged") is True and pr.get("state") == "closed" and pr.get("merge_commit_sha") == commit
            and pr.get("base", {}).get("ref") == policy["branch"]
            and pr.get("base", {}).get("repo", {}).get("id") == policy["configuration_repository_id"],
            "candidate is not a merged PR for the trusted base")
    head = pr.get("head", {}).get("sha")
    require(isinstance(head, str) and bool(SHA.fullmatch(head)), "reviewed head SHA is unavailable")
    reviewed = api.get(f"{prefix}/git/commits/{head}")
    require(reviewed.get("sha") == head and reviewed.get("tree", {}).get("sha") == tree,
            "applied tree differs from the reviewed PR head")
    author_id = pr.get("user", {}).get("id")
    require(positive_id(author_id), "PR author identity is unavailable")
    latest: dict[int, dict[str, Any]] = {}
    reviews = api.pages(f"{prefix}/pulls/{number}/reviews")
    require(all(positive_id(review.get("id")) for review in reviews), "invalid review identifiers")
    for review in sorted(reviews, key=lambda item: item["id"]):
        reviewer = review.get("user", {}).get("id")
        state = review.get("state")
        require(state in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED", "COMMENTED", "PENDING"}, "invalid review state")
        if reviewer in policy["reviewer_ids"] and state not in {"COMMENTED", "PENDING"}:
            latest[reviewer] = review
    require(not any(review["state"] == "CHANGES_REQUESTED" for review in latest.values()), "trusted reviewer requested changes")
    require(any(reviewer != author_id and review["state"] == "APPROVED" and review.get("commit_id") == head
                for reviewer, review in latest.items()), "independent exact-head approval is missing")
    runs = api.pages(f"{prefix}/actions/workflows/{policy['workflow_id']}/runs?head_sha={commit}&event=push", "workflow_runs")
    require(bool(runs) and all(positive_id(run.get("id")) for run in runs), "exact-commit workflow is missing")
    run = max(runs, key=run_order)
    require(run.get("workflow_id") == policy["workflow_id"] and run.get("head_sha") == commit
            and run.get("event") == "push" and run.get("head_branch") == policy["branch"]
            and run.get("repository", {}).get("id") == policy["configuration_repository_id"]
            and run.get("status") == "completed" and run.get("conclusion") == "success"
            and positive_id(run.get("run_attempt")), "latest exact-commit workflow did not pass")
    jobs = api.pages(f"{prefix}/actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs", "jobs")
    for required in policy["required_jobs"]:
        matching = [job for job in jobs if job.get("name") == required]
        require(len(matching) == 1 and matching[0].get("status") == "completed" and matching[0].get("conclusion") == "success",
                "required workflow job did not pass")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--tree", required=True)
    args = parser.parse_args()

    def deadline_expired(_signal: int, _frame: Any) -> None:
        raise Rejected("GitHub evidence deadline reached")

    previous_handler = signal.signal(signal.SIGALRM, deadline_expired)
    signal.setitimer(signal.ITIMER_REAL, 120)
    try:
        policy = read_policy(args.policy)
        verify(GitHub(sys.stdin.read(8192).strip()), policy, args.repository, args.commit, args.tree)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        # API responses, policy contents, and authentication material are never logged.
        print("ERROR: configuration approval rejected or evidence unavailable", file=sys.stderr)
        return 2
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
    print("APPROVED: exact configuration tree, independent review, and required checks verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
