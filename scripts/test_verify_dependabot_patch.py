#!/usr/bin/env python3
"""Exercise artifact patch verification with GitHub API response shapes."""

import copy
import json
import unittest
from urllib.parse import urlencode

from verify_dependabot_patch import BUILD_CHECK, BUILD_WORKFLOW_ID, WORKFLOW, artifact_patch, verify_patch


REPO = "repos/RandomDevelopment/ci-fleet"
PULL = f"{REPO}/pulls/17"
HEAD, BASE, PIN = "1" * 40, "2" * 40, "3" * 40
BRANCH = "dependabot/github_actions/routine-actions-7884ae14f9"
REPO_ID = 12
BOT = {"id": 49699333, "type": "Bot", "login": "dependabot[bot]"}
COMPARE = f"{REPO}/compare/{BASE}...{HEAD}"
PULLS = f"{REPO}/pulls?" + urlencode({
    "head": f"RandomDevelopment:{BRANCH}", "base": "main", "state": "open",
})
RUNS = f"{REPO}/actions/workflows/{BUILD_WORKFLOW_ID}/runs?" + urlencode({
    "event": "pull_request", "head_sha": HEAD, "per_page": 1,
})
CHECKS = f"{REPO}/check-suites/20/check-runs?" + urlencode({
    "check_name": BUILD_CHECK, "filter": "latest",
})
RELEASE = "repos/actions/upload-artifact/releases/tags/v7.0.3"
UPSTREAM_TAG = "repos/actions/upload-artifact/commits/refs%2Ftags%2Fv7.0.3"
MESSAGE = """ci(deps): bump actions/upload-artifact

---
updated-dependencies:
- dependency-name: actions/upload-artifact
  dependency-version: 7.0.3
  dependency-type: direct:production
  update-type: version-update:semver-patch
  dependency-group: routine-actions
...

Signed-off-by: dependabot[bot] <support@github.com>
"""
DIFF = f"""diff --git a/{WORKFLOW} b/{WORKFLOW}
index 87dc9e8a..91d6e71c 100644
--- a/{WORKFLOW}
+++ b/{WORKFLOW}
@@ -211,3 +211,3 @@ jobs:
         id: validation-artifact
-        uses: actions/upload-artifact@{"4" * 40} # v7.0.2
+        uses: actions/upload-artifact@{PIN} # v7.0.3
         with:
"""


class VerifyPatchTest(unittest.TestCase):
    def setUp(self):
        self.event = {
            "repository": {"id": REPO_ID, "full_name": "RandomDevelopment/ci-fleet",
                           "owner": {"login": "RandomDevelopment"}},
            "workflow_run": {
                "workflow_id": BUILD_WORKFLOW_ID, "id": 19, "run_attempt": 2,
                "event": "pull_request", "head_repository": {"id": REPO_ID},
                "head_sha": HEAD, "head_branch": BRANCH, "conclusion": "success",
            },
        }
        self.diff = DIFF
        self.current = None
        self.read_pull = False
        self.responses = {
            PULLS: [{"number": 17}],
            PULL: {
                "user": copy.deepcopy(BOT), "state": "open", "draft": False, "number": 17,
                "head": {"ref": BRANCH, "sha": HEAD, "repo": {"id": REPO_ID}},
                "base": {"ref": "main", "sha": BASE, "repo": {"id": REPO_ID}},
                "commits": 1, "changed_files": 1, "additions": 1, "deletions": 1,
            },
            COMPARE: {
                "merge_base_commit": {"sha": BASE}, "total_commits": 1,
                "commits": [{
                    "sha": HEAD, "parents": [{"sha": BASE}], "author": copy.deepcopy(BOT),
                    "committer": {"login": "web-flow"},
                    "commit": {"verification": {"verified": True}, "message": MESSAGE},
                }],
            },
            RELEASE: {"tag_name": "v7.0.3", "draft": False, "prerelease": False},
            UPSTREAM_TAG: {"sha": PIN},
            RUNS: {"workflow_runs": [{
                "workflow_id": BUILD_WORKFLOW_ID, "id": 19, "run_attempt": 2,
                "event": "pull_request", "head_sha": HEAD,
                "status": "completed", "conclusion": "success",
                "check_suite_id": 20,
                "pull_requests": [{
                    "number": 17,
                    "head": {"ref": BRANCH, "sha": HEAD, "repo": {"id": REPO_ID}},
                    "base": {"ref": "main", "sha": BASE, "repo": {"id": REPO_ID}},
                }],
            }]},
            CHECKS: {"total_count": 1, "check_runs": [{
                "name": BUILD_CHECK, "app": {"id": 15368}, "head_sha": HEAD,
                "status": "completed", "conclusion": "success",
            }]},
        }

    def request(self, path, *args):
        if path == COMPARE and args == ("--header", "Accept: application/vnd.github.diff"):
            return self.diff
        if path == PULL and self.read_pull and self.current is not None:
            return json.dumps(self.current)
        if path == PULL:
            self.read_pull = True
        return json.dumps(self.responses[path])

    def test_verifies_the_current_patch_using_only_read_requests(self):
        self.assertTrue(verify_patch(self.event, self.request))

    def test_refuses_untrusted_or_mixed_updates(self):
        for case in ("wrong PR author", "unsigned head", "extra file", "outdated base",
                     "bad upstream tag", "prerelease"):
            with self.subTest(case=case):
                self.setUp()
                if case == "wrong PR author":
                    self.responses[PULL]["user"]["id"] += 1
                elif case == "unsigned head":
                    self.responses[COMPARE]["commits"][0]["commit"]["verification"]["verified"] = False
                elif case == "extra file":
                    self.responses[PULL]["changed_files"] += 1
                elif case == "outdated base":
                    self.responses[COMPARE]["merge_base_commit"]["sha"] = PIN
                elif case == "bad upstream tag":
                    self.responses[UPSTREAM_TAG]["sha"] = BASE
                else:
                    self.responses[RELEASE]["prerelease"] = True
                self.assertFalse(verify_patch(self.event, self.request))

    def test_requires_an_actual_success_from_the_latest_build_on_this_head(self):
        for case in ("missing", "pending", "skipped", "cancelled", "failure", "wrong app",
                     "wrong head", "newer run", "new attempt"):
            with self.subTest(case=case):
                self.setUp()
                check = self.responses[CHECKS]["check_runs"][0]
                run = self.responses[RUNS]["workflow_runs"][0]
                if case == "missing":
                    self.responses[CHECKS] = {"total_count": 0, "check_runs": []}
                elif case == "pending":
                    check.update(status="in_progress", conclusion=None)
                elif case in {"skipped", "cancelled", "failure"}:
                    check["conclusion"] = case
                elif case == "wrong app":
                    check["app"]["id"] += 1
                elif case == "wrong head":
                    check["head_sha"] = BASE
                elif case == "newer run":
                    run["id"] += 1
                else:
                    run["run_attempt"] += 1
                self.assertFalse(verify_patch(self.event, self.request))

    def test_requires_build_association_with_the_same_pr_and_base(self):
        for case in ("missing", "ambiguous", "wrong PR", "wrong base branch", "wrong base SHA"):
            with self.subTest(case=case):
                self.setUp()
                run = self.responses[RUNS]["workflow_runs"][0]
                associated, = run["pull_requests"]
                if case == "missing":
                    del run["pull_requests"]
                elif case == "ambiguous":
                    run["pull_requests"].append({**associated, "number": 18})
                elif case == "wrong PR":
                    associated["number"] = 18
                elif case == "wrong base branch":
                    associated["base"]["ref"] = "other"
                else:
                    associated["base"]["sha"] = PIN
                self.assertFalse(verify_patch(self.event, self.request))

    def test_rechecks_pr_head_and_base_before_reporting_success(self):
        for side in ("head", "base"):
            with self.subTest(side=side):
                self.setUp()
                self.current = copy.deepcopy(self.responses[PULL])
                self.current[side]["sha"] = PIN
                self.assertFalse(verify_patch(self.event, self.request))

    def test_exact_pin_and_signed_metadata_scope(self):
        for diff, message in (
            (DIFF.replace("7.0.3", "7.1.0"), MESSAGE.replace("7.0.3", "7.1.0")),
            (DIFF.replace("7.0.3", "8.0.0"), MESSAGE.replace("7.0.3", "8.0.0")),
            (DIFF.replace("100644", "100755"), MESSAGE),
            (DIFF.replace("+        uses:", "+         uses:"), MESSAGE),
            (DIFF.replace("# v7.0.3\n", "# v7.0.3\r\n"), MESSAGE),
            (DIFF.replace(WORKFLOW, ".github/workflows/update-actions-runner.yml"), MESSAGE),
            (DIFF.replace("actions/upload-artifact", "actions/checkout"), MESSAGE),
            (DIFF + "+        shell: bash\n", MESSAGE),
            (DIFF, MESSAGE.replace("version-update:semver-patch", "security-update:semver-patch")),
            (DIFF, MESSAGE.replace("routine-actions", "another-group")),
            (DIFF, MESSAGE.replace("...\n", "- dependency-name: actions/checkout\n...\n")),
        ):
            with self.subTest(diff=diff, message=message):
                self.assertIsNone(artifact_patch(diff, message))


if __name__ == "__main__":
    unittest.main()
