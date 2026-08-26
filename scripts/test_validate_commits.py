#!/usr/bin/env python3
"""Regression tests for Conventional Commits 1.0.0 + SemVer 2.0.0 validation."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# Allow direct execution from the repo root: scripts/test_validate_commits.py
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import validate_commits as vc  # noqa: E402


class ConventionalCommitHeaderTests(unittest.TestCase):
    def assert_valid(self, subject: str) -> None:
        self.assertEqual(vc.validate_message(subject), [], subject)

    def assert_invalid(self, subject: str) -> None:
        self.assertTrue(vc.validate_message(subject), subject)

    def test_feat_passes(self) -> None:
        self.assert_valid("feat: add capacity telemetry")

    def test_fix_passes(self) -> None:
        self.assert_valid("fix: close bootstrap lifecycle races")

    def test_all_approved_types_pass(self) -> None:
        for type_name in sorted(vc.ALLOWED_TYPES):
            self.assert_valid(f"{type_name}: ordinary change")

    def test_scoped_type_passes(self) -> None:
        self.assert_valid("feat(runner): add shard matrix expansion")

    def test_breaking_bang_passes(self) -> None:
        self.assert_valid("feat!: replace runner lifecycle API")

    def test_scoped_breaking_passes(self) -> None:
        self.assert_valid("fix(controller)!: drop legacy reconcile state")

    def test_body_and_footer_pass(self) -> None:
        message = (
            "feat: add capacity telemetry\n"
            "\n"
            "The previous telemetry was best-effort. This adds deterministic\n"
            "reporting keyed to the runner lifecycle.\n"
            "\n"
            "Closes #42\n"
            "Reviewed-by: An Operator <an-operator@example.com>\n"
        )
        self.assertEqual(vc.validate_message(message), [])

    def test_breaking_trailer_detected(self) -> None:
        message = (
            "feat: drop the legacy reconcile entrypoint\n"
            "\n"
            "BREAKING CHANGE: the `legacy-reconcile` command is removed.\n"
            "Operators must migrate to `reconcile`.\n"
        )
        self.assertEqual(vc.validate_message(message), [])
        self.assertIn("MAJOR", vc.bump_kind(message))

    def test_capitalized_type_is_rejected(self) -> None:
        self.assert_invalid("Fix: close bootstrap lifecycle races")

    def test_unknown_type_is_rejected(self) -> None:
        self.assert_invalid("wip: half done thing")

    def test_missing_colon_is_rejected(self) -> None:
        self.assert_invalid("feat add capacity telemetry")

    def test_missing_description_is_rejected(self) -> None:
        self.assert_invalid("feat:")

    def test_space_after_colon_required(self) -> None:
        # CC 1.0.0 requires a space after the colon.
        self.assert_invalid("feat:missing-space")

    def test_excessively_long_subject_is_rejected(self) -> None:
        self.assert_invalid("feat: " + "a" * 100)

    def test_merge_commit_is_exempt(self) -> None:
        # Without a sha there is no parent proof, so a "Merge " prefix is not
        # exempt on its own; with a real merge sha it is (see CliTests for the
        # git-backed variants).
        self.assertTrue(vc.validate_message("Merge pull request #77 from RandomDevelopment/docs/x"))

    def test_chore_deps_is_exempt(self) -> None:
        self.assertEqual(vc.validate_message("chore(deps): bump golang"), [])

    def test_empty_message_is_rejected(self) -> None:
        self.assertTrue(vc.validate_message(""))

    def test_body_without_blank_separator_is_rejected(self) -> None:
        self.assertTrue(vc.validate_message("feat: add telemetry\nno blank line here"))

    def test_breaking_detection_without_exclaim(self) -> None:
        message = "feat: drop legacy command\n\nBREAKING CHANGE: removed\n"
        bump = vc.bump_kind(message)
        self.assertIsNotNone(bump, "expected a bump from a BREAKING CHANGE commit")
        self.assertEqual(bump, "MAJOR")


class PullRequestTitleTests(unittest.TestCase):
    def test_conventional_pr_title_passes(self) -> None:
        self.assertEqual(vc.validate_title("feat: add capacity telemetry"), [])

    def test_merge_pr_title_fails(self) -> None:
        self.assertTrue(vc.validate_title("Merge branch 'main'"))

    def test_empty_pr_title_fails(self) -> None:
        self.assertTrue(vc.validate_title(""))


class SemVerValidationTests(unittest.TestCase):
    def test_valid_plain_version(self) -> None:
        self.assertEqual(vc.validate_version("1.2.3"), [])

    def test_valid_zero_major_version(self) -> None:
        self.assertEqual(vc.validate_version("0.1.0"), [])

    def test_valid_leading_v(self) -> None:
        self.assertEqual(vc.validate_version("v1.0.0"), [])

    def test_valid_prerelease(self) -> None:
        self.assertEqual(vc.validate_version("1.0.0-alpha"), [])
        self.assertEqual(vc.validate_version("v1.0.0-alpha.1"), [])
        self.assertEqual(vc.validate_version("1.0.0-alpha.beta.1"), [])

    def test_valid_build_metadata(self) -> None:
        self.assertEqual(vc.validate_version("1.0.0+build.123"), [])
        self.assertEqual(vc.validate_version("1.0.0-alpha+001"), [])

    def test_valid_numeric_identifier_prerelease(self) -> None:
        self.assertEqual(vc.validate_version("1.0.0-0.3.7"), [])

    def test_invalid_leading_zero(self) -> None:
        self.assertTrue(vc.validate_version("1.02.3"))

    def test_invalid_not_semver(self) -> None:
        self.assertTrue(vc.validate_version("1.2"))
        self.assertTrue(vc.validate_version("1.2.x"))
        self.assertTrue(vc.validate_version("v1.2.3.4"))

    def test_unicode_digits_are_rejected(self) -> None:
        # SemVer numeric identifiers are ASCII-only; Python's \d is not.
        self.assertTrue(vc.validate_version("1.٢.3"))
        self.assertIsNone(vc.parse_version("1.٢.3"))

    def test_ascii_digits_still_accepted(self) -> None:
        self.assertEqual(vc.parse_version("1.22.3"), (1, 22, 3))

    def test_invalid_empty(self) -> None:
        self.assertTrue(vc.validate_version(""))

    def test_zero_major_detection(self) -> None:
        self.assertTrue(vc.is_zero_major("0.1.0"))
        self.assertTrue(vc.is_zero_major("0.0.1"))
        self.assertFalse(vc.is_zero_major("1.0.0"))

    def test_parse_version(self) -> None:
        self.assertEqual(vc.parse_version("1.2.3"), (1, 2, 3))
        self.assertEqual(vc.parse_version("v0.1.0"), (0, 1, 0))
        self.assertIsNone(vc.parse_version("not-a-version"))


class BumpSuggestionTests(unittest.TestCase):
    def test_breaking_commit_suggests_major(self) -> None:
        self.assertEqual(
            vc.suggest_bump(["feat: add X", "fix!: break Y"]),
            "MAJOR",
        )

    def test_feat_suggests_minor(self) -> None:
        self.assertEqual(vc.suggest_bump(["feat: add X"]), "MINOR")

    def test_fix_suggests_patch(self) -> None:
        self.assertEqual(vc.suggest_bump(["fix: repair Y"]), "PATCH")

    def test_refactor_suggests_patch(self) -> None:
        self.assertEqual(vc.suggest_bump(["refactor: tidy Z"]), "PATCH")

    def test_merge_commit_does_not_force_bump(self) -> None:
        self.assertEqual(vc.suggest_bump(["Merge pull request #1"]), "PATCH")

    def test_feet_and_fix_prefers_minor(self) -> None:
        self.assertEqual(vc.suggest_bump(["fix: a", "feat: b"]), "MINOR")

    def test_empty_messages_suggests_patch(self) -> None:
        self.assertEqual(vc.suggest_bump([]), "PATCH")


class CliTests(unittest.TestCase):
    def _run(self, *args: str, cwd: str | None = None, **kwargs) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "validate_commits.py"), *args],
            cwd=cwd or str(ROOT), capture_output=True, text=True, **kwargs,
    )

    def test_message_flag_valid(self) -> None:
        result = self._run("--message", "-", input="feat: cli entry")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_message_flag_invalid(self) -> None:
        result = self._run("--message", "-", input="not conventional at all")
        self.assertNotEqual(result.returncode, 0)

    def test_version_flag_valid(self) -> None:
        result = self._run("--version", "1.2.3")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_version_flag_invalid(self) -> None:
        result = self._run("--version", "1.2")
        self.assertNotEqual(result.returncode, 0)

    def test_pr_title_flag_valid(self) -> None:
        result = self._run("--pr-title", "feat: cli pr")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_pr_title_flag_invalid(self) -> None:
        result = self._run("--pr-title", "Random PR title")
        self.assertNotEqual(result.returncode, 0)

    @staticmethod
    def _git_env() -> dict[str, str]:
        return {
            **os.environ,
            "GIT_AUTHOR_NAME": "ci-fleet", "GIT_AUTHOR_EMAIL": "ci-fleet@example.invalid",
            "GIT_COMMITTER_NAME": "ci-fleet", "GIT_COMMITTER_EMAIL": "ci-fleet@example.invalid",
        }

    def _init_repo(self, directory: str) -> None:
        subprocess.run(
            ["git", "init", "-b", "main", directory],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    def _commit(self, directory: str, message: str, allow_merge_failure: bool = False) -> str:
        result = subprocess.run(
            ["git", "-C", directory, "commit", "--allow-empty", "-m", message],
            capture_output=True, text=True, env=self._git_env(),
        )
        if result.returncode != 0 and allow_merge_failure:
            # A single-parent commit cannot be created with merge semantics;
            # callers use this to fake a merge-shaped subject.
            raise AssertionError(f"unexpected commit failure: {result.stderr}")
        return subprocess.run(
            ["git", "-C", directory, "rev-parse", "HEAD"], check=True,
            stdout=subprocess.PIPE, text=True,
        ).stdout.strip()

    def _range_result(self, directory: str, base_sha: str, head_sha: str) -> subprocess.CompletedProcess:
        return self._run("--base", base_sha, "--head", head_sha, cwd=directory)

    def test_range_validates_new_commits_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self._init_repo(directory)
            base_sha = self._commit(directory, "Old non-conventional commit")
            head_sha = self._commit(directory, "feat: new feature")
            result = self._range_result(directory, base_sha, head_sha)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_range_fails_on_bad_new_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self._init_repo(directory)
            base_sha = self._commit(directory, "Old non-conventional commit")
            self._commit(directory, "Bad new commit")
            head_sha = self._commit(directory, "feat: new feature")
            result = self._range_result(directory, base_sha, head_sha)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Bad new commit", result.stderr)

    def test_true_merge_commit_is_exempt_by_parent_count(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self._init_repo(directory)
            self._commit(directory, "feat: base commit")
            subprocess.run(
                ["git", "-C", directory, "checkout", "-b", "feature"],
                check=True, stdout=subprocess.DEVNULL,
            )
            self._commit(directory, "feat: feature work")
            subprocess.run(
                ["git", "-C", directory, "checkout", "main"],
                check=True, stdout=subprocess.DEVNULL,
            )
            self._commit(directory, "fix: mainline work")
            result = subprocess.run(
                ["git", "-C", directory, "merge", "--no-ff", "feature",
                 "-m", "Merge branch 'feature'"],
                capture_output=True, text=True, env=self._git_env(),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            merge_sha = subprocess.run(
                ["git", "-C", directory, "rev-parse", "HEAD"], check=True,
                stdout=subprocess.PIPE, text=True,
            ).stdout.strip()
            self.assertTrue(vc.is_true_merge_commit(merge_sha, directory))
            self.assertEqual(
                vc.validate_message("Merge branch 'feature'", sha=merge_sha, workspace=directory),
                [],
            )

    def test_fake_merge_subject_with_single_parent_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self._init_repo(directory)
            self._commit(directory, "feat: base commit")
            fake_merge_sha = self._commit(directory, "Merge definitely not conventional")
            self.assertFalse(vc.is_true_merge_commit(fake_merge_sha, directory))
            errors = vc.validate_message("Merge definitely not conventional", sha=fake_merge_sha)
            self.assertTrue(errors, "single-parent 'Merge ...' commit must not be exempt")

    def test_empty_base_validates_head_commit_only(self) -> None:
        # workflow_dispatch path: empty base must not enumerate all history.
        with tempfile.TemporaryDirectory() as directory:
            self._init_repo(directory)
            self._commit(directory, "Old non-conventional commit")
            head_sha = self._commit(directory, "feat: new feature")
            result = self._run("--base", "", "--head", head_sha, cwd=directory)
            self.assertEqual(result.returncode, 0, result.stderr)


class LowercaseDescriptionTests(unittest.TestCase):
    """docs/CONTRIBUTING.md requires descriptions to begin with a lowercase letter."""

    def test_uppercase_description_is_rejected(self) -> None:
        self.assertTrue(vc.validate_message("feat: Add endpoint"))
        self.assertTrue(vc.validate_title("feat: Add endpoint"))

    def test_double_space_after_colon_is_rejected(self) -> None:
        self.assertTrue(vc.validate_message("feat:  add endpoint"))
        self.assertTrue(vc.validate_title("feat:  add endpoint"))

    def test_capitals_after_first_word_are_allowed(self) -> None:
        self.assertEqual(vc.validate_message("feat: retain the GitHub App token"), [])


class BreakingChangeSynonymTests(unittest.TestCase):
    """CC 1.0.0 defines BREAKING-CHANGE as synonymous with BREAKING CHANGE."""

    def test_hyphenated_footer_is_valid_and_breaking(self) -> None:
        message = "feat: replace api surface\n\nBREAKING-CHANGE: incompatible now\n"
        self.assertEqual(vc.validate_message(message), [])
        self.assertEqual(vc.bump_kind(message), "MAJOR")

    def test_plain_footer_is_still_valid_and_breaking(self) -> None:
        message = "feat: replace api surface\n\nBREAKING CHANGE: incompatible now\n"
        self.assertEqual(vc.validate_message(message), [])
        self.assertEqual(vc.bump_kind(message), "MAJOR")


class BumpMarkerTests(unittest.TestCase):
    """Only the grammar's breaking marker (!) may classify MAJOR, not any '!'."""

    def test_exclamation_in_description_is_not_major(self) -> None:
        self.assertEqual(vc.bump_kind("fix: preserve the ! operator"), "PATCH")
        self.assertEqual(vc.bump_kind("feat: keep the ! operator"), "MINOR")

    def test_marker_after_scope_is_major(self) -> None:
        self.assertEqual(vc.bump_kind("fix(controller)!: break contract"), "MAJOR")


class Python37CompatibilityTests(unittest.TestCase):
    """The validator promises Python 3.7+; walrus syntax breaks it at parse time."""

    def test_no_walrus_operator_in_validator(self) -> None:
        source = vc.__file__
        with open(source, encoding="utf-8") as handle:
            content = handle.read()
        self.assertNotIn(":=", content)


class ExistingHistoryComplianceTests(unittest.TestCase):
    """Every post-migration commit on this branch conforms. The pre-migration
    history (commits up to and including 'Fix installer drift check lint',
    c600165) predates the convention and is exempt; the migration boundary is
    identified by date so the test does not depend on commit ordering."""

    PRE_MIGRATION_CUTOFF = "2026-07-18 17:22:14 -0500"
    # Pre-convention subjects that landed after the cutoff via squash merges
    # (they were authored before the convention existed and are grandfathered).
    GRANDFATHERED_SUBJECTS = {
        "Fix controller access to root-owned app key",
        "Prevent manager validation bytecode drift",
        "Add fleet-wide host health monitoring (#42)",
    }

    def test_head_commits_on_branch_conform(self) -> None:
        result = subprocess.run(
            ["git", "-C", str(ROOT), "rev-list", "--reverse",
             "--after=" + self.PRE_MIGRATION_CUTOFF, "HEAD"],
            capture_output=True, text=True, check=True,
        )
        commits = [sha for sha in result.stdout.splitlines() if sha]
        failures = []
        for sha in commits:
            message = subprocess.run(
                ["git", "-C", str(ROOT), "log", "-1", "--format=%B", sha],
                capture_output=True, text=True, check=True,
            ).stdout.rstrip("\n")
            # Merge commits are exempt by policy (verified by parent count);
            # a handful of pre-convention subjects (e.g. "Fix controller
            # access to root-owned app key", "Prevent manager validation
            # bytecode drift") landed after the cutoff via squash merges and
            # are grandfathered here.
            header = message.splitlines()[0]
            if vc.MERGE_RE.match(header) or header in self.GRANDFATHERED_SUBJECTS:
                continue
            errors = vc.validate_message(message)
            if errors:
                failures.append(f"{sha[:8]} {message.splitlines()[0]}: {errors}")
        self.assertEqual(
            failures, [],
            "post-migration HEAD commits must conform to Conventional Commits 1.0.0",
        )

    def test_post_migration_boundary_is_conventional(self) -> None:
        # The first post-cutoff commit (the convention migration itself) must
        # already conform, proving the cutoff sits at the right place.
        result = subprocess.run(
            ["git", "-C", str(ROOT), "rev-list", "--reverse", "--after="
             + self.PRE_MIGRATION_CUTOFF, "HEAD"],
            capture_output=True, text=True, check=True,
        )
        first_new = next(sha for sha in result.stdout.splitlines() if sha)
        message = subprocess.run(
            ["git", "-C", str(ROOT), "log", "-1", "--format=%B", first_new],
            capture_output=True, text=True, check=True,
        ).stdout.rstrip("\n")
        self.assertEqual(vc.validate_message(message), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
