#!/usr/bin/env python3
"""Regression tests for the tag-validation step in .github/workflows/validate.yml.

Finding 3861004945: the release-tag step must run the validator extracted from
a trusted revision (the merge base with origin/main), never the tagged-tree
copy, so a branch-local commit cannot weaken its own tag policy.
"""

from __future__ import annotations

import re
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

    def test_tag_step_uses_trusted_base_extraction(self) -> None:
        step = self._tag_step()
        self.assertIn("trusted-validator.py", step)
        self.assertIn("merge-base", step)
        self.assertIn("git show", step)


if __name__ == "__main__":
    unittest.main(verbosity=2)
