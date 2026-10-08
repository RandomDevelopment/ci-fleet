#!/usr/bin/env python3
"""Regression tests for the tag-validation step in .github/workflows/validate.yml.

Finding 3861004945: the release-tag step must run the validator extracted from
current trusted main, never the tagged-tree
copy, so a branch-local commit cannot weaken its own tag policy.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "validate.yml"


class TrustedTagValidatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = WORKFLOW.read_text(encoding="utf-8")

    def _tag_step(self) -> str:
        match = re.search(
            r"- name: Validate release tags are SemVer 2\.0\.0\n(.*?)(?=\n  [a-z-]+:|\Z)",
            self.text,
            re.DOTALL,
        )
        self.assertIsNotNone(match, "release-tag validation step not found")
        return match.group(0)

    def test_tag_step_does_not_run_the_tagged_tree_validator(self) -> None:
        step = self._tag_step()
        self.assertNotIn(
            "python3 scripts/validate_commits.py",
            step,
            "the tag step must not execute the tagged-tree copy of the validator",
        )
        self.assertNotIn(
            "cp scripts/validate_commits.py",
            step,
            "a tag push must fail when the trusted revision lacks the validator",
        )

    def test_policy_is_loaded_once_from_current_main(self) -> None:
        loader = step_script("Load current main commit validator")
        self.assertIn('git rev-parse origin/main', loader)
        self.assertIn('git show "$TRUSTED_SHA:scripts/validate_commits.py"', loader)
        self.assertNotIn("merge-base", loader)
        self.assertIn("trusted commit validator unavailable", loader)
        self.assertIn('"$RUNNER_TEMP/trusted-validator.py"', self._tag_step())

    def test_tag_step_passes_the_release_range(self) -> None:
        step = self._tag_step()
        self.assertIn(
            'RELEASE_BASE="$(python3 "$validator" --release-base-for "$TAG_COMMIT" '
            '--exclude-tag "$TAG_NAME")"',
            step,
        )
        self.assertIn('--base "$RELEASE_BASE" --head "$TAG_COMMIT"', step)

    def test_new_tag_secret_scan_uses_a_finite_range(self) -> None:
        scanner = self.text.split("- name: Scan every proposed commit for secrets", 1)[1]
        self.assertIn('BASE_SHA="$(git merge-base "$HEAD_SHA" origin/main)"', scanner)

    def test_tag_secret_scan_uses_the_trusted_scanner(self) -> None:
        scanner = self.text.split("- name: Scan every proposed commit for secrets", 1)[1]
        self.assertNotIn('[[ "$EVENT_NAME" == pull_request ]] && git cat-file', scanner)
        self.assertIn('git show "$TRUSTED_SHA:scripts/scan_committed_secrets.py"', scanner)
        self.assertIn("trusted secret scanner unavailable", scanner)

    def test_validation_runs_after_failure_but_stops_on_cancellation(self) -> None:
        validate_job = self.text.split("\n  validate:\n", 1)[1]
        condition = validate_job.split("    steps:", 1)[0]
        self.assertIn("!cancelled()", condition)
        self.assertNotIn("always()", condition)

    def test_tag_deletion_skips_both_validation_jobs(self) -> None:
        guard = "github.event_name != 'push' || github.event.deleted == false"
        convention_job = self.text.split("\n  commit-convention:\n", 1)[1]
        convention_job = convention_job.split("    steps:", 1)[0]
        validate_job = self.text.split("\n  validate:\n", 1)[1].split("    steps:", 1)[0]
        self.assertIn(guard, convention_job)
        self.assertIn(guard, validate_job)

    def test_updated_tags_are_rejected_before_validation(self) -> None:
        convention = self.text.split("\n  commit-convention:\n", 1)[1]
        reject = convention.split("- name: Check out repository", 1)[0]
        self.assertIn("Reject updates to published tags", reject)
        self.assertIn("github.event.created == false", reject)
        self.assertIn("startsWith(github.ref, 'refs/tags/')", reject)

    def test_repository_validation_runs_this_suite(self) -> None:
        validation = (ROOT / "scripts" / "validate.sh").read_text(encoding="utf-8")
        self.assertIn("python3 scripts/test_workflow_tag_validation.py", validation)


def step_script(name: str) -> str:
    step = WORKFLOW.read_text().split(f"      - name: {name}\n", 1)[1]
    step = step.split("\n      - name:", 1)[0].split("\n  validate:", 1)[0]
    return textwrap.dedent(step.split("        run: |\n", 1)[1])


class WorkflowExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        self.runtime = Path(self.temp.name) / "runtime"
        self.runtime.mkdir()
        self.env = {**os.environ, "RUNNER_TEMP": str(self.runtime),
                    "GITHUB_WORKSPACE": str(self.repo), "BASE_REF": "main"}
        self.git("init", "-b", "main")
        self.git("config", "user.name", "CI test")
        self.git("config", "user.email", "ci@example.invalid")
        self.git("remote", "add", "origin", str(self.repo))
        # Both the branch's merge base and tagged tree carry obsolete policy.
        self.write("scripts/validate_commits.py", "raise SystemExit(0)\n")
        self.write("scripts/scan_committed_secrets.py", "raise SystemExit(0)\n")
        self.base = self.commit("feat: introduce old policy")
        self.git("checkout", "-b", "prerelease")
        self.write("payload.txt", "ordinary test payload\n")
        self.head = self.commit("fix: prepare prerelease")
        self.git("checkout", "main")
        for script in ("validate_commits.py", "scan_committed_secrets.py"):
            self.write("scripts/" + script, (ROOT / "scripts" / script).read_text())
        self.main = self.commit("fix: harden validation policy")
        self.git("checkout", "prerelease")

    def git(self, *args: str) -> str:
        result = subprocess.run(["git", *args], cwd=self.repo, env=self.env,
                                capture_output=True, text=True, check=True)
        return result.stdout.strip()

    def write(self, name: str, content: str) -> None:
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def commit(self, message: str) -> str:
        self.git("add", "--all")
        self.git("commit", "-m", message)
        return self.git("rev-parse", "HEAD")

    def run_step(self, name: str, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", "-euo", "pipefail", "-c", step_script(name)],
                              cwd=self.repo, env={**self.env, **env},
                              capture_output=True, text=True)

    def load_validator(self, event: str = "push", head: str | None = None):
        return self.run_step("Load current main commit validator",
                             EVENT_NAME=event, HEAD_SHA=head or self.head)

    def test_manual_existing_prerelease_tag_runs_current_main_policy(self) -> None:
        self.git("tag", "v0.1.0-rc.1")
        # Evaluate the workflow guard with GitHub's missing-created coercion.
        guard = WORKFLOW.read_text().split("- name: Reject updates to published tags", 1)[1]
        guard = guard.split("if: ${{ ", 1)[1].split(" }}", 1)[0]
        for event, created, expected in (("workflow_dispatch", False, False),
                                          ("push", False, True), ("push", True, False)):
            expression = guard.replace("github.event_name", repr(event))
            expression = expression.replace("startsWith(github.ref, 'refs/tags/')", "True")
            expression = expression.replace("github.event.created", repr(created))
            expression = expression.replace("false", "False").replace("&&", " and ")
            self.assertEqual(eval(expression, {"__builtins__": {}}, {}), expected)
        result = self.load_validator("workflow_dispatch")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_step("Validate proposed commit messages",
                               EVENT_NAME="workflow_dispatch", BASE_SHA="", HEAD_SHA=self.head)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_step("Validate release tags are SemVer 2.0.0",
                               TAG_NAME="v0.1.0-rc.1", TAG_COMMIT=self.head)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_old_merge_base_does_not_supply_commit_or_tag_policy(self) -> None:
        self.assertEqual(self.load_validator().returncode, 0)
        self.assertEqual(self.git("merge-base", self.head, "origin/main"), self.base)
        self.write("payload.txt", "changed\n")
        bad_head = self.commit("invalid commit message")
        result = self.run_step("Validate proposed commit messages", EVENT_NAME="push",
                               BASE_SHA="0" * 40, HEAD_SHA=bad_head)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("header is not conventional", result.stderr)
        for tag in ("v0.1.0-01", "v0.1.0"):
            result = self.run_step("Validate release tags are SemVer 2.0.0",
                                   TAG_NAME=tag, TAG_COMMIT=self.head)
            self.assertNotEqual(result.returncode, 0, tag)

    def test_current_scanner_inspects_intermediate_prerelease_commits(self) -> None:
        # No real secret: the current scanner forbids this filename itself.
        self.write(".env", "EXAMPLE=placeholder\n")
        self.commit("fix: add forbidden environment file")
        (self.repo / ".env").unlink()
        head = self.commit("fix: remove forbidden environment file")
        result = self.run_step("Scan every proposed commit for secrets",
                               EVENT_NAME="push", BASE_SHA="0" * 40, HEAD_SHA=head)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(".env", result.stdout + result.stderr)

    def test_missing_policy_fails_closed_even_when_tagged_copy_exists(self) -> None:
        self.git("checkout", "main")
        self.git("rm", "scripts/validate_commits.py", "scripts/scan_committed_secrets.py")
        self.commit("fix: remove policy")
        self.git("checkout", "prerelease")
        for event in ("push", "workflow_dispatch", "pull_request"):
            result = self.load_validator(event)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("trusted commit validator unavailable", result.stderr)
        result = self.run_step("Scan every proposed commit for secrets", EVENT_NAME="push",
                               BASE_SHA=self.base, HEAD_SHA=self.head)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("trusted secret scanner unavailable", result.stderr)

    def test_initial_validator_introduction_and_first_main_push(self) -> None:
        # Use the fixture's scanner history but a new validator path history.
        self.git("checkout", "--orphan", "bootstrap-main")
        self.git("rm", "-rf", ".")
        self.write("README.md", "bootstrap fixture\n")
        before = self.commit("docs: prepare bootstrap")
        self.git("branch", "-f", "main", before)
        self.git("checkout", "-b", "bootstrap-pr")
        self.write("scripts/validate_commits.py", (ROOT / "scripts/validate_commits.py").read_text())
        head = self.commit("feat: introduce validator")
        result = self.load_validator("pull_request", head)
        self.assertEqual(result.returncode, 0, result.stderr)
        for event in ("push", "workflow_dispatch"):
            result = self.load_validator(event, head)
            self.assertNotEqual(result.returncode, 0)
        self.git("branch", "-f", "main", head)
        result = self.load_validator("push", head)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_step("Validate proposed commit messages", EVENT_NAME="push",
                               BASE_SHA=before, HEAD_SHA=head)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_new_stable_tag_allows_empty_proposed_commit_range(self) -> None:
        self.assertEqual(self.load_validator().returncode, 0)
        result = self.run_step("Validate proposed commit messages", EVENT_NAME="push",
                               BASE_SHA="0" * 40, HEAD_SHA=self.main)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("no commits to validate", result.stdout)

    def release_validation(self, conclusion: str = "success"):
        self.git("tag", "v0.1.0", self.base)
        tools = self.runtime / "bin"
        tools.mkdir()
        gh = tools / "gh"
        checks = [
            {"name": name, "head_sha": self.main, "status": "completed",
             "conclusion": conclusion, "app_id": 15368}
            for name in ("Build without registering a runner",
                         "Enforce conventional commits and pull-request title")
        ]
        gh.write_text("#!/usr/bin/env python3\nimport json\n"
                      + f"checks = {checks!r}\n"
                      + "for check in checks: print(json.dumps(check))\n")
        gh.chmod(0o755)
        return subprocess.run(
            ["bash", str(ROOT / "scripts" / "validate-release.sh"), "v0.1.1", self.main],
            cwd=self.repo, capture_output=True, text=True,
            env={**self.env, "GITHUB_REPOSITORY": "test/repo",
                 "PATH": str(tools) + os.pathsep + self.env["PATH"]},
        )

    def test_prepublication_validation_leaves_tag_absent(self) -> None:
        result = self.release_validation()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("no tag was created", result.stdout)
        self.assertNotIn("v0.1.1", self.git("tag", "--list").splitlines())

    def test_prepublication_validation_rejects_failed_ci(self) -> None:
        result = self.release_validation("failure")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("successful exact-commit check", result.stderr)
        self.assertNotIn("v0.1.1", self.git("tag", "--list").splitlines())

    def test_prepublication_validation_rejects_existing_tag(self) -> None:
        self.git("tag", "v0.1.1", self.main)
        result = self.release_validation()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already exists", result.stderr)

    def test_secret_scan_rejects_an_unresolvable_range(self) -> None:
        result = self.run_step("Scan every proposed commit for secrets", EVENT_NAME="push",
                               BASE_SHA="not-a-commit", HEAD_SHA=self.head)
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
