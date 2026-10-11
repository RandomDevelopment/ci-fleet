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


def evidence() -> dict:
    return {
        "/graphql": {"data": {"repository": {"pullRequest": {"timelineItems": {
            "nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None},
        }}}}},
        PREFIX: {"id": 101, "full_name": REPOSITORY, "default_branch": "main"},
        f"{PREFIX}/git/commits/{COMMIT}": {"sha": COMMIT, "tree": {"sha": TREE}, "parents": [{"sha": PARENT}]},
        f"{PREFIX}/git/commits/{HEAD}": {"sha": HEAD, "tree": {"sha": TREE}},
        f"{PREFIX}/compare/{PARENT}...{HEAD}": {"merge_base_commit": {"sha": PARENT}, "status": "ahead"},
        f"{PREFIX}/commits/{COMMIT}/pulls": [{"number": 8, "merge_commit_sha": COMMIT, "merged_at": "2026-01-01"}],
        f"{PREFIX}/pulls/8": {
            "merged": True, "state": "closed", "merge_commit_sha": COMMIT,
            "base": {"ref": "main", "repo": {"id": 101}}, "head": {"sha": HEAD}, "user": {"id": 201},
        },
        f"{PREFIX}/pulls/8/reviews": [{"id": 900, "user": {"id": 202}, "state": "APPROVED", "commit_id": HEAD,
                                      "submitted_at": "2026-01-01T00:00:00Z"}],
        f"{PREFIX}/actions/workflows/501/runs": {"workflow_runs": [{
            "id": 700, "workflow_id": 501, "head_sha": COMMIT, "event": "push", "head_branch": "main",
            "repository": {"id": 101}, "status": "completed", "conclusion": "success", "run_attempt": 2,
            "created_at": "2026-01-01T00:00:00Z", "run_started_at": "2026-01-01T00:00:01Z", "updated_at": "2026-01-01T00:01:00Z",
        }]},
        f"{PREFIX}/actions/runs/700/attempts/2/jobs": {"jobs": [{"name": "validate", "status": "completed", "conclusion": "success"}]},
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
        value = self.data[url.path]
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)


class ApprovalTests(unittest.TestCase):
    def test_existing_hosts_reject_replay_or_divergence_from_the_installed_commit(self):
        previous = "6" * 40
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


class InstallerGateTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
