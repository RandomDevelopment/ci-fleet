#!/usr/bin/env python3
"""Verify GitHub review and check evidence using an independently provisioned policy."""

from __future__ import annotations

import argparse
import base64
import hashlib
import subprocess
import tempfile
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
LOCAL_POLICY_KEYS = {"directory", "public_key", "validator_revision", "suite", "required_checks", "max_age_seconds"}
CODEX_CLEAN = re.compile(
    r"\ACodex Review: Didn't find any major issues\.(?: What shall we delve into next\?| Keep it up!| Nice work!| :tada:)?\n\n"
    r"\*\*Reviewed commit:\*\* `([0-9a-f]{10,40})`\n"
    r"(?:\n(<details>[\s\S]*</details>\s*))?\Z"
)
CODEX_HELP = '''<details> <summary>ℹ️ About Codex in GitHub</summary>
<br/>
[Your team has set up Codex to review pull requests in this repo](https://chatgpt.com/codex/cloud/settings/general).
Reviews are triggered when you
- Open a pull request for review
- Mark a draft as ready
- Comment "@codex review".
If Codex has suggestions, it will comment; otherwise it will react with 👍.
Codex can also answer questions or update the PR. Try commenting "@codex address that feedback".
</details>'''
CODEX_SUMMARY_MARKER = "<!-- codex-pull-request-review-summary -->"
CODEX_SUMMARY_PREFIX = CODEX_SUMMARY_MARKER + '''
## Codex Review Summary
This comment shows the latest Codex review activity on this pull request.
| Review | Status | Commit | Review trigger |
| --- | --- | --- | --- |
'''
CODEX_SUMMARY_HELP = '''<details> <summary>ℹ️ About Codex in GitHub</summary>
<br/>
[Your team has set up Codex to review pull requests in this repo](https://chatgpt.com/codex/cloud/settings/general).
Reviews are triggered when you
- Open a pull request for review
- Mark a draft as ready
- Comment "@codex review" or "@codex security review".
Codex reacts with 👀 while any review is running, comments if it has suggestions,
and reacts with 👍 once all reviews finish with no findings.
</details>'''
CODEX_COMPLETED_ROW = re.compile(
    r'\| 📝 \*\*Code Review\*\* \| ✅ \*\*Completed\*\* <relative-time datetime="(?P<time>[^"]+)">'
    r'(?P=time)</relative-time> \| `(?P<commit>[0-9a-f]{7,40})` \| (?:Manual request|PR opened) \|'
)


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
    require(isinstance(policy, dict), "invalid approval policy")
    version = policy.get("schema_version")
    require(type(version) is int and version in {1, 2}, "unsupported approval policy version")
    required = POLICY_KEYS if version == 1 else (POLICY_KEYS - {"workflow_id", "required_jobs"}) | {"local_validation"}
    optional = {"codex_reviewer_ids"} | ({"workflow_id", "required_jobs"} if version == 2 else set())
    require(required <= set(policy) and set(policy) <= required | optional, "invalid approval policy keys")
    require(positive_id(policy["configuration_repository_id"]), "approval repository ID must be positive")
    require(("workflow_id" in policy) == ("required_jobs" in policy), "hosted validation policy is incomplete")
    if "workflow_id" in policy:
        require(positive_id(policy["workflow_id"]), "workflow ID must be positive")
    if version == 2:
        local = policy["local_validation"]
        require(isinstance(local, dict) and set(local) == LOCAL_POLICY_KEYS, "invalid local validation policy")
        require(isinstance(local["directory"], str) and Path(local["directory"]).is_absolute(), "invalid evidence directory")
        require(isinstance(local["public_key"], str) and len(local["public_key"]) <= 1024
                and local["public_key"].startswith("-----BEGIN PUBLIC KEY-----\n"), "invalid local validation public key")
        public = local["public_key"].splitlines()
        require(len(public) == 3 and public[0] == "-----BEGIN PUBLIC KEY-----"
                and public[2] == "-----END PUBLIC KEY-----", "invalid Ed25519 public key")
        der = base64.b64decode(public[1], validate=True)
        require(len(der) == 44 and der[:12] == bytes.fromhex("302a300506032b6570032100"), "key must be Ed25519")
        require(isinstance(local["validator_revision"], str) and bool(SHA.fullmatch(local["validator_revision"])),
                "local validator revision must be pinned")
        require(isinstance(local["suite"], str) and bool(re.fullmatch(r"[a-z0-9-]{1,100}", local["suite"])), "invalid suite")
        checks = local["required_checks"]
        require(isinstance(checks, list) and 0 < len(checks) <= 100 and all(
            isinstance(check, str) and bool(re.fullmatch(r"[a-z0-9-]{1,100}", check)) for check in checks)
            and len(set(checks)) == len(checks), "invalid required local checks")
        require(type(local["max_age_seconds"]) is int and 60 <= local["max_age_seconds"] <= 604800,
                "invalid local validation maximum age")
    branch = policy["branch"]
    require(isinstance(branch, str) and bool(re.fullmatch(r"[A-Za-z0-9_-]+", branch)), "unsupported approval policy branch")
    reviewers = policy["reviewer_ids"]
    require(isinstance(reviewers, list) and 0 < len(reviewers) <= 20 and all(positive_id(item) for item in reviewers),
            "approval policy requires reviewer IDs")
    require(len(set(reviewers)) == len(reviewers), "duplicate reviewer IDs")
    codex = policy.get("codex_reviewer_ids", [])
    require(isinstance(codex, list) and all(positive_id(item) and item in reviewers for item in codex)
            and len(set(codex)) == len(codex), "Codex reviewer IDs must be unique allowed reviewer IDs")
    if "required_jobs" in policy:
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


def read_installed_commit(path: Path, repository: str, owner: int = 0, controller: str | None = None) -> str | None:
    for parent in path.parents:
        try:
            metadata = parent.lstat()
        except FileNotFoundError:
            continue
        require(stat.S_ISDIR(metadata.st_mode) and metadata.st_uid in {0, owner} and not metadata.st_mode & 0o022,
                "installed state parent is not trusted")
    if not path.exists() and not path.is_symlink():
        return None
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, encoding="utf-8") as handle:
        metadata = os.fstat(handle.fileno())
        require(stat.S_ISREG(metadata.st_mode) and metadata.st_uid == owner and not metadata.st_mode & 0o022
                and metadata.st_size <= 1024 * 1024, "installed state is not trusted")
        if controller is not None:
            require(stat.S_IMODE(metadata.st_mode) == 0o600, "recovery record requires mode 0600")
        installed = json.load(handle, object_pairs_hook=unique_object)
    if controller is not None:
        require(set(installed) == {"config_repository", "config_ref", "controller", "saved_at"}
                and installed["controller"] == controller and type(installed["saved_at"]) is int
                and installed["saved_at"] >= 0, "recovery record does not match the controller")
    require(installed["config_repository"].lower() == repository.lower(), "installed repository identity differs")
    previous = installed["config_ref"]
    require(isinstance(previous, str) and bool(SHA.fullmatch(previous)), "installed configuration SHA is invalid")
    return previous


class GitHub:
    def __init__(self, token: str):
        require(bool(token) and "\n" not in token and "\r" not in token, "installation token is unavailable")
        self.token = token
        self.opener = urllib.request.build_opener(NoRedirect())
        self.deadline = time.monotonic() + 120

    def get(self, path: str, query: str | None = None) -> Any:
        require((query is None and path.startswith("/repos/") and not path.startswith("//"))
                or (query is not None and path == "/graphql"), "invalid GitHub API path")
        request = urllib.request.Request("https://api.github.com" + path,
                                        data=None if query is None else json.dumps({"query": query}).encode(), headers={
            "Authorization": "Bearer " + self.token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
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

    def latest_base_change(self, repository: str, number: int) -> datetime | None:
        owner, name = repository.split("/")
        cursor = None
        latest = None
        for _ in range(20):
            query = '''query { repository(owner: %s, name: %s) { pullRequest(number: %d) {
                timelineItems(first: 100, after: %s, itemTypes: [BASE_REF_CHANGED_EVENT]) {
                    nodes { ... on BaseRefChangedEvent { createdAt } }
                    pageInfo { hasNextPage endCursor }
                }
            } } }''' % (json.dumps(owner), json.dumps(name), number, json.dumps(cursor))
            result = self.get("/graphql", query)
            require(isinstance(result, dict) and not result.get("errors"), "PR base history is unavailable")
            timeline = result["data"]["repository"]["pullRequest"]["timelineItems"]
            nodes = timeline["nodes"]
            require(isinstance(nodes, list) and len(nodes) <= 100, "invalid PR base history")
            for node in nodes:
                changed = evidence_timestamp(node["createdAt"])
                latest = changed if latest is None else max(latest, changed)
            page = timeline["pageInfo"]
            require(type(page.get("hasNextPage")) is bool, "incomplete PR base history")
            if not page["hasNextPage"]:
                return latest
            following = page.get("endCursor")
            require(isinstance(following, str) and bool(following) and following != cursor, "invalid PR base history cursor")
            cursor = following
        raise Rejected("PR base history pagination limit reached")

    def pull_request_nodes(self, repository: str, number: int, connection: str, fields: str,
                           item_type: str | None = None) -> list[dict[str, Any]]:
        owner, name = repository.split("/")
        cursor = None
        items = []
        for _ in range(20):
            query = '''query { repository(owner: %s, name: %s) { pullRequest(number: %d) {
                %s(first: 100, after: %s%s) { nodes { %s } pageInfo { hasNextPage endCursor } }
            } } }''' % (json.dumps(owner), json.dumps(name), number, connection, json.dumps(cursor),
                        "" if item_type is None else ", itemTypes: [" + item_type + "]", fields)
            result = self.get("/graphql", query)
            require(isinstance(result, dict) and not result.get("errors"), "PR review evidence is unavailable")
            collection = result["data"]["repository"]["pullRequest"][connection]
            nodes, page = collection["nodes"], collection["pageInfo"]
            require(isinstance(nodes, list) and len(nodes) <= 100
                    and all(isinstance(node, dict) for node in nodes), "invalid PR review evidence")
            items.extend(nodes)
            require(type(page.get("hasNextPage")) is bool, "incomplete PR review evidence")
            if not page["hasNextPage"]:
                return items
            following = page.get("endCursor")
            require(isinstance(following, str) and bool(following) and following != cursor, "invalid PR review cursor")
            cursor = following
        raise Rejected("PR review evidence pagination limit reached")


def evidence_timestamp(timestamp: Any) -> datetime:
    require(isinstance(timestamp, str), "evidence timestamp is unavailable")
    value = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    require(value.tzinfo is not None, "evidence timestamp lacks timezone")
    return value


def review_order(review: dict[str, Any], head: str) -> tuple[datetime, int, int]:
    # GitHub timestamps have second precision; ambiguous ties must reject approval.
    rejection = {"APPROVED": int(review.get("commit_id") != head), "DISMISSED": 2, "CHANGES_REQUESTED": 3}
    return evidence_timestamp(review.get("submitted_at")), rejection[review["state"]], review["id"]


def run_order(run: dict[str, Any]) -> tuple[datetime, int]:
    timestamps = [run.get("created_at")]
    if run.get("run_started_at") is not None:
        timestamps.append(run["run_started_at"])
    if run.get("status") != "completed":
        timestamps.append(run.get("updated_at"))
    return max(evidence_timestamp(timestamp) for timestamp in timestamps), run["id"]


def completed_codex_summary(api: GitHub, repository: str, comment: dict[str, Any], head: str,
                            submitted: datetime) -> bool:
    state_lines = [line.strip() for line in comment["body"].splitlines() if "codex-review-state" in line.lower()]
    if state_lines:
        match = re.fullmatch(r"<!-- codex-review-state:v1 (.+) -->", state_lines[0])
        if len(state_lines) != 1 or match is None:
            return False
        state = json.loads(match[1], object_pairs_hook=unique_object)
        keys = {"commit_sha", "status", "has_any_findings", "has_unresolved_findings"}
        if (not isinstance(state, dict) or set(state) != {"code_review", "security_review"}
                or not all(isinstance(value, dict) and set(value) == keys for value in state.values())):
            return False
        code, security = state["code_review"], state["security_review"]
        if (code["commit_sha"] != head or code["status"] != "completed"
                or code["has_any_findings"] is not False or code["has_unresolved_findings"] is not False
                or any(value is not None for value in security.values())):
            return False
    rows = [line for line in comment["body"].splitlines() if line.startswith("|")]
    if len(rows) != 3 or rows[:2] != ["| Review | Status | Commit | Review trigger |", "| --- | --- | --- | --- |"]:
        return False
    match = CODEX_COMPLETED_ROW.fullmatch(rows[2])
    body = '\n'.join(line for line in comment["body"].splitlines() if line.strip() not in state_lines)
    return (match is not None and body.split() == (CODEX_SUMMARY_PREFIX + match[0] + '\n' + CODEX_SUMMARY_HELP).split()
            and evidence_timestamp(match['time']) >= submitted
            and api.get(f"/repos/{repository}/commits/{match['commit']}").get("sha") == head)


def clean_codex_review(api: GitHub, policy: dict[str, Any], repository: str, number: int, head: str,
                       author_id: int, reviews: list[dict[str, Any]], base_changed: datetime | None) -> bool:
    pinned = set(policy.get("codex_reviewer_ids", []))
    allowed = pinned - {author_id}
    if not allowed:
        return False
    comments = api.pull_request_nodes(repository, number, "comments",
                                     "databaseId body createdAt lastEditedAt author { __typename ... on Bot { databaseId } }")
    threads = api.pull_request_nodes(repository, number, "reviewThreads", "isResolved")
    require(all(thread.get("isResolved") is True for thread in threads), "unresolved PR review threads")
    deletions = api.pull_request_nodes(repository, number, "timelineItems", "... on CommentDeletedEvent { createdAt }",
                                       "COMMENT_DELETED_EVENT")
    latest_deletion = max((evidence_timestamp(event["createdAt"]) for event in deletions), default=None)
    bot_comments = [comment for comment in comments if isinstance(comment.get("author"), dict)
                    and comment["author"].get("__typename") == "Bot"
                    and comment["author"].get("databaseId") in pinned]
    for reviewer in allowed:
        responses = [comment for comment in bot_comments if comment["author"]["databaseId"] == reviewer]
        decisions = [comment for comment in responses if not comment["body"].startswith(CODEX_SUMMARY_MARKER)]
        if not decisions:
            continue
        # An edited older response is newer evidence, but can never grant approval.
        def comment_time(comment: dict[str, Any]) -> datetime:
            return max(evidence_timestamp(comment["createdAt"]),
                       evidence_timestamp(comment.get("lastEditedAt") or comment["createdAt"]))
        response = max(decisions, key=lambda comment: (comment_time(comment), comment["databaseId"]))
        submitted = evidence_timestamp(response["createdAt"])
        match = CODEX_CLEAN.fullmatch(response["body"])
        if response["lastEditedAt"] is not None or match is None or (base_changed is not None and submitted <= base_changed):
            continue
        if latest_deletion is not None and submitted <= latest_deletion:
            continue
        if match[2] is not None and match[2].split() != CODEX_HELP.split():
            continue
        if any(comment is not response and comment_time(comment) >= submitted
               and (comment in bot_comments or comment.get("lastEditedAt") is not None
                    or re.search(r"(?i)@codex\s+(?:security\s+)?review\b", comment["body"]))
               and not (comment in bot_comments and comment["body"].startswith(CODEX_SUMMARY_MARKER)
                        and completed_codex_summary(api, repository, comment, head, submitted))
               for comment in comments):
            continue
        if any(review.get("user", {}).get("id") in pinned
               and (review["state"] == "PENDING" or evidence_timestamp(review["submitted_at"]) >= submitted)
               for review in reviews):
            continue
        # Resolve abbreviated GitHub output rather than accepting a textual prefix match.
        reported = api.get(f"/repos/{repository}/commits/{match[1]}")
        if reported.get("sha") == head:
            return True
    return False


def policy_digest(policy: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def trusted_evidence(path: Path, owner: int = 0) -> bytes:
    for parent in path.parents:
        metadata = parent.lstat()
        require(stat.S_ISDIR(metadata.st_mode) and metadata.st_uid in {0, owner} and not metadata.st_mode & 0o022,
                "local evidence parent is not trusted")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as handle:
        metadata = os.fstat(handle.fileno())
        require(stat.S_ISREG(metadata.st_mode) and metadata.st_uid == owner
                and stat.S_IMODE(metadata.st_mode) == 0o600 and metadata.st_nlink == 1
                and metadata.st_size <= 1024 * 1024, "local evidence file is not trusted")
        data = handle.read(1024 * 1024 + 1)
        require(len(data) <= 1024 * 1024, "local evidence exceeds size limit")
        return data


def verify_local_evidence(policy: dict[str, Any], commit: str, tree: str, parent: str, head: str,
                          number: int, owner: int = 0, now: int | None = None) -> None:
    local = policy["local_validation"]
    directory = Path(local["directory"])
    # The fixed commit filename and protected path never come from the candidate.
    envelope = json.loads(trusted_evidence(directory / (commit + ".json"), owner), object_pairs_hook=unique_object)
    require(isinstance(envelope, dict) and set(envelope) == {"payload", "signature"}, "invalid evidence envelope")
    payload = base64.b64decode(envelope["payload"], validate=True)
    signature = base64.b64decode(envelope["signature"], validate=True)
    require(len(signature) == 64, "invalid Ed25519 signature")
    with tempfile.TemporaryDirectory(prefix="ci-fleet-evidence-") as temporary:
        root = Path(temporary)
        for name, data in (("key", local["public_key"].encode()), ("payload", payload), ("signature", signature)):
            (root / name).write_bytes(data)
        result = subprocess.run(["openssl", "pkeyutl", "-verify", "-rawin", "-pubin", "-inkey", str(root / "key"),
                                 "-in", str(root / "payload"), "-sigfile", str(root / "signature")],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                                env={"PATH": "/usr/bin:/bin", "OPENSSL_CONF": "/dev/null"})
        require(result.returncode == 0, "local evidence signature rejected")
    value = json.loads(payload, object_pairs_hook=unique_object)
    require(isinstance(value, dict) and set(value) == {"schema_version", "policy_sha256", "repository_id", "branch",
            "pull_request", "reviewed_head", "result"} and type(value["schema_version"]) is int
            and value["schema_version"] == 1, "invalid local evidence payload")
    require(value["policy_sha256"] == policy_digest(policy)
            and value["repository_id"] == policy["configuration_repository_id"] and value["branch"] == policy["branch"]
            and value["pull_request"] == number and value["reviewed_head"] == head, "local evidence context differs")
    report = value["result"]
    require(isinstance(report, dict) and report.get("source") == commit and report.get("tree") == tree
            and report.get("base") == parent and report.get("policy") == local["validator_revision"]
            and report.get("suite") == local["suite"] and report.get("profile") == "full"
            and report.get("status") == "passed" and report.get("expected_skips") == []
            and type(report.get("schema_version")) is int and report["schema_version"] == 1, "local validation is incomplete or has different identities")
    started, finished = report.get("started_at"), report.get("finished_at")
    current = int(time.time()) if now is None else now
    require(type(started) is int and type(finished) is int and 0 < started <= finished <= current
            and current - started <= local["max_age_seconds"], "local evidence expired or clock is uncertain")
    require(isinstance(report.get("tools"), dict) and bool(report["tools"])
            and all(isinstance(v, str) and v for v in report["tools"].values()), "tool versions are missing")
    checks = report.get("results")
    require(isinstance(checks, list) and all(isinstance(item, dict) for item in checks), "local results missing")
    names = [item.get("name") for item in checks]
    require(len(names) == len(set(names)) and set(names) == set(local["required_checks"]), "local suite coverage differs")
    for item in checks:
        require(item.get("status") == "passed" and isinstance(item.get("sha256"), str)
                and bool(re.fullmatch(r"[0-9a-f]{64}", item["sha256"])), "local check did not pass")
        log = trusted_evidence(directory / (commit + ".logs") / (item["name"] + ".log"), owner)
        require(hashlib.sha256(log).hexdigest() == item["sha256"], "local log was tampered with")


def verify_one(api: GitHub, policy: dict[str, Any], repository: str, commit: str, tree: str) -> str:
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
    parents = candidate.get("parents")
    require(isinstance(parents, list) and 1 <= len(parents) <= 2
            and all(isinstance(parent, dict) and isinstance(parent.get("sha"), str)
                    and bool(SHA.fullmatch(parent["sha"])) for parent in parents), "candidate parents are unavailable")
    require(len(parents) == 1 or parents[1]["sha"] == head, "merge parent differs from reviewed head")
    parent = parents[0]["sha"]
    comparison = api.get(f"{prefix}/compare/{parent}...{head}")
    require(comparison.get("merge_base_commit", {}).get("sha") == parent
            and comparison.get("status") in {"ahead", "identical"},
            "reviewed head does not include the candidate's previous base")
    author_id = pr.get("user", {}).get("id")
    require(positive_id(author_id), "PR author identity is unavailable")
    latest: dict[int, dict[str, Any]] = {}
    reviews = api.pages(f"{prefix}/pulls/{number}/reviews")
    require(all(positive_id(review.get("id")) for review in reviews), "invalid review identifiers")
    for review in reviews:
        reviewer = review.get("user", {}).get("id")
        state = review.get("state")
        require(state in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED", "COMMENTED", "PENDING"}, "invalid review state")
        if reviewer in policy["reviewer_ids"] and state not in {"COMMENTED", "PENDING"}:
            order = review_order(review, head)
            previous = latest.get(reviewer)
            if previous is None or order > review_order(previous, head):
                latest[reviewer] = review
    require(not any(review["state"] == "CHANGES_REQUESTED" for review in latest.values()), "trusted reviewer requested changes")
    base_changed = api.latest_base_change(repository, number)
    formal = any(reviewer != author_id and review["state"] == "APPROVED" and review.get("commit_id") == head
                 and (base_changed is None or evidence_timestamp(review.get("submitted_at")) > base_changed)
                 for reviewer, review in latest.items())
    require(formal or clean_codex_review(api, policy, repository, number, head, author_id, reviews, base_changed),
            "independent exact-head approval is missing")
    if "local_validation" in policy:
        verify_local_evidence(policy, commit, tree, parent, head, number)
    if "workflow_id" in policy:
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
    return parent


def verify(api: GitHub, policy: dict[str, Any], repository: str, commit: str, tree: str,
           previous_commit: str | None = None) -> None:
    if previous_commit is None or previous_commit == commit:
        verify_one(api, policy, repository, commit, tree)
        return
    require(bool(SHA.fullmatch(previous_commit)), "installed configuration SHA is invalid")
    prefix = f"/repos/{repository}"
    lineage = api.get(f"{prefix}/compare/{previous_commit}...{commit}")
    require(lineage.get("merge_base_commit", {}).get("sha") == previous_commit and lineage.get("status") == "ahead",
            "candidate rewrites or replays older installed configuration history")
    seen = set()
    for _ in range(20):
        require(commit not in seen, "configuration history cycle")
        seen.add(commit)
        parent = verify_one(api, policy, repository, commit, tree)
        if parent == previous_commit:
            return
        commit = parent
        tree = api.get(f"{prefix}/git/commits/{commit}")["tree"]["sha"]
    raise Rejected("configuration history verification limit reached")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--tree", required=True)
    parser.add_argument("--installed-state", type=Path, required=True)
    parser.add_argument("--recovery-record", type=Path)
    parser.add_argument("--controller")
    args = parser.parse_args()

    def deadline_expired(_signal: int, _frame: Any) -> None:
        raise Rejected("GitHub evidence deadline reached")

    previous_handler = signal.signal(signal.SIGALRM, deadline_expired)
    signal.setitimer(signal.ITIMER_REAL, 120)
    try:
        policy = read_policy(args.policy)
        if args.recovery_record is not None:
            require(bool(REPOSITORY.fullmatch(args.repository)) and bool(SHA.fullmatch(args.commit))
                    and bool(SHA.fullmatch(args.tree)) and bool(args.controller), "invalid recovery identity")
            require(read_installed_commit(args.recovery_record, args.repository, controller=args.controller) == args.commit,
                    "candidate is not the trusted last-known-good configuration")
        else:
            previous = read_installed_commit(args.installed_state, args.repository)
            require(previous is not None, "configuration approval requires an installed trusted baseline")
            verify(GitHub(sys.stdin.read(8192).strip()), policy, args.repository, args.commit, args.tree, previous)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError):
        # API responses, policy contents, and authentication material are never logged.
        print("ERROR: configuration approval rejected or evidence unavailable", file=sys.stderr)
        return 2
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
    print("APPROVED: trusted last-known-good recovery" if args.recovery_record is not None
          else "APPROVED: exact configuration tree, independent review, and required checks verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
