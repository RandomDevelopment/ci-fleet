#!/usr/bin/env python3
"""Exercise the approval verifier without credentials, GitHub, or host mutation."""

from __future__ import annotations

import copy
import io
import json
import os
import re
import shlex
import subprocess
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest.mock import patch

from verify_config_approval import GitHub, NoRedirect, Rejected, read_installed_commit, read_policy, verify
import verify_config_approval

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "example-org/private-config"
PREFIX = "/repos/" + REPOSITORY
COMMIT, HEAD, TREE = "1" * 40, "2" * 40, "3" * 40
PARENT = "5" * 40
POLICY = {
    "schema_version": 1, "configuration_repository_id": 101, "branch": "main",
    "workflow_id": 501, "required_jobs": ["validate"], "reviewer_ids": [202],
}
CODEX_HELP_FIXTURE = '''<details> <summary>ℹ️ About Codex in GitHub</summary>
<br/>

[Your team has set up Codex to review pull requests in this repo](https://chatgpt.com/codex/cloud/settings/general). Reviews are triggered when you
- Open a pull request for review
- Mark a draft as ready
- Comment "@codex review".

If Codex has suggestions, it will comment; otherwise it will react with 👍.

Codex can also answer questions or update the PR. Try commenting "@codex address that feedback".
</details>'''


def evidence(commit=COMMIT, head=HEAD, tree=TREE, parent=PARENT, number=8, run_id=700) -> dict:
    return {
        "/graphql": {"data": {"repository": {"pullRequest": {"timelineItems": {
            "nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None},
        }}}}},
        PREFIX: {"id": 101, "full_name": REPOSITORY, "default_branch": "main"},
        f"{PREFIX}/git/commits/{commit}": {"sha": commit, "tree": {"sha": tree}, "parents": [{"sha": parent}]},
        f"{PREFIX}/git/commits/{head}": {"sha": head, "tree": {"sha": tree}},
        f"{PREFIX}/compare/{parent}...{head}": {"merge_base_commit": {"sha": parent}, "status": "ahead"},
        f"{PREFIX}/commits/{commit}/pulls": [{"number": number, "merge_commit_sha": commit, "merged_at": "2026-01-01"}],
        f"{PREFIX}/pulls/{number}": {
            "merged": True, "state": "closed", "merge_commit_sha": commit,
            "base": {"ref": "main", "repo": {"id": 101}}, "head": {"sha": head}, "user": {"id": 201},
        },
        f"{PREFIX}/pulls/{number}/reviews": [{"id": 900, "user": {"id": 202}, "state": "APPROVED", "commit_id": head,
                                      "submitted_at": "2026-01-01T00:00:00Z"}],
        f"{PREFIX}/actions/workflows/501/runs": {"workflow_runs": [{
            "id": run_id, "workflow_id": 501, "head_sha": commit, "event": "push", "head_branch": "main",
            "repository": {"id": 101}, "status": "completed", "conclusion": "success", "run_attempt": 2,
            "created_at": "2026-01-01T00:00:00Z", "run_started_at": "2026-01-01T00:00:01Z", "updated_at": "2026-01-01T00:01:00Z",
        }]},
        f"{PREFIX}/actions/runs/{run_id}/attempts/2/jobs": {"jobs": [{"name": "validate", "status": "completed", "conclusion": "success"}]},
    }


class FixtureAPI(GitHub):
    def __init__(self, data: dict):
        self.data = data
        self.paths: list[str] = []

    def get(self, path: str, query: str | None = None):
        self.paths.append(path)
        url = urllib.parse.urlsplit(path)
        query = urllib.parse.parse_qs(url.query)
        if query.get("page", ["1"])[0] != "1":
            raise Rejected("unexpected fixture pagination")
        key = url.path
        if "head_sha" in query:
            exact = key + "?head_sha=" + query["head_sha"][0]
            if exact in self.data:
                key = exact
        value = self.data[key]
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)


class ApprovalTests(unittest.TestCase):
    def test_every_integrated_commit_since_the_baseline_needs_approval_and_checks(self):
        intermediate = "6" * 40
        run_path = f"{PREFIX}/actions/workflows/501/runs"
        data = evidence(parent=intermediate)
        data[run_path + f"?head_sha={COMMIT}"] = data.pop(run_path)
        staged = evidence(commit=intermediate, head="8" * 40, tree="9" * 40, number=9, run_id=701)
        staged[run_path + f"?head_sha={intermediate}"] = staged.pop(run_path)
        data.update(staged)
        data[f"{PREFIX}/compare/{PARENT}...{COMMIT}"] = {"merge_base_commit": {"sha": PARENT}, "status": "ahead"}
        api = FixtureAPI(data)
        verify(api, POLICY, REPOSITORY, COMMIT, TREE, PARENT)
        self.assertTrue(any(f"/commits/{intermediate}/pulls" in path for path in api.paths))
        for path, changed in (
            (f"{PREFIX}/commits/{intermediate}/pulls", []),
            (f"{PREFIX}/pulls/9/reviews", []),
        ):
            rejected = copy.deepcopy(data)
            rejected[path] = changed
            with self.subTest(path=path), self.assertRaises(Rejected):
                verify(FixtureAPI(rejected), POLICY, REPOSITORY, COMMIT, TREE, PARENT)
        data[run_path + f"?head_sha={intermediate}"]["workflow_runs"][0]["conclusion"] = "failure"
        with self.assertRaises(Rejected):
            verify(FixtureAPI(data), POLICY, REPOSITORY, COMMIT, TREE, PARENT)

    def test_history_verification_has_a_hard_commit_limit(self):
        api = FixtureAPI(evidence())
        commits = [f"{index:040x}" for index in range(10, 31)]
        for commit in commits:
            api.data[f"{PREFIX}/git/commits/{commit}"] = {"tree": {"sha": TREE}}
        api.data[f"{PREFIX}/compare/{PARENT}...{COMMIT}"] = {"merge_base_commit": {"sha": PARENT}, "status": "ahead"}
        with patch.object(verify_config_approval, "verify_one", side_effect=commits) as verifier:
            with self.assertRaisesRegex(Rejected, "history verification limit"):
                verify(api, POLICY, REPOSITORY, COMMIT, TREE, PARENT)
            self.assertEqual(verifier.call_count, 20)

    def test_existing_hosts_reject_replay_or_divergence_from_the_installed_commit(self):
        previous = PARENT
        path = f"{PREFIX}/compare/{previous}...{COMMIT}"
        data = evidence()
        data[path] = {"merge_base_commit": {"sha": previous}, "status": "ahead"}
        verify(FixtureAPI(data), POLICY, REPOSITORY, COMMIT, TREE, previous)
        verify(FixtureAPI(data), POLICY, REPOSITORY, COMMIT, TREE, COMMIT)
        for status in ("behind", "diverged", "identical"):
            data[path] = {"merge_base_commit": {"sha": PARENT}, "status": status}
            with self.subTest(status=status), self.assertRaises(Rejected):
                verify(FixtureAPI(data), POLICY, REPOSITORY, COMMIT, TREE, previous)
    def test_retargeting_requires_a_new_approval_in_the_main_branch_context(self):
        data = evidence()
        nodes = data["/graphql"]["data"]["repository"]["pullRequest"]["timelineItems"]["nodes"]
        nodes.append({"createdAt": "2026-01-01T00:01:00Z"})
        with self.assertRaises(Rejected):
            verify(FixtureAPI(data), POLICY, REPOSITORY, COMMIT, TREE)
        data[f"{PREFIX}/pulls/8/reviews"][0]["submitted_at"] = "2026-01-01T00:02:00Z"
        verify(FixtureAPI(data), POLICY, REPOSITORY, COMMIT, TREE)
        nodes.append({"createdAt": "2026-01-01T00:02:00Z"})
        with self.assertRaises(Rejected):
            verify(FixtureAPI(data), POLICY, REPOSITORY, COMMIT, TREE)

    def test_base_history_pagination_and_api_errors_fail_closed(self):
        first = evidence()["/graphql"]
        timeline = first["data"]["repository"]["pullRequest"]["timelineItems"]
        timeline["pageInfo"].update(hasNextPage=True, endCursor="fixture-cursor")
        last = evidence()["/graphql"]
        last["data"]["repository"]["pullRequest"]["timelineItems"]["nodes"] = [{"createdAt": "2026-01-01T00:01:00Z"}]
        api = GitHub("fixture-authentication")
        with patch.object(api, "get", side_effect=[first, last]) as getter:
            self.assertEqual(api.latest_base_change(REPOSITORY, 8).isoformat(), "2026-01-01T00:01:00+00:00")
            self.assertIn('after: "fixture-cursor"', getter.call_args.args[1])
        with patch.object(api, "get", return_value=first):
            with self.assertRaises(Rejected):
                api.latest_base_change(REPOSITORY, 8)
        with patch.object(api, "get", return_value={"errors": [{"message": "unavailable"}]}):
            with self.assertRaises(Rejected):
                api.latest_base_change(REPOSITORY, 8)

    def test_squash_tree_equivalence_and_exact_applied_commit_checks(self):
        api = FixtureAPI(evidence())
        verify(api, POLICY, REPOSITORY, COMMIT, TREE)
        self.assertTrue(any(f"head_sha={COMMIT}" in path for path in api.paths))
        self.assertIn(f"{PREFIX}/actions/runs/700/attempts/2/jobs?per_page=100&page=1", api.paths)

    def test_identity_tree_and_merge_bypasses_are_rejected(self):
        cases = (
            (PREFIX, "id", 999),
            (PREFIX, "default_branch", "other"),
            (f"{PREFIX}/git/commits/{COMMIT}", "tree", {"sha": "4" * 40}),
            (f"{PREFIX}/git/commits/{HEAD}", "tree", {"sha": "4" * 40}),
            (f"{PREFIX}/pulls/8", "merged", False),
            (f"{PREFIX}/pulls/8", "merge_commit_sha", "4" * 40),
            (f"{PREFIX}/pulls/8", "base", {"ref": "other", "repo": {"id": 101}}),
        )
        for path, field, changed in cases:
            with self.subTest(path=path, field=field):
                data = evidence()
                data[path][field] = changed
                with self.assertRaises(Rejected):
                    verify(FixtureAPI(data), POLICY, REPOSITORY, COMMIT, TREE)
        for associated in ([], evidence()[f"{PREFIX}/commits/{COMMIT}/pulls"] * 2):
            data = evidence()
            data[f"{PREFIX}/commits/{COMMIT}/pulls"] = associated
            with self.assertRaises(Rejected):
                verify(FixtureAPI(data), POLICY, REPOSITORY, COMMIT, TREE)

    def test_review_decisions_are_independent_current_and_not_dismissed(self):
        review_path = f"{PREFIX}/pulls/8/reviews"
        for field, changed in (("commit_id", "4" * 40), ("user", {"id": 201}), ("user", {"id": 999}),
                               ("state", "DISMISSED"), ("state", "CHANGES_REQUESTED"), ("state", "COMMENTED")):
            with self.subTest(field=field, changed=changed):
                data = evidence()
                data[review_path][0][field] = changed
                with self.assertRaises(Rejected):
                    verify(FixtureAPI(data), POLICY, REPOSITORY, COMMIT, TREE)
        for state, commit_id in (("DISMISSED", HEAD), ("CHANGES_REQUESTED", HEAD), ("APPROVED", "4" * 40)):
            data = evidence()
            data[review_path].append({"id": 901, "user": {"id": 202}, "state": state, "commit_id": commit_id,
                                      "submitted_at": "2026-01-01T00:01:00Z"})
            with self.assertRaises(Rejected):
                verify(FixtureAPI(data), POLICY, REPOSITORY, COMMIT, TREE)
        data = evidence()
        data[review_path] = []
        with self.assertRaises(Rejected):
            verify(FixtureAPI(data), POLICY, REPOSITORY, COMMIT, TREE)

    def test_review_submission_time_orders_a_previously_pending_review(self):
        data = evidence()
        reviews = data[f"{PREFIX}/pulls/8/reviews"]
        # A review created earlier can be submitted after the approval.
        reviews.append({"id": 899, "user": {"id": 202}, "state": "CHANGES_REQUESTED", "commit_id": HEAD,
                        "submitted_at": "2026-01-01T00:01:00Z"})
        with self.assertRaises(Rejected):
            verify(FixtureAPI(data), POLICY, REPOSITORY, COMMIT, TREE)
        reviews[0]["submitted_at"] = "2026-01-01T00:02:00Z"
        verify(FixtureAPI(data), POLICY, REPOSITORY, COMMIT, TREE)
        for invalid in (None, "invalid", "2026-01-01T00:02:00"):
            reviews[0]["submitted_at"] = invalid
            with self.subTest(timestamp=invalid), self.assertRaises(ValueError):
                verify(FixtureAPI(data), POLICY, REPOSITORY, COMMIT, TREE)

    def test_same_second_review_conflicts_cannot_preserve_an_approval(self):
        for state, commit_id in (("CHANGES_REQUESTED", HEAD), ("DISMISSED", HEAD), ("APPROVED", "4" * 40)):
            data = evidence()
            data[f"{PREFIX}/pulls/8/reviews"].append({
                "id": 899, "user": {"id": 202}, "state": state, "commit_id": commit_id,
                "submitted_at": "2026-01-01T00:00:00Z",
            })
            with self.subTest(state=state), self.assertRaises(Rejected):
                verify(FixtureAPI(data), POLICY, REPOSITORY, COMMIT, TREE)

    def test_reviewed_head_must_include_previous_main_for_squash_and_merge(self):
        data = evidence()
        data[f"{PREFIX}/git/commits/{COMMIT}"]["parents"].append({"sha": HEAD})
        verify(FixtureAPI(data), POLICY, REPOSITORY, COMMIT, TREE)
        for parents in ([], [{"sha": "invalid"}], [{"sha": PARENT}, {"sha": "4" * 40}],
                        [{"sha": PARENT}] * 3):
            data = evidence()
            data[f"{PREFIX}/git/commits/{COMMIT}"]["parents"] = parents
            with self.subTest(parents=parents), self.assertRaises(Rejected):
                verify(FixtureAPI(data), POLICY, REPOSITORY, COMMIT, TREE)
        for parent_count in (1, 2):
            data = evidence()
            if parent_count == 2:
                data[f"{PREFIX}/git/commits/{COMMIT}"]["parents"].append({"sha": HEAD})
            # Equal trees and an exact-head approval cannot authorize deleting later main changes.
            data[f"{PREFIX}/compare/{PARENT}...{HEAD}"] = {
                "merge_base_commit": {"sha": "4" * 40}, "status": "diverged",
            }
            with self.subTest(parent_count=parent_count), self.assertRaises(Rejected):
                verify(FixtureAPI(data), POLICY, REPOSITORY, COMMIT, TREE)

    def test_never_started_runs_do_not_hide_newer_success_or_pending_evidence(self):
        data = evidence()
        runs = data[f"{PREFIX}/actions/workflows/501/runs"]["workflow_runs"]
        never_started = copy.deepcopy(runs[0])
        never_started.update(id=699, run_started_at=None, conclusion="cancelled",
                             created_at="2025-12-31T00:00:00Z")
        runs.append(never_started)
        verify(FixtureAPI(data), POLICY, REPOSITORY, COMMIT, TREE)
        never_started.update(status="queued", conclusion=None, updated_at="2026-01-02T00:00:00Z")
        with self.assertRaises(Rejected):
            verify(FixtureAPI(data), POLICY, REPOSITORY, COMMIT, TREE)

    def test_comments_do_not_withdraw_approval_and_later_approval_resolves_changes(self):
        data = evidence()
        data[f"{PREFIX}/pulls/8/reviews"].append({"id": 901, "user": {"id": 202}, "state": "COMMENTED", "commit_id": HEAD})
        verify(FixtureAPI(data), POLICY, REPOSITORY, COMMIT, TREE)
        data[f"{PREFIX}/pulls/8/reviews"].insert(0, {"id": 899, "user": {"id": 202}, "state": "CHANGES_REQUESTED", "commit_id": HEAD,
                                                   "submitted_at": "2025-12-31T00:00:00Z"})
        verify(FixtureAPI(data), POLICY, REPOSITORY, COMMIT, TREE)

    def test_latest_run_and_exact_attempt_jobs_must_pass(self):
        run_path = f"{PREFIX}/actions/workflows/501/runs"
        for field, changed in (("head_sha", HEAD), ("workflow_id", 999), ("event", "workflow_dispatch"),
                               ("head_branch", "other"), ("repository", {"id": 999}), ("status", "queued"),
                               ("conclusion", "failure"), ("conclusion", "cancelled"), ("run_attempt", 0)):
            with self.subTest(field=field):
                data = evidence()
                data[run_path]["workflow_runs"][0][field] = changed
                with self.assertRaises(Rejected):
                    verify(FixtureAPI(data), POLICY, REPOSITORY, COMMIT, TREE)
        data = evidence()
        newer = copy.deepcopy(data[run_path]["workflow_runs"][0])
        newer.update(id=701, status="in_progress", conclusion=None)
        data[run_path]["workflow_runs"].append(newer)
        with self.assertRaises(Rejected):
            verify(FixtureAPI(data), POLICY, REPOSITORY, COMMIT, TREE)
        for conclusion in ("failure", "cancelled"):
            newer.update(status="completed", conclusion=conclusion)
            with self.subTest(newer_conclusion=conclusion), self.assertRaises(Rejected):
                verify(FixtureAPI(data), POLICY, REPOSITORY, COMMIT, TREE)
        newer.update(status="in_progress", conclusion=None)
        older_id_rerun = copy.deepcopy(newer)
        older_id_rerun.update(id=699, created_at="2025-12-31T00:00:00Z", run_started_at="2026-01-02T00:00:00Z",
                              updated_at="2026-01-02T00:00:01Z")
        data[run_path]["workflow_runs"] = [evidence()[run_path]["workflow_runs"][0], older_id_rerun]
        with self.assertRaises(Rejected):
            verify(FixtureAPI(data), POLICY, REPOSITORY, COMMIT, TREE)
        data[run_path]["workflow_runs"] = []
        with self.assertRaises(Rejected):
            verify(FixtureAPI(data), POLICY, REPOSITORY, COMMIT, TREE)
        for jobs in ([], [{"name": "validate", "status": "completed", "conclusion": "skipped"}],
                     evidence()[f"{PREFIX}/actions/runs/700/attempts/2/jobs"]["jobs"] * 2):
            data = evidence()
            data[f"{PREFIX}/actions/runs/700/attempts/2/jobs"]["jobs"] = jobs
            with self.assertRaises(Rejected):
                verify(FixtureAPI(data), POLICY, REPOSITORY, COMMIT, TREE)

    def test_api_errors_never_fall_back_to_candidate_claims(self):
        for path in evidence():
            with self.subTest(path=path):
                data = evidence()
                data[path] = Rejected("API unavailable")
                with self.assertRaises(Rejected):
                    verify(FixtureAPI(data), POLICY, REPOSITORY, COMMIT, TREE)

    def test_pagination_finds_late_evidence_and_fails_when_incomplete(self):
        api = GitHub("fixture-authentication")
        with patch.object(api, "get", side_effect=[[{"id": 1}] * 100, [{"id": 2}]]) as getter:
            self.assertEqual(len(api.pages(f"{PREFIX}/pulls/8/reviews")), 101)
            self.assertTrue(getter.call_args.args[0].endswith("page=2"))
        with patch.object(api, "get", return_value=[{"id": 1}] * 100):
            with self.assertRaises(Rejected):
                api.pages(f"{PREFIX}/pulls/8/reviews")
        with patch.object(api.opener, "open", side_effect=urllib.error.URLError("fixture-authentication")):
            with self.assertRaisesRegex(Rejected, "evidence unavailable"):
                api.get(PREFIX)
        with self.assertRaises(Rejected):
            NoRedirect().redirect_request(None, None, None, None, None, "https://example.invalid")

    def test_malformed_json_and_expired_api_budget_are_rejected(self):
        api = GitHub("fixture-authentication")
        with patch.object(api.opener, "open", return_value=io.BytesIO(b"not-json")):
            with self.assertRaises(Rejected):
                api.get(PREFIX)
        api.deadline = 0
        with self.assertRaises(Rejected):
            api.get(PREFIX)

    def test_malformed_api_objects_fail_closed_in_cli(self):
        for path in evidence():
            data = evidence()
            data[path] = ["malformed"] if path == PREFIX else None
            with self.subTest(path=path), patch.object(verify_config_approval, "read_policy", return_value=POLICY), \
                 patch.object(verify_config_approval, "read_installed_commit", return_value=COMMIT), \
                 patch.object(verify_config_approval, "GitHub", return_value=FixtureAPI(data)), \
                 patch("sys.stdin", io.StringIO("fixture-authentication")), patch("sys.stderr", io.StringIO()), \
                 patch("sys.argv", ["verify", "--policy", "/unused", "--repository", REPOSITORY, "--commit", COMMIT, "--tree", TREE,
                                    "--installed-state", "/nonexistent-approval-fixture-state"]):
                self.assertEqual(verify_config_approval.main(), 2)


def installer_function(name: str) -> str:
    source = (ROOT / "scripts" / "install-worker-controller.sh").read_text()
    start = source.index(f"\n{name}() {{") + 1
    following = re.search(r"(?m)^[a-z_]+\(\) \{", source[start + len(name) + 5:])
    assert following is not None
    return source[start:start + len(name) + 5 + following.start()]


class CodexApprovalTests(unittest.TestCase):
    def setUp(self):
        self.policy = dict(POLICY, codex_reviewer_ids=[202])
        self.data = evidence()
        self.reviews = self.data[f"{PREFIX}/pulls/8/reviews"]
        self.reviews.clear()
        self.comment = {
            "databaseId": 1000, "author": {"__typename": "Bot", "databaseId": 202},
            "body": "Codex Review: Didn't find any major issues. What shall we delve into next?\n\n"
                    f"**Reviewed commit:** `{HEAD[:10]}`\n\n{CODEX_HELP_FIXTURE}",
            "createdAt": "2026-01-01T00:01:00Z", "lastEditedAt": None,
        }
        self.pr = self.data["/graphql"]["data"]["repository"]["pullRequest"]
        for key, nodes in (("comments", [self.comment]), ("reviewThreads", [{"isResolved": True}])):
            self.pr[key] = {"nodes": nodes, "pageInfo": {"hasNextPage": False, "endCursor": None}}
        self.data[f"{PREFIX}/commits/{HEAD[:10]}"] = {"sha": HEAD}

    def check(self):
        verify(FixtureAPI(self.data), self.policy, REPOSITORY, COMMIT, TREE)

    def test_clean_review_requires_operator_opt_in_and_successful_exact_applied_checks(self):
        self.check()
        with self.assertRaises(Rejected):
            verify(FixtureAPI(self.data), POLICY, REPOSITORY, COMMIT, TREE)
        self.data[f"{PREFIX}/actions/workflows/501/runs"]["workflow_runs"][0]["conclusion"] = "failure"
        with self.assertRaises(Rejected):
            self.check()

    def test_known_clean_response_follow_up_sentences(self):
        body = self.comment["body"]
        for sentence in ("", " Keep it up!", " Nice work!"):
            self.comment["body"] = body.replace(" What shall we delve into next?", sentence)
            self.check()

    def test_completed_activity_refresh_does_not_replace_clean_decision(self):
        summary = dict(self.comment, databaseId=999, createdAt="2026-01-01T00:00:00Z",
                       lastEditedAt="2026-01-01T00:01:02Z", body=(
            '<!-- codex-pull-request-review-summary -->\n\n## Codex Review Summary\n\n'
            '| Review | Status | Commit | Review trigger |\n| --- | --- | --- | --- |\n'
            '| 📝 **Code Review** | ✅ **Completed** <relative-time datetime="2026-01-01T00:01:01Z">'
            f'2026-01-01T00:01:01Z</relative-time> | `{HEAD[:7]}` | Manual request |\n\n'
            '<details>Comment "@codex review" or "@codex security review".</details>'))
        self.pr["comments"]["nodes"].append(summary)
        self.data[f"{PREFIX}/commits/{HEAD[:7]}"] = {"sha": HEAD}
        self.data[f"{PREFIX}/commits/{'4' * 7}"] = {"sha": "4" * 40}
        self.check()
        body = summary["body"]
        state = {"code_review": {"commit_sha": HEAD, "status": "completed", "has_any_findings": False,
                                  "has_unresolved_findings": False},
                 "security_review": {"commit_sha": None, "status": None, "has_any_findings": None,
                                      "has_unresolved_findings": None}}
        summary["body"] = body + '\n<!-- codex-review-state:v1 ' + json.dumps(state) + ' -->'
        self.check()
        state["code_review"]["has_unresolved_findings"] = True
        for prefix in ('', ' ', '\t', 'Text before '):
            with self.subTest(prefix=prefix), self.assertRaises(Rejected):
                summary["body"] = body + '\n' + prefix + '<!-- codex-review-state:v1 ' + json.dumps(state) + ' -->'
                self.check()
        for changed in (body.replace("Completed", "Running"), body.replace("Completed", "Failed"),
                        body.replace(HEAD[:7], "4" * 7), body.replace("00:01:01", "00:00:59"),
                        body + '\n<!-- codex-review-state:v2 {} -->',
                        body + '\n| Security review | Running | unknown | Manual request |'):
            with self.subTest(body=changed), self.assertRaises(Rejected):
                summary["body"] = changed
                self.check()
        summary["body"] = body
        self.data[f"{PREFIX}/commits/{HEAD[:7]}"] = {"sha": "4" * 40}
        with self.assertRaises(Rejected):
            self.check()
        self.data[f"{PREFIX}/commits/{HEAD[:7]}"] = {"sha": HEAD}
        self.pr["comments"]["nodes"] = [summary]
        with self.assertRaises(Rejected):
            self.check()

    def test_copied_summary_wrong_identity_or_author_cannot_approve(self):
        for author in ({"__typename": "User", "databaseId": 202},
                       {"__typename": "Bot", "databaseId": 999}, None):
            with self.subTest(author=author), self.assertRaises(Rejected):
                self.comment["author"] = author
                self.check()
        self.comment["author"] = {"__typename": "Bot", "databaseId": 202}
        self.data[f"{PREFIX}/pulls/8"]["user"]["id"] = 202
        with self.assertRaises(Rejected):
            self.check()

    def test_edited_or_unrecognized_summaries_do_not_approve(self):
        body = self.comment["body"]
        for changed in (body.replace("Didn't find any major issues.", "Review completed."),
                        "Quoted response:\n" + body, body + "\nAdditional finding", body.replace(HEAD[:10], HEAD[:7])):
            with self.subTest(body=changed), self.assertRaises(Rejected):
                self.comment["body"] = changed
                self.check()
        self.comment["body"] = body
        self.comment["lastEditedAt"] = self.comment["createdAt"]
        with self.assertRaises(Rejected):
            self.check()

    def test_reported_abbreviation_must_resolve_to_exact_head(self):
        self.data[f"{PREFIX}/commits/{HEAD[:10]}"] = {"sha": "4" * 40}
        with self.assertRaises(Rejected):
            self.check()
        self.data[f"{PREFIX}/commits/{HEAD[:10]}"] = Rejected("ambiguous commit")
        with self.assertRaises(Rejected):
            self.check()

    def test_footer_accepts_only_known_help_text_or_no_footer(self):
        body = self.comment["body"]
        self.comment["body"] = body.split('\n\n<details>')[0] + '\n'
        self.check()
        for footer in ('<details>Warning: this commit has a finding.</details>',
                       CODEX_HELP_FIXTURE.replace('otherwise it will react with 👍.', 'this commit is unsafe.'),
                       CODEX_HELP_FIXTURE.replace('</details>', 'Additional finding</details>')):
            self.comment["body"] = body.replace(CODEX_HELP_FIXTURE, footer)
            with self.subTest(footer=footer), self.assertRaises(Rejected):
                self.check()

    def test_newer_conflicts_from_another_pinned_bot_block_clean_response(self):
        self.policy.update(reviewer_ids=[202, 203], codex_reviewer_ids=[202, 203])
        for timestamp in ('2026-01-01T00:01:00Z', '2026-01-01T00:02:00Z'):
            other = dict(self.comment, databaseId=1001, author={"__typename": "Bot", "databaseId": 203},
                         body='Review failed: candidate is unsafe', createdAt=timestamp)
            self.pr["comments"]["nodes"] = [self.comment, other]
            with self.subTest(timestamp=timestamp), self.assertRaises(Rejected):
                self.check()
        other['createdAt'] = '2026-01-01T00:00:59Z'
        self.check()
        for state in ('COMMENTED', 'PENDING', 'DISMISSED'):
            self.reviews[:] = [{"id": 901, "user": {"id": 203}, "state": state, "commit_id": HEAD,
                               "submitted_at": None if state == 'PENDING' else '2026-01-01T00:02:00Z'}]
            with self.subTest(state=state), self.assertRaises(Rejected):
                self.check()

    def test_newer_requests_bot_responses_and_same_second_conflicts_invalidate_clean_review(self):
        for author, body in (({"__typename": "User", "databaseId": 201}, "@codex review"),
                             ({"__typename": "User", "databaseId": 201}, "Please @codex security review"),
                             ({"__typename": "Bot", "databaseId": 202}, "Code review usage limit reached")):
            for timestamp in ("2026-01-01T00:01:00Z", "2026-01-01T00:02:00Z"):
                with self.subTest(body=body, timestamp=timestamp), self.assertRaises(Rejected):
                    self.pr["comments"]["nodes"] = [self.comment, dict(self.comment, databaseId=1001, author=author,
                                                                  body=body, createdAt=timestamp)]
                    self.check()
        edited = dict(self.comment, databaseId=999, createdAt="2026-01-01T00:00:00Z",
                      lastEditedAt="2026-01-01T00:02:00Z")
        self.pr["comments"]["nodes"] = [self.comment, edited]
        with self.assertRaises(Rejected):
            self.check()

    def test_later_or_pending_formal_bot_review_invalidates_comment(self):
        for state in ("APPROVED", "COMMENTED", "CHANGES_REQUESTED", "DISMISSED", "PENDING"):
            for timestamp in ("2026-01-01T00:01:00Z", "2026-01-01T00:02:00Z"):
                self.reviews[:] = [{"id": 901, "user": {"id": 202}, "state": state, "commit_id": "4" * 40,
                                   "submitted_at": None if state == "PENDING" else timestamp}]
                with self.subTest(state=state, timestamp=timestamp), self.assertRaises(Rejected):
                    self.check()
        self.reviews[:] = [{"id": 900, "user": {"id": 202}, "state": "COMMENTED", "commit_id": HEAD,
                           "submitted_at": "2026-01-01T00:00:00Z"}]
        self.check()

    def test_unresolved_threads_and_trusted_change_requests_still_block(self):
        self.pr["reviewThreads"]["nodes"] = [{"isResolved": False}]
        with self.assertRaises(Rejected):
            self.check()
        self.pr["reviewThreads"]["nodes"] = [{"isResolved": True}]
        self.policy["reviewer_ids"] = [202, 203]
        self.reviews.append({"id": 900, "user": {"id": 203}, "state": "CHANGES_REQUESTED", "commit_id": HEAD,
                             "submitted_at": "2026-01-01T00:00:00Z"})
        with self.assertRaises(Rejected):
            self.check()

    def test_retargeted_pr_requires_fresh_clean_review(self):
        self.pr["timelineItems"]["nodes"] = [{"createdAt": "2026-01-01T00:01:00Z"}]
        with self.assertRaises(Rejected):
            self.check()
        self.comment["createdAt"] = "2026-01-01T00:02:00Z"
        self.check()

    def test_missing_or_incomplete_comment_and_thread_evidence_fails_closed(self):
        for connection in ("comments", "reviewThreads"):
            collection = copy.deepcopy(self.pr[connection])
            for changed in ({"nodes": [], "pageInfo": {}},
                            {"nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": None}}):
                with self.subTest(connection=connection, changed=changed), self.assertRaises(Rejected):
                    self.pr[connection] = changed
                    self.check()
            self.pr[connection] = collection

    def test_bot_evidence_pagination_checks_every_page_and_rejects_cycles(self):
        api = GitHub("fixture-authentication")
        first, last = copy.deepcopy(self.data["/graphql"]), copy.deepcopy(self.data["/graphql"])
        collection = first["data"]["repository"]["pullRequest"]["comments"]
        collection["pageInfo"] = {"hasNextPage": True, "endCursor": "fixture-cursor"}
        last["data"]["repository"]["pullRequest"]["comments"]["nodes"] = []
        with patch.object(api, "get", side_effect=[first, last]) as getter:
            self.assertEqual(len(api.pull_request_nodes(REPOSITORY, 8, "comments", "body")), 1)
            self.assertIn('after: "fixture-cursor"', getter.call_args.args[1])
        with patch.object(api, "get", return_value=first), self.assertRaises(Rejected):
            api.pull_request_nodes(REPOSITORY, 8, "comments", "body")


class InstallerGateTests(unittest.TestCase):
    def test_recovery_option_requires_an_inherited_upgrade_lock(self):
        result = subprocess.run(["bash", str(ROOT / "scripts" / "install-worker-controller.sh"),
                                 "--upgrade", "--restore-last-known-good"],
                                env={**os.environ, "CI_FLEET_INSTALLER_LOCK_FD": ""}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("last-known-good restoration requires a locked upgrade", result.stderr)

    def test_policy_resume_rechecks_approval_before_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            metadata = Path(directory) / "metadata.json"
            metadata.write_text(json.dumps({"config_repository": REPOSITORY, "config_ref": COMMIT, "config_tree": TREE}))
            script = installer_function("perform_policy_action") + f'''
set -eu
policy_action=resume
config_identity={REPOSITORY}
candidate_metadata={shlex.quote(str(metadata))}
load_policy_action_context() {{ :; }}
configuration_approval_required() {{ return 0; }}
verify_configuration_approval() {{ printf 'APPROVAL_RECHECK\n'; return 2; }}
run_candidate_preflight() {{ printf 'UNAUTHORIZED_PREFLIGHT\n'; }}
perform_policy_action
'''
            result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("APPROVAL_RECHECK", result.stdout)
            self.assertNotIn("UNAUTHORIZED_PREFLIGHT", result.stdout)
            metadata.write_text(json.dumps({"config_repository": REPOSITORY, "config_ref": COMMIT, "config_tree": TREE,
                                            "configuration_recovery_required": True}))
            recovery_script = script.replace("APPROVAL_RECHECK", "RECOVERY_RECHECK:%s").replace("; return 2;", ' "$restore_lkg"; return 2;')
            result = subprocess.run(["bash", "-c", recovery_script], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("RECOVERY_RECHECK:true", result.stdout)
            self.assertNotIn("UNAUTHORIZED_PREFLIGHT", result.stdout)

    def test_rollback_rejects_a_manager_without_enforcement_before_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = root / "managers" / ("4" * 40)
            manager.mkdir(parents=True)
            (manager / "engine-capabilities.json").write_text('{"schema_version":1,"capabilities":{}}')
            checkpoint = root / "checkpoint"
            checkpoint.mkdir()
            (checkpoint / "manager-target").write_text(str(manager) + "\n")
            (checkpoint / "manager-target").chmod(0o600)
            script = installer_function("restore_checkpoint") + f'''
set -eu
checkpoint_dir={shlex.quote(str(checkpoint))}
manager_releases={shlex.quote(str(root / 'managers'))}
rendered_env={shlex.quote(str(root / 'absent'))}
testing=1
configuration_approval_required() {{ return 0; }}
require_configuration_approval_capability() {{ python3 {shlex.quote(str(ROOT / 'scripts' / 'desired_state.py'))} validate-engine-capabilities --manifest "$1" --require-configuration-approval; }}
note() {{ printf '%s\n' "$*"; }}
restore_checkpoint
'''
            result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("would remove configuration approval enforcement", result.stdout)

    def test_capability_requires_explicit_reviewed_engine_support(self):
        result = subprocess.run(["python3", str(ROOT / "scripts" / "desired_state.py"), "validate-engine-capabilities",
                                 "--manifest", str(ROOT / "engine-capabilities.json"), "--require-configuration-approval"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path.home())
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "config-review-policy.json"
        self.path.write_text(json.dumps(POLICY))
        self.path.chmod(0o600)
        self.marker = self.path.with_name("config-review-required")
        self.marker.write_text("1\n")
        self.marker.chmod(0o600)

    def test_trusted_policy_and_marker(self):
        self.assertEqual(read_policy(self.path, os.getuid()), POLICY)

    def test_active_policy_syntax_blocks_incompatible_upgrade_and_checkpoint_rollback(self):
        root = self.path.parent
        manager = root / 'managers' / ('4' * 40)
        manager.mkdir(parents=True)
        manifest = manager / 'engine-capabilities.json'
        manifest.write_text(json.dumps({'schema_version': 1, 'capabilities': {'configuration_approval_gate': True}}))
        checkpoint = root / 'checkpoint'
        checkpoint.mkdir()
        (checkpoint / 'manager-target').write_text(str(manager) + '\n')
        (checkpoint / 'manager-target').chmod(0o600)
        shim = root / 'capability-check.py'
        shim.write_text(f'''import os, sys
sys.path.insert(0, {str(ROOT / 'scripts')!r})
import desired_state, verify_config_approval
read_policy = verify_config_approval.read_policy
verify_config_approval.read_policy = lambda path: read_policy(path, os.getuid())
raise SystemExit(desired_state.main())
''')
        setup = f'''
set -eu
repo_root={shlex.quote(str(ROOT))}
config_review_policy={shlex.quote(str(self.path))}
checkpoint_dir={shlex.quote(str(checkpoint))}
manager_releases={shlex.quote(str(root / 'managers'))}
rendered_env={shlex.quote(str(root / 'absent'))}
testing=1
configuration_approval_required() {{ return 0; }}
note() {{ printf '%s\n' "$*"; }}
'''
        helper = installer_function('require_configuration_approval_capability').replace(
            '"$repo_root/scripts/desired_state.py"', shlex.quote(str(shim)))
        check = setup + helper + f'\nrequire_configuration_approval_capability {shlex.quote(str(manifest))}\n'
        self.assertEqual(subprocess.run(['bash', '-c', check], capture_output=True).returncode, 0)
        for ids in ([], [202]):
            self.path.write_text(json.dumps(dict(POLICY, codex_reviewer_ids=ids)))
            with self.subTest(ids=ids):
                result = subprocess.run(['bash', '-c', check], capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                rollback = setup + helper + installer_function('restore_checkpoint') + '\nrestore_checkpoint\n'
                result = subprocess.run(['bash', '-c', rollback], capture_output=True, text=True)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn('would remove configuration approval enforcement', result.stdout)
                supported = check.replace(str(manifest), str(ROOT / 'engine-capabilities.json'))
                self.assertEqual(subprocess.run(['bash', '-c', supported], capture_output=True).returncode, 0)

    def test_codex_requires_explicit_allowed_numeric_id_opt_in(self):
        value = dict(POLICY, codex_reviewer_ids=[202])
        self.path.write_text(json.dumps(value))
        self.assertEqual(read_policy(self.path, os.getuid()), value)
        for reviewers in (True, None, [True], [999], [202, 202]):
            self.path.write_text(json.dumps(dict(POLICY, codex_reviewer_ids=reviewers)))
            with self.subTest(reviewers=reviewers), self.assertRaises(Rejected):
                read_policy(self.path, os.getuid())

    def test_invalid_policy_schema_is_rejected(self):
        for field, changed in (("schema_version", True), ("configuration_repository_id", True), ("workflow_id", 0),
                               ("reviewer_ids", []), ("reviewer_ids", [True]), ("required_jobs", []),
                               ("branch", "../main"), ("approval", True)):
            with self.subTest(field=field):
                value = dict(POLICY)
                value[field] = changed
                self.path.write_text(json.dumps(value))
                with self.assertRaises(Rejected):
                    read_policy(self.path, os.getuid())
        self.path.write_text('{"schema_version":1,"schema_version":1}')
        with self.assertRaises(Rejected):
            read_policy(self.path, os.getuid())

    def test_missing_insecure_or_symlinked_policy_and_marker_are_rejected(self):
        for path in (self.path, self.marker):
            with self.subTest(path=path.name):
                content = path.read_text()
                path.chmod(0o644)
                with self.assertRaises(Rejected):
                    read_policy(self.path, os.getuid())
                path.unlink()
                with self.assertRaises(OSError):
                    read_policy(self.path, os.getuid())
                path.symlink_to("missing")
                with self.assertRaises(OSError):
                    read_policy(self.path, os.getuid())
                path.unlink()
                path.write_text(content)
                path.chmod(0o600)
        Path(self.directory.name).chmod(0o777)
        with self.assertRaises(Rejected):
            read_policy(self.path, os.getuid())

    def test_cli_error_output_never_contains_policy_or_credentials(self):
        self.path.write_text(json.dumps({"approval": "fixture-sensitive-marker"}))
        result = subprocess.run([
            "python3", str(ROOT / "scripts" / "verify_config_approval.py"), "--policy", str(self.path),
            "--repository", REPOSITORY, "--commit", COMMIT, "--tree", TREE, "--installed-state", str(self.path.with_name("state.json")),
        ], input="fixture-authentication", text=True, capture_output=True)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("fixture-authentication", result.stdout + result.stderr)
        self.assertNotIn("fixture-sensitive-marker", result.stdout + result.stderr)

    def test_installed_baseline_identity_and_permissions_are_required(self):
        path = self.path.with_name("state.json")
        self.assertIsNone(read_installed_commit(path, REPOSITORY, os.getuid()))
        original = {"config_repository": REPOSITORY, "config_ref": COMMIT}
        path.write_text(json.dumps(original))
        path.chmod(0o600)
        self.assertEqual(read_installed_commit(path, REPOSITORY, os.getuid()), COMMIT)
        for key, value in (("config_repository", "other/private-config"), ("config_ref", "invalid")):
            path.write_text(json.dumps({**original, key: value}))
            with self.subTest(key=key), self.assertRaises(Rejected):
                read_installed_commit(path, REPOSITORY, os.getuid())
        path.write_text(json.dumps(original))
        path.chmod(0o666)
        with self.assertRaises(Rejected):
            read_installed_commit(path, REPOSITORY, os.getuid())
        path.unlink()
        path.symlink_to("missing")
        with self.assertRaises(OSError):
            read_installed_commit(path, REPOSITORY, os.getuid())

    def test_recovery_uses_only_the_trusted_saved_identity_without_github(self):
        path = self.path.with_name("lkg.json")
        record = {"config_repository": REPOSITORY, "config_ref": COMMIT, "controller": "example-ci-01", "saved_at": 1}
        path.write_text(json.dumps(record))
        path.chmod(0o600)
        read_record = read_installed_commit

        def fixture_record(path, repository, controller=None):
            return read_record(path, repository, os.getuid(), controller)

        argv = ["verify", "--policy", str(self.path), "--repository", REPOSITORY, "--commit", COMMIT,
                "--tree", TREE, "--installed-state", str(self.path.with_name("failed-state.json")),
                "--recovery-record", str(path), "--controller", "example-ci-01"]
        with patch.object(verify_config_approval, "read_policy", return_value=POLICY), \
             patch.object(verify_config_approval, "read_installed_commit", side_effect=fixture_record), \
             patch.object(verify_config_approval, "GitHub", side_effect=AssertionError("recovery must not query GitHub")), \
             patch("sys.argv", argv), patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()):
            self.assertEqual(verify_config_approval.main(), 0)
            for key, value in (("config_ref", "4" * 40), ("controller", "other-ci"), ("saved_at", "invalid")):
                path.write_text(json.dumps({**record, key: value}))
                with self.subTest(key=key):
                    self.assertEqual(verify_config_approval.main(), 2)
            path.write_text(json.dumps(record))
            path.chmod(0o644)
            self.assertEqual(verify_config_approval.main(), 2)

    def test_forward_activation_requires_an_existing_trusted_baseline(self):
        with patch.object(verify_config_approval, "read_policy", return_value=POLICY), \
             patch.object(verify_config_approval, "read_installed_commit", return_value=None), \
             patch("sys.argv", ["verify", "--policy", str(self.path), "--repository", REPOSITORY, "--commit", COMMIT,
                                "--tree", TREE, "--installed-state", str(self.path.with_name("missing-state.json"))]), \
             patch("sys.stderr", io.StringIO()):
            self.assertEqual(verify_config_approval.main(), 2)


if __name__ == "__main__":
    unittest.main()
