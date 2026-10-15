#!/usr/bin/env python3
"""Test isolated execution and signed evidence without Actions or credentials."""
from __future__ import annotations

import base64
import copy
import io
import hashlib
import json
import os
import shutil
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from local_validate import run
from verify_config_approval import Rejected, policy_digest, read_policy, verify, verify_local_evidence
from test_config_approval import COMMIT, HEAD, TREE, PARENT, POLICY, REPOSITORY, FixtureAPI, evidence

ROOT = Path(__file__).resolve().parents[1]


class LocalEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path.home())
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.key = self.root / "signing-key"
        subprocess.run(["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(self.key)], check=True, capture_output=True)
        public = subprocess.check_output(["openssl", "pkey", "-in", str(self.key), "-pubout"], text=True)
        self.policy = {k: v for k, v in POLICY.items() if k not in {"workflow_id", "required_jobs"}}
        self.policy.update(schema_version=2, local_validation={"directory": str(self.root), "public_key": public,
            "validator_revision": "9" * 40, "suite": "ci-fleet-config-local-v1", "required_checks": ["configuration"],
            "max_age_seconds": 3600})
        self.path = self.root / "config-review-policy.json"
        self.path.write_text(json.dumps(self.policy)); self.path.chmod(0o600)
        marker = self.root / "config-review-required"
        marker.write_text("1\n"); marker.chmod(0o600)
        logs = self.root / (COMMIT + ".logs"); logs.mkdir(mode=0o700)
        self.log = logs / "configuration.log"
        self.log.write_bytes(b"independently observed validation output\n"); self.log.chmod(0o600)
        self.now = int(time.time())
        self.payload = {"schema_version": 1, "policy_sha256": policy_digest(self.policy), "repository_id": 101,
            "branch": "main", "pull_request": 8, "reviewed_head": HEAD,
            "result": {"schema_version": 1, "source": COMMIT, "tree": TREE, "base": PARENT, "policy": "9" * 40,
                "suite": "ci-fleet-config-local-v1", "profile": "full", "status": "passed", "expected_skips": [],
                "started_at": self.now - 20, "finished_at": self.now - 1, "tools": {"python": "Python fixture"},
                "results": [{"name": "configuration", "status": "passed", "sha256": hashlib.sha256(self.log.read_bytes()).hexdigest()}]}}
        self.sign()

    def sign(self, value=None):
        raw = json.dumps(self.payload if value is None else value).encode()
        data = self.root / "payload"; data.write_bytes(raw)
        signature = subprocess.check_output(["openssl", "pkeyutl", "-sign", "-rawin", "-inkey", str(self.key), "-in", str(data)])
        path = self.root / (COMMIT + ".json")
        path.write_text(json.dumps({"payload": base64.b64encode(raw).decode(), "signature": base64.b64encode(signature).decode()}))
        path.chmod(0o600)

    def check(self):
        verify_local_evidence(self.policy, COMMIT, TREE, PARENT, HEAD, 8, owner=os.getuid(), now=self.now)

    def test_signed_complete_evidence_and_policy(self):
        self.assertEqual(read_policy(self.path, os.getuid()), self.policy)
        self.check()

    def test_actions_unavailable_is_accepted_only_with_valid_local_evidence(self):
        class NoActions(FixtureAPI):
            def get(self, path, query=None):
                if "/actions/" in path:
                    raise Rejected("Actions unavailable or quota blocked")
                return super().get(path, query)
        api = NoActions(evidence())
        # Fixed production owner is root. Test the filesystem path through the same verifier.
        with patch("verify_config_approval.verify_local_evidence", side_effect=lambda *args: self.check()):
            verify(api, self.policy, REPOSITORY, COMMIT, TREE)
            (self.root / (COMMIT + ".json")).unlink()
            with self.assertRaises(OSError):
                verify(api, self.policy, REPOSITORY, COMMIT, TREE)
        self.assertFalse(any("/actions/" in path for path in api.paths))
        with self.assertRaises(Rejected):
            verify(api, POLICY, REPOSITORY, COMMIT, TREE)

    def test_independent_review_and_tree_rules_still_apply(self):
        for path, value in ((f"/repos/{REPOSITORY}/pulls/8/reviews", []),
                            (f"/repos/{REPOSITORY}/git/commits/{HEAD}", {"sha": HEAD, "tree": {"sha": PARENT}})):
            data = evidence(); data[path] = value
            with self.subTest(path=path), self.assertRaises(Rejected):
                verify(FixtureAPI(data), self.policy, REPOSITORY, COMMIT, TREE)

    def test_every_identity_and_incomplete_result_fails_even_with_valid_signature(self):
        changes = [((), "repository_id", 999), ((), "branch", "other"), ((), "pull_request", 99),
                   ((), "reviewed_head", PARENT), ((), "policy_sha256", "0" * 64)]
        for field, value in (("source", HEAD), ("tree", HEAD), ("base", HEAD), ("policy", HEAD),
                             ("suite", "other"), ("profile", "quick"), ("status", "failed"),
                             ("expected_skips", ["configuration"]), ("tools", {}), ("results", []),
                             ("started_at", self.now - 4000), ("finished_at", self.now + 1)):
            changes.append((("result",), field, value))
        for keys, field, value in changes:
            changed = copy.deepcopy(self.payload); target = changed
            for key in keys: target = target[key]
            target[field] = value
            self.sign(changed)
            with self.subTest(field=field), self.assertRaises(Rejected): self.check()

    def test_tampered_unsigned_missing_and_unsafe_files_reject(self):
        path = self.root / (COMMIT + ".json")
        value = json.loads(path.read_text()); value["signature"] = base64.b64encode(b"x" * 64).decode()
        path.write_text(json.dumps(value))
        with self.assertRaises(Rejected): self.check()
        self.sign(); self.log.write_text("tampered")
        with self.assertRaises(Rejected): self.check()
        self.log.write_bytes(b"independently observed validation output\n")
        self.log.chmod(0o644)
        with self.assertRaises(Rejected): self.check()
        self.log.chmod(0o600); path.unlink()
        with self.assertRaises(OSError): self.check()
        path.symlink_to(self.root / "payload")
        with self.assertRaises(OSError): self.check()

    def test_signing_helper_and_offline_last_known_good_recovery(self):
        run_dir = self.root / "verified-run"; run_dir.mkdir()
        (run_dir / "result.json").write_text(json.dumps(self.payload["result"]))
        (run_dir / "configuration.log").write_bytes(self.log.read_bytes())
        output = self.root / "issued.json"
        command = ["python3", str(ROOT / "scripts/attest_local_validation.py"), "--policy", str(self.path),
                   "--result", str(run_dir), "--key", str(self.key), "--reviewed-head", HEAD,
                   "--pull-request", "8", "--output", str(output)]
        # Production signing and recovery require root-owned policy. Fixture
        # owner checks are covered separately for non-root test environments.
        if os.getuid() != 0:
            self.skipTest("signer and protected recovery fixtures require root")
        subprocess.run(command, check=True, capture_output=True)
        output.replace(self.root / (COMMIT + ".json")); self.check()
        report = self.payload["result"].copy(); report["profile"] = "quick"
        (run_dir / "result.json").write_text(json.dumps(report))
        self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
        record = self.root / "recovery.json"
        record.write_text(json.dumps({"config_repository": REPOSITORY, "config_ref": COMMIT,
                                     "controller": "fixture", "saved_at": self.now}))
        record.chmod(0o600)
        import verify_config_approval
        args = ["verify", "--policy", str(self.path), "--repository", REPOSITORY, "--commit", COMMIT,
                "--tree", TREE, "--installed-state", str(self.root / "absent"),
                "--recovery-record", str(record), "--controller", "fixture"]
        with patch("sys.argv", args), patch("sys.stdin", io.StringIO()), patch("sys.stdout", io.StringIO()), \
             patch("verify_config_approval.GitHub", side_effect=AssertionError("offline recovery must not call GitHub")):
            self.assertEqual(verify_config_approval.main(), 0)

    def test_explicit_hosted_policy_remains_an_additional_gate(self):
        policy = dict(self.policy, workflow_id=501, required_jobs=["validate"])
        with patch("verify_config_approval.verify_local_evidence"):
            api = FixtureAPI(evidence()); verify(api, policy, REPOSITORY, COMMIT, TREE)
            self.assertTrue(any("/actions/" in path for path in api.paths))
            api.data[f"/repos/{REPOSITORY}/actions/workflows/501/runs"] = {"workflow_runs": []}
            with self.assertRaises(Rejected): verify(api, policy, REPOSITORY, COMMIT, TREE)

    def test_policy_v2_requires_new_capability_for_candidate_and_checkpoint(self):
        from argparse import Namespace
        from desired_state import command_validate_engine_capabilities, DesiredStateError
        old = self.root / "old.json"
        old.write_text(json.dumps({"schema_version": 1, "capabilities": {"configuration_approval_gate": True}}))
        args = Namespace(manifest=old, require_configuration_approval=True, configuration_approval_policy=self.path)
        with patch("verify_config_approval.read_policy", return_value=self.policy), self.assertRaises(DesiredStateError):
            command_validate_engine_capabilities(args)


class RunnerTests(unittest.TestCase):
    def test_failure_and_timeout_do_not_pass(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for command, error in ((["bash", "-c", "exit 3"], RuntimeError), (["bash", "-c", "sleep 30"], subprocess.TimeoutExpired)):
                with self.subTest(command=command), self.assertRaises(error):
                    run(command, root, os.environ.copy(), root / "test.log", 1)

    def test_pinned_configuration_validator_rejects_candidate_bypass(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); repo = root / "config"
            shutil.copytree(ROOT / "templates/config-repository", repo)
            def git(*args):
                return subprocess.check_output(["git", "-C", str(repo), *args], text=True, stderr=subprocess.DEVNULL).strip()
            git("init", "-b", "main")
            git("config", "user.name", "Validation test"); git("config", "user.email", "test@example.invalid")
            path = repo / "fleet.json"
            fleet = json.loads(path.read_text().replace("example-org", "validation-company").replace("example-app", "validation-app"))
            for architectures in fleet["managed_images"].values():
                for images in architectures.values():
                    images.update(controller="sha256:" + "3" * 64, runner="sha256:" + "4" * 64)
            for controller in fleet["controllers"].values():
                controller.pop("docker_network_policy", None)
            path.write_text(json.dumps(fleet))
            git("add", "."); git("commit", "-m", "feat: initialize fictional configuration")
            base = git("rev-parse", "HEAD")
            # Exercise the current trusted plan directly so the regression also
            # runs before this change has a committed policy identity.
            env = {**os.environ, "CI_FLEET_VALIDATION_ROOT": str(repo), "CI_FLEET_VALIDATION_BASE": base}
            for bypass in (False, True):
                if bypass:
                    (repo / "scripts/validate.py").write_text("raise SystemExit(0)\n")
                    fleet["environments"]["production"]["requires_approval"] = False
                    path.write_text(json.dumps(fleet)); git("add", "."); git("commit", "-m", "test: attempt validator bypass")
                result = subprocess.run(["bash", str(ROOT / "scripts/validate-configuration.sh")], cwd=repo, env=env,
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode == 0, not bypass, result.stdout + result.stderr)
                if bypass:
                    self.assertIn("production deployment must require approval", result.stdout + result.stderr)

    def test_source_requires_full_resolvable_identities(self):
        result = subprocess.run(["python3", str(ROOT / "scripts/local_validate.py"), "--source", "HEAD",
                                 "--base", "HEAD", "--policy", "HEAD", "--main", "HEAD", "--output", "/unused"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("full available commit SHAs", result.stderr)


class PolicyExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"; self.repo.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Validation test")
        self.git("config", "user.email", "test@example.invalid")
        (self.repo / "payload").write_text("base")
        self.base = self.commit("feat: initial fixture")
        (self.repo / "scripts").mkdir()
        for script in ("validate_commits.py", "scan_committed_secrets.py"):
            (self.repo / "scripts" / script).write_text((ROOT / "scripts" / script).read_text())
        (self.repo / "scripts/validate-configuration.sh").write_text("#!/bin/bash\nset -eu\ntest -f payload\n")
        self.policy = self.commit("fix: establish trusted policy")
        self.git("checkout", "-b", "candidate", self.base)
        (self.repo / "scripts").mkdir()
        for script in ("validate_commits.py", "scan_committed_secrets.py"):
            (self.repo / "scripts" / script).write_text("raise SystemExit(0)\n")
        self.source = self.commit("fix: candidate has weaker policies")
        self.count = 0

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], stderr=subprocess.DEVNULL, text=True).strip()

    def commit(self, message):
        self.git("add", "."); self.git("commit", "-m", message)
        return self.git("rev-parse", "HEAD")

    def validate(self, *extra, source=None):
        self.count += 1
        output = self.root / ("run-" + str(self.count))
        result = subprocess.run(["python3", str(ROOT / "scripts/local_validate.py"), "--kind", "configuration",
            "--repository", str(self.repo), "--source", source or self.source, "--base", self.base,
            "--policy", self.policy, "--main", self.policy, "--output", str(output), *extra], capture_output=True, text=True)
        report = json.loads((output / "result.json").read_text()) if (output / "result.json").exists() else None
        return result, report

    def test_no_actions_no_gh_and_concurrent_paths_preserve_the_source(self):
        before = self.git("status", "--porcelain")
        for _ in range(2):
            result, report = self.validate("--pr-title", "fix: valid title")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(report["status"], "passed")
            self.assertEqual(report["source"], self.source)
            self.assertEqual(report["profile"], "full")
            self.assertEqual(report["expected_skips"], [])
        self.assertEqual(self.git("status", "--porcelain"), before)

    def test_bad_commit_and_pr_title_cannot_use_candidate_policy(self):
        result, _ = self.validate("--pr-title", "invalid title")
        self.assertNotEqual(result.returncode, 0)
        (self.repo / "payload").write_text("changed")
        bad = self.commit("invalid commit")
        result, report = self.validate(source=bad)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(report["results"][0]["status"], "failed")

    def test_removed_secret_in_intermediate_history_is_rejected(self):
        (self.repo / ".env").write_text("EXAMPLE=placeholder\n")
        self.commit("fix: add forbidden filename")
        (self.repo / ".env").unlink()
        head = self.commit("fix: remove forbidden filename")
        result, report = self.validate(source=head)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(report["results"][-1]["name"], "history-secrets")
        self.assertEqual(report["results"][-1]["status"], "failed")

    def test_tag_update_invalid_version_and_stable_branch_policy(self):
        for args in (("--tag", "v0.1.0"), ("--tag", "v0.1.0-01", "--tag-created"),
                     ("--tag", "v0.1.0", "--tag-created")):
            result, report = self.validate(*args)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(report["status"], "failed")
        result, report = self.validate("--tag", "v0.1.0-rc.1", "--tag-created")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report["history_base"], "")

    def test_execution_home_has_no_writable_parent(self):
        self.git("checkout", "main")
        (self.repo / "scripts/validate-configuration.sh").write_text(
            "#!/bin/bash\npython3 -c 'from pathlib import Path; assert all(not p.stat().st_mode & 0o022 for p in Path.home().parents)'\n")
        self.policy = self.commit("test: require protected fixture home")
        result, report = self.validate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report["status"], "passed")

    def test_missing_tool_records_incomplete_failure(self):
        import argparse
        import local_validate
        original = subprocess.check_output
        def missing_tool(command, *args, **kwargs):
            if command[:1] == ["shellcheck"]:
                raise FileNotFoundError("shellcheck missing")
            return original(command, *args, **kwargs)
        output = self.root / "missing-tool-run"
        args = argparse.Namespace(repository=self.repo, policy_repository=None, source=self.source, base=self.base,
            policy=self.policy, main=self.policy, output=output, kind="configuration", profile="full",
            pr_title=None, tag=None, tag_created=False, disposable_host=False, timeout=30)
        with patch("local_validate.subprocess.check_output", side_effect=missing_tool), patch("sys.stderr", io.StringIO()):
            self.assertEqual(local_validate.validate(args), 1)
        report = json.loads((output / "result.json").read_text())
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["results"], [])

    def test_unexpected_skip_and_missing_tools_are_failures(self):
        self.git("checkout", "main")
        (self.repo / "scripts/validate-configuration.sh").write_text("#!/bin/bash\necho 'OK (skipped=1)'\n")
        self.policy = self.commit("fix: skipped fixture")
        result, report = self.validate()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unexpected skipped tests", result.stderr)
        self.assertEqual(report["status"], "failed")


if __name__ == "__main__":
    unittest.main()
