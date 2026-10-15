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
            "result": {"schema_version": 1, "source": COMMIT, "reviewed_head": HEAD, "tree": TREE, "base": PARENT, "policy": "9" * 40,
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
        for field, value in (("source", HEAD), ("reviewed_head", PARENT), ("tree", HEAD), ("base", HEAD), ("policy", HEAD),
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

    @unittest.skipUnless(os.getuid() == 0, "signer requires root-owned policy")
    def test_signer_and_verifier_reject_unusable_timestamps_before_signing(self):
        import attest_local_validation
        run_dir = self.root / "timestamp-run"; run_dir.mkdir()
        (run_dir / "configuration.log").write_bytes(self.log.read_bytes())
        output = self.root / "issued.json"
        command = ["attest", "--policy", str(self.path), "--result", str(run_dir), "--key", str(self.key),
                   "--reviewed-head", HEAD, "--pull-request", "8", "--output", str(output)]
        cases = [
            {"started_at": self.now - self.policy["local_validation"]["max_age_seconds"] - 1,
             "finished_at": self.now},
            {"started_at": self.now + 1, "finished_at": self.now + 2},
            {"finished_at": self.now + 1},
            {"started_at": self.now - 1, "finished_at": self.now - 2},
            {"started_at": 0}, {"started_at": -1},
        ]
        for field in ("started_at", "finished_at"):
            cases.extend({field: value} for value in (None, True, False, 1.5, "1", [], {}))
            cases.append({field: "missing"})
        for changes in cases:
            with self.subTest(changes=changes):
                payload = copy.deepcopy(self.payload)
                report = payload["result"]
                report.update(changes)
                for field, value in changes.items():
                    if value == "missing":
                        report.pop(field)
                self.sign(payload)
                with self.assertRaisesRegex(Rejected, "local evidence expired or clock is uncertain"):
                    self.check()
                (run_dir / "result.json").write_text(json.dumps(report))
                with patch("sys.argv", command), patch("verify_config_approval.time.time", return_value=self.now), \
                     patch("attest_local_validation.subprocess.run") as signing:
                    with self.assertRaisesRegex(Rejected, "local evidence expired or clock is uncertain"):
                        attest_local_validation.main()
                    signing.assert_not_called()
                self.assertFalse(output.exists())

    @unittest.skipUnless(os.getuid() == 0, "signer requires root-owned policy")
    def test_signer_and_verifier_accept_exact_age_and_current_timestamps(self):
        import attest_local_validation
        run_dir = self.root / "boundary-run"; run_dir.mkdir()
        (run_dir / "configuration.log").write_bytes(self.log.read_bytes())
        output = self.root / "issued.json"
        command = ["attest", "--policy", str(self.path), "--result", str(run_dir), "--key", str(self.key),
                   "--reviewed-head", HEAD, "--pull-request", "8", "--output", str(output)]
        for started in (self.now - self.policy["local_validation"]["max_age_seconds"], self.now):
            with self.subTest(started=started):
                report = dict(self.payload["result"], started_at=started, finished_at=self.now)
                (run_dir / "result.json").write_text(json.dumps(report))
                with patch("sys.argv", command), patch("verify_config_approval.time.time", return_value=self.now):
                    self.assertEqual(attest_local_validation.main(), 0)
                    issued = json.loads(output.read_text())
                    self.assertEqual(json.loads(base64.b64decode(issued["payload"]))["result"], report)
                    with self.assertRaises(FileExistsError):
                        attest_local_validation.main()
                    self.assertEqual(json.loads(output.read_text()), issued)
                output.replace(self.root / (COMMIT + ".json"))
                self.check()

    @unittest.skipUnless(os.getuid() == 0, "signer requires root-owned policy")
    def test_report_expiring_during_signing_creates_no_output(self):
        import attest_local_validation
        run_dir = self.root / "expiring-run"; run_dir.mkdir()
        (run_dir / "configuration.log").write_bytes(self.log.read_bytes())
        report = dict(self.payload["result"], started_at=self.now - 3599)
        (run_dir / "result.json").write_text(json.dumps(report))
        output = self.root / "expired.json"
        command = ["attest", "--policy", str(self.path), "--result", str(run_dir), "--key", str(self.key),
                   "--reviewed-head", HEAD, "--pull-request", "8", "--output", str(output)]
        with patch("sys.argv", command), \
             patch("verify_config_approval.time.time", side_effect=[self.now, self.now + 2]), \
             self.assertRaisesRegex(Rejected, "local evidence expired"):
            attest_local_validation.main()
        self.assertFalse(output.exists())

    @unittest.skipUnless(os.getuid() == 0, "signer requires root-owned policy")
    def test_signer_enforces_verifier_log_and_envelope_limits(self):
        run_dir = self.root / "bounded-run"; run_dir.mkdir()
        output = self.root / "bounded.json"
        command = ["python3", str(ROOT / "scripts/attest_local_validation.py"), "--policy", str(self.path),
                   "--result", str(run_dir), "--key", str(self.key), "--reviewed-head", HEAD,
                   "--pull-request", "8", "--output", str(output)]
        for log_size, tool_size, accepted in ((1024 * 1024, 0, True), (1024 * 1024 + 1, 0, False),
                                               (0, 800 * 1024, False)):
            with self.subTest(log_size=log_size, tool_size=tool_size):
                report = copy.deepcopy(self.payload["result"])
                data = b"x" * log_size
                report["results"][0]["sha256"] = hashlib.sha256(data).hexdigest()
                if tool_size:
                    report["tools"]["large"] = "x" * tool_size
                (run_dir / "result.json").write_text(json.dumps(report))
                (run_dir / "configuration.log").write_bytes(data)
                result = subprocess.run(command, capture_output=True, text=True)
                self.assertEqual(result.returncode == 0, accepted, result.stderr)
                if accepted:
                    output.replace(self.root / (COMMIT + ".json"))
                    self.log.write_bytes(data)
                    self.check()
                else:
                    self.assertIn("size limit", result.stderr)
                    self.assertFalse(output.exists())

    def test_policy_v2_requires_new_capability_for_candidate_and_checkpoint(self):
        from argparse import Namespace
        from desired_state import command_validate_engine_capabilities, DesiredStateError
        old = self.root / "old.json"
        old.write_text(json.dumps({"schema_version": 1, "capabilities": {"configuration_approval_gate": True}}))
        args = Namespace(manifest=old, require_configuration_approval=True, configuration_approval_policy=self.path)
        with patch("verify_config_approval.read_policy", return_value=self.policy), self.assertRaises(DesiredStateError):
            command_validate_engine_capabilities(args)


class RunnerTests(unittest.TestCase):
    def test_missing_procfs_fails_before_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch("local_validate.Path.is_file", return_value=False), \
                 patch("local_validate.subprocess.Popen") as launch, \
                 self.assertRaisesRegex(RuntimeError, "Linux /proc"):
                run(["true"], root, os.environ.copy(), root / "test.log", 5)
            launch.assert_not_called()

    def test_successful_leader_cannot_hide_forced_descendant_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            child = root / "child.py"
            child.write_text("import signal, time\nfrom pathlib import Path\n"
                             "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                             "Path('ready').write_text('ready')\ntime.sleep(60)\n")
            leader = "import subprocess,sys,time; from pathlib import Path; " \
                     "subprocess.Popen([sys.executable, 'child.py']); " \
                     "exec(\"while not Path('ready').exists(): time.sleep(0.01)\")"
            with patch("local_validate.CLEANUP_GRACE_SECONDS", 0.2), \
                 self.assertRaisesRegex(RuntimeError, "forced termination"):
                run(["python3", "-c", leader], root, os.environ.copy(), root / "test.log", 5)

    def test_successful_leader_allows_descendant_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            child = root / "child.py"
            child.write_text("import signal, sys, time\nfrom pathlib import Path\n"
                             "def cleanup(*args):\n    Path('cleaned').write_text('done')\n    sys.exit(0)\n"
                             "signal.signal(signal.SIGTERM, cleanup)\n"
                             "Path('ready').write_text('ready')\ntime.sleep(60)\n")
            leader = "import subprocess,sys,time; from pathlib import Path; " \
                     "subprocess.Popen([sys.executable, 'child.py']); " \
                     "exec(\"while not Path('ready').exists(): time.sleep(0.01)\")"
            with patch("local_validate.CLEANUP_GRACE_SECONDS", 2):
                run(["python3", "-c", leader], root, os.environ.copy(), root / "test.log", 5)
            self.assertEqual((root / "cleaned").read_text(), "done")

    def test_container_stage_propagates_cleanup_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scripts = root / "scripts"; scripts.mkdir()
            (scripts / "test_apt_cache_e2e.py").write_text(
                "import os, sys\nsys.exit(int(os.environ['TEST_STAGE_STATUS']))\n")
            (scripts / "scan_committed_secrets.py").write_text("")
            for name in ("test-cleanup-e2e.sh", "cleanup.sh"):
                path = scripts / name
                path.write_text("#!/bin/bash\nexit 0\n"); path.chmod(0o755)
            docker = root / "docker"
            docker.write_text('''#!/bin/bash
printf '%s\\n' "$*" >> "$TEST_DOCKER_LOG"
case "$1 $2" in
  'compose -f')
    [[ -n ${CI_FLEET_RUNNER_IMAGE_DIGEST:-} ]] || exit 1
    if [[ "$*" == *'--format json' ]]; then
      printf '{"services":{"controller":{"environment":{"CI_FLEET_RUNNER_IMAGE":"%s"}}}}\\n' "$CI_FLEET_RUNNER_IMAGE_DIGEST"
    fi ;;
  'info ') [[ $TEST_CLEANUP_FAILURE != info ]] || exit 1 ;;
  'inspect '*) [[ $TEST_CLEANUP_FAILURE != container-query && $TEST_CLEANUP_FAILURE != absent ]] || exit 1 ;;
  'image inspect') [[ $TEST_CLEANUP_FAILURE != image-query && $TEST_CLEANUP_FAILURE != absent ]] || exit 1 ;;
  'container ls')
    [[ $TEST_CLEANUP_FAILURE != container-query ]] || exit 1
    [[ $TEST_CLEANUP_FAILURE == absent ]] || printf 'fixture-container\\n' ;;
  'image ls')
    [[ $TEST_CLEANUP_FAILURE != image-query ]] || exit 1
    [[ $TEST_CLEANUP_FAILURE == absent ]] || printf 'fixture-image\\n' ;;
  'rm -f') [[ $TEST_CLEANUP_FAILURE != container ]] || exit 1 ;;
  'image rm') [[ $TEST_CLEANUP_FAILURE != image ]] || exit 1 ;;
esac
exit 0
''')
            docker.chmod(0o755)
            log = root / "docker.log"
            env = {**os.environ, "PATH": str(root) + os.pathsep + os.environ["PATH"],
                   "CI_FLEET_VALIDATION_ROOT": str(root), "CI_FLEET_VALIDATION_ID": "cleanup-fixture",
                   "CI_FLEET_RUNNER_IMAGE": "cleanup-fixture-runner:dev",
                   "CI_FLEET_CONTROLLER_IMAGE": "cleanup-fixture-controller:dev",
                   "TEST_DOCKER_LOG": str(log)}
            for failure, stage_status in (("none", 0), ("absent", 0), ("info", 0), ("container", 0), ("image", 0),
                                          ("container-query", 0), ("image-query", 0),
                                          ("none", 7), ("none", 130), ("none", 143)):
                with self.subTest(failure=failure, stage_status=stage_status):
                    log.write_text("")
                    result = subprocess.run(["bash", str(ROOT / "scripts/validate.sh"), "--containers"],
                        env={**env, "TEST_CLEANUP_FAILURE": failure, "TEST_STAGE_STATUS": str(stage_status)},
                        capture_output=True, text=True)
                    self.assertEqual(result.returncode, stage_status if failure in {"none", "absent"} else 1,
                                     result.stdout + result.stderr)
                    for command, omitted in (("rm -f cleanup-fixture-runner-inspect", "container-query"),
                                             ("image rm cleanup-fixture-runner:dev", "image-query"),
                                             ("image rm cleanup-fixture-controller:dev", "image-query")):
                        self.assertEqual(command in log.read_text(), failure not in {"info", "absent", omitted})

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

    @unittest.skipUnless(os.geteuid() == 0, "engine fixtures require root")
    def test_each_stage_gets_fresh_checkouts_and_allows_generated_caches(self):
        import argparse
        import local_validate
        calls = []
        def stage(command, cwd, env, log, timeout):
            for target in (cwd, cwd.parent / "policy"):
                cache = target / "__pycache__/generated.pyc"
                self.assertFalse(cache.exists(), calls)
                cache.parent.mkdir()
                cache.write_bytes(b"fixture cache")
            log.write_text("fixture stage\n")
            calls.append(command)
        output = self.root / "fresh-stages"
        args = argparse.Namespace(repository=self.repo, policy_repository=None, source=self.source,
            base=self.base, policy=self.policy, main=self.policy, output=output, kind="engine", profile="quick",
            pr_title=None, reviewed_head=None, tag=None, tag_created=False, disposable_host=True, timeout=30)
        with patch("local_validate.run", side_effect=stage):
            self.assertEqual(local_validate.validate(args), 0)
        self.assertGreater(len(calls), 1)

    def test_candidate_cannot_mutate_verification_seeds(self):
        self.git("checkout", "main")
        (self.repo / "scripts/validate-configuration.sh").write_text(
            "#!/bin/bash\npython3 -c \"from pathlib import Path; (Path.cwd().parent / 'source-seed/payload').write_text('changed')\"\n")
        self.policy = self.commit("test: attempt verification seed mutation")
        result, report = self.validate()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("checkout changed", report["error"])

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

    def test_candidate_module_cannot_replace_trusted_history_drivers(self):
        for invalid in ("commit", "secret"):
            with self.subTest(invalid=invalid):
                self.git("checkout", "--detach", self.source)
                (self.repo / "subprocess.py").write_text("def run(*args, **kwargs): pass\n")
                if invalid == "secret":
                    (self.repo / ".env").write_text("EXAMPLE=placeholder\n")
                head = self.commit("invalid commit" if invalid == "commit" else "fix: candidate shadows subprocess")
                result, report = self.validate(source=head)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(report["results"][-1]["name"], "commits" if invalid == "commit" else "history-secrets")
                self.assertEqual(report["results"][-1]["status"], "failed")

    def test_candidate_cannot_mutate_policy_source_or_git_metadata(self):
        self.git("checkout", "main")
        (self.repo / "scripts/validate-configuration.sh").write_text("#!/bin/bash\nset -e\npython3 attack.py\n")
        self.policy = self.commit("test: execute candidate mutation fixture")
        attacks = {
            "policy": "(root.parent / 'policy/scripts/validate-configuration.sh').write_text('#!/bin/bash\\nexit 0\\n')",
            "source": "(root / 'runner/Dockerfile').write_text('FROM changed\\n')",
            "hidden-source": "(root / 'runner/Dockerfile').write_text('FROM changed\\n'); subprocess.run(['git', 'update-index', '--assume-unchanged', 'runner/Dockerfile'], check=True)",
            "index": "subprocess.run(['git', 'update-index', '--assume-unchanged', 'runner/Dockerfile'], check=True)",
            "config": "subprocess.run(['git', 'config', 'core.hooksPath', '/unused-fixture-hooks'], check=True)",
        }
        for name, attack in attacks.items():
            with self.subTest(attack=name):
                self.git("checkout", "--detach", self.source)
                (self.repo / "runner").mkdir()
                (self.repo / "runner/Dockerfile").write_text("FROM scratch\n")
                (self.repo / "attack.py").write_text("from pathlib import Path\nimport subprocess\nroot = Path.cwd()\n" + attack + "\n")
                head = self.commit("test: attempt " + name + " mutation")
                result, report = self.validate(source=head)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(report["results"][-1]["status"], "failed")
                self.assertIn("checkout changed", report["error"])

    def test_integrated_squash_scans_removed_secrets_in_reviewed_history(self):
        (self.repo / ".env").write_text("EXAMPLE=placeholder\n")
        self.commit("fix: add forbidden filename")
        (self.repo / ".env").unlink()
        reviewed = self.commit("fix: remove forbidden filename")
        self.git("checkout", "-b", "integrated", self.base)
        self.git("read-tree", "--reset", "-u", reviewed)
        integrated = self.commit("fix: integrate reviewed source tree")
        result, report = self.validate("--reviewed-head", reviewed, source=integrated)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(report["reviewed_head"], reviewed)
        self.assertEqual(report["results"][-1]["name"], "history-secrets")

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

    def test_execution_tempdir_allows_existing_unprivileged_fixture(self):
        if os.geteuid() != 0:
            self.skipTest("permission fixture requires root")
        self.git("checkout", "main")
        script = "#!/bin/bash\npython3 " + str(ROOT / "scripts/test_apply_docker_network_policy.py") + \
            " ApplyScriptTests.test_testing_mode_accepts_default_tmp_as_non_root\n"
        script += "python3 " + str(ROOT / "scripts/test_endpoint_snapshot.py") + \
            " SnapshotReadbackTests.test_unprivileged_observer_reads_export_but_cannot_read_private_sources\n"
        (self.repo / "scripts/validate-configuration.sh").write_text(script)
        self.policy = self.commit("test: run unprivileged permission fixture")
        result, report = self.validate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report["status"], "passed")

    def test_both_profiles_reject_installed_controller_tester_and_deployer_hosts(self):
        import argparse
        import local_validate
        exists = Path.exists
        for kind in ("engine", "configuration"):
            for marker in ("/etc/ci-fleet", "/etc/ci-fleet-deployer", "/var/lib/ci-fleet-tester", "/opt/ci-fleet-status"):
                with self.subTest(kind=kind, marker=marker):
                    output = self.root / ("installed-" + kind + marker.replace("/", "-"))
                    args = argparse.Namespace(repository=self.repo, policy_repository=None, source=self.source,
                        base=self.base, policy=self.policy, main=self.policy, output=output, kind=kind, profile="quick",
                        pr_title=None, reviewed_head=None, tag=None, tag_created=False, disposable_host=True, timeout=30)
                    with patch("local_validate.Path.exists", side_effect=lambda path: str(path) == marker or exists(path), autospec=True), \
                         patch("sys.stderr", io.StringIO()):
                        self.assertEqual(local_validate.validate(args), 1)
                    report = json.loads((output / "result.json").read_text())
                    self.assertIn("installed fleet host", report["error"])
                    self.assertEqual(report["results"], [])

    def test_both_profiles_reject_partial_role_metadata_and_units(self):
        import argparse
        import local_validate
        exists, is_symlink = Path.exists, Path.is_symlink
        markers = ("/var/lib/ci-fleet-status-installer", "/var/lib/ci-fleet-deployer-container-credentials",
                   "/etc/systemd/system/ci-fleet-status-receiver.service",
                   "/etc/systemd/system/timers.target.wants/ci-fleet-tester-health.timer",
                   "/run/systemd/system/actions.runner.example.service")
        for kind in ("engine", "configuration"):
            for marker in markers:
                for dangling in (False, True):
                    with self.subTest(kind=kind, marker=marker, dangling=dangling):
                        output = self.root / (kind + marker.replace("/", "-") + str(dangling))
                        args = argparse.Namespace(repository=self.repo, policy_repository=None, source=self.source,
                            base=self.base, policy=self.policy, main=self.policy, output=output, kind=kind, profile="quick",
                            pr_title=None, reviewed_head=None, tag=None, tag_created=False, disposable_host=True, timeout=30)
                        with patch("local_validate.Path.exists", autospec=True,
                                   side_effect=lambda p: not dangling if str(p) == marker else exists(p)), \
                             patch("local_validate.Path.is_symlink", autospec=True,
                                   side_effect=lambda p: dangling if str(p) == marker else is_symlink(p)), \
                             patch("local_validate.Path.rglob", return_value=iter([Path(marker)])), \
                             patch("sys.stderr", io.StringIO()):
                            self.assertEqual(local_validate.validate(args), 1)
                        report = json.loads((output / "result.json").read_text())
                        self.assertIn("installed fleet host", report["error"])
                        self.assertEqual(report["results"], [])

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
            pr_title=None, reviewed_head=None, tag=None, tag_created=False, disposable_host=False, timeout=30)
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
