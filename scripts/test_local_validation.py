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
                             ("base", "0" * 40),
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
    def test_signer_rejects_private_keys_that_do_not_match_policy(self):
        run_dir = self.root / "key-mismatch-run"; run_dir.mkdir()
        (run_dir / "result.json").write_text(json.dumps(self.payload["result"]))
        (run_dir / "configuration.log").write_bytes(self.log.read_bytes())
        output = self.root / "wrong-key-evidence.json"
        for algorithm in ("ED25519", "RSA"):
            with self.subTest(algorithm=algorithm):
                output.unlink(missing_ok=True)
                wrong = self.root / ("wrong-" + algorithm + ".key")
                generate = ["openssl", "genpkey", "-algorithm", algorithm, "-out", str(wrong)]
                if algorithm == "RSA":
                    generate.extend(("-pkeyopt", "rsa_keygen_bits:2048"))
                subprocess.run(generate, check=True, capture_output=True)
                command = ["python3", str(ROOT / "scripts/attest_local_validation.py"), "--policy", str(self.path),
                           "--result", str(run_dir), "--key", str(wrong), "--reviewed-head", HEAD,
                           "--pull-request", "8", "--output", str(output)]
                result = subprocess.run(command, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(output.exists())

    def test_signer_rejects_nonpositive_pr_ids_before_reading_policy_or_signing(self):
        import attest_local_validation
        output = self.root / "invalid-pr-evidence.json"
        for number in (0, -1):
            command = ["attest", "--policy", str(self.path), "--result", str(self.root),
                       "--key", str(self.key), "--reviewed-head", HEAD, "--pull-request", str(number),
                       "--output", str(output)]
            with self.subTest(number=number), patch("sys.argv", command), \
                    patch("attest_local_validation.read_policy") as policy, \
                    patch("attest_local_validation.subprocess.run") as signing, \
                    self.assertRaisesRegex(Rejected, "pull-request number must be a positive ID"):
                attest_local_validation.main()
            policy.assert_not_called()
            signing.assert_not_called()
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
    def test_checkout_mutation_diagnostics_identify_paths_and_kinds_without_contents(self):
        from local_validate import checkout_manifest, verify_checkout
        for change in ("content", "mode", "type", "removed"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / ".git").mkdir()
                path = root / ".git/config"
                path.write_text("original private contents")
                path.chmod(0o600)
                expected = checkout_manifest(root)
                if change == "content":
                    path.write_text("replacement private contents")
                elif change == "mode":
                    path.chmod(0o644)
                else:
                    path.unlink()
                    if change == "type":
                        path.symlink_to("private-target")
                with self.assertRaises(RuntimeError) as failure:
                    verify_checkout(root, expected)
                message = str(failure.exception)
                self.assertIn("'.git/config' (file " + change, message)
                for private in ("original private contents", "replacement private contents", "private-target"):
                    self.assertNotIn(private, message)

    def test_mutable_installer_fixture_setup_preserves_source_git_and_tracked_edits(self):
        from local_validate import checkout_manifest
        for script in ("test-install-worker-controller.sh", "test-install-deployer.sh", "test-install-tester.sh"):
            with self.subTest(script=script), tempfile.TemporaryDirectory() as temporary:
                text = (ROOT / "scripts" / script).read_text()
                start = text.index("fixture_repo=$tmp/core\n")
                end = text.index("repo_root=$fixture_repo\n", start) + len("repo_root=$fixture_repo\n")
                setup = text[start:end]
                root = Path(temporary)
                source = root / "source"; source.mkdir()
                fixture_root = root / "fixture"; fixture_root.mkdir()
                def git(repo, *args):
                    return subprocess.check_output(["git", "-C", str(repo), *args], text=True, stderr=subprocess.DEVNULL).strip()
                git(source, "init", "-q")
                git(source, "config", "user.name", "Validation test")
                git(source, "config", "user.email", "test@example.invalid")
                (source / "tracked").write_text("initial\n")
                (source / "binary").write_bytes(b"initial\x00bytes")
                cached = source / "stat-cache-only"
                cached.write_text("unchanged\n")
                git(source, "add", "."); git(source, "commit", "-qm", "feat: initial source")
                (source / "tracked").write_text("committed\n")
                git(source, "add", "."); git(source, "commit", "-qm", "fix: second source commit")
                head = git(source, "rev-parse", "HEAD")
                (source / "tracked").write_text("staged\n")
                git(source, "add", "tracked")
                (source / "tracked").write_text("staged and unstaged\n")
                (source / "binary").write_bytes(b"changed\x00bytes")
                previous = cached.stat()
                os.utime(cached, ns=(previous.st_atime_ns, previous.st_mtime_ns + 2_000_000_000))
                expected = checkout_manifest(source)
                subprocess.run(["bash", "-euo", "pipefail", "-c", "umask 077\n" + setup], check=True, capture_output=True,
                               env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "repo_root": str(source), "tmp": str(fixture_root)})
                fixture = fixture_root / "core"
                self.assertEqual(git(fixture, "rev-parse", "HEAD"), head)
                self.assertEqual(git(fixture, "rev-list", "--count", "HEAD"), "2")
                for name in ("tracked", "binary"):
                    self.assertEqual((fixture / name).read_bytes(), (source / name).read_bytes())
                replacement = git(fixture, "-c", "user.name=Validation test", "-c", "user.email=test@example.invalid",
                                  "commit-tree", "HEAD^{tree}", "-m", "test: synthetic fixture commit")
                git(fixture, "replace", head, replacement)
                self.assertEqual(checkout_manifest(source), expected)
                git(fixture, "replace", "-d", head)
                self.assertEqual(checkout_manifest(source), expected)
                git(fixture, "reset", "--hard", head)
                cached = fixture / "stat-cache-only"
                previous = cached.stat()
                os.utime(cached, ns=(previous.st_atime_ns, previous.st_mtime_ns + 2_000_000_000))
                subprocess.run(["bash", "-euc", 'umask 077; git -C "$1" diff --quiet', "fixture", str(fixture)],
                               check=True, capture_output=True, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
                self.assertEqual(checkout_manifest(source), expected)

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

    def configuration_fixture(self, root):
        repo = root / "config"
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
        return repo, fleet, git

    def test_pinned_configuration_validator_rejects_candidate_bypass(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo, fleet, git = self.configuration_fixture(Path(temporary))
            path = repo / "fleet.json"
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

    def test_no_prior_configuration_rejects_root_capability_and_evidence_staging(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo, fleet, git = self.configuration_fixture(Path(temporary))
            controller = next(iter(fleet["controllers"]))
            evidence = {"schema_version": 1, "status_reporting_engine_capabilities": {
                controller: {"engine_ref": fleet["controllers"][controller]["engine_ref"],
                             "status_reporting_config": True, "required_status_reporting": False}}}
            for attack in ("none", "reporting", "evidence"):
                with self.subTest(attack=attack):
                    candidate = copy.deepcopy(fleet)
                    if attack == "reporting":
                        candidate["controllers"][controller]["status_reporting"] = {
                            "enabled": False, "config_file": "/etc/ci-fleet/monitoring.env"}
                    (repo / "fleet.json").write_text(json.dumps(candidate))
                    (repo / "engine-rollout-evidence.json").write_text(json.dumps(
                        evidence if attack != "none" else {"schema_version": 1, "status_reporting_engine_capabilities": {}}))
                    git("add", "."); git("commit", "--amend", "--no-edit")
                    self.assertEqual(git("rev-list", "--count", "HEAD"), "1")
                    env = {**os.environ, "CI_FLEET_VALIDATION_ROOT": str(repo), "CI_FLEET_VALIDATION_BASE": "0" * 40}
                    result = subprocess.run(["bash", str(ROOT / "scripts/validate-configuration.sh")], cwd=repo, env=env,
                                            capture_output=True, text=True)
                    self.assertEqual(result.returncode == 0, attack == "none", result.stdout + result.stderr)
                    if attack != "none":
                        self.assertIn("previous integrated fleet configuration", result.stderr)
                    if attack == "reporting":
                        self.assertIn("must be omitted from a new controller", result.stderr)

    def test_optional_hosted_job_cannot_emit_the_required_validate_context(self):
        import yaml
        workflow = yaml.safe_load((ROOT / "templates/config-repository/.github/workflows/validate.yml").read_text())
        triggers = workflow.get("on", workflow.get(True))
        self.assertEqual(set(triggers["pull_request"]["types"]), {"opened", "synchronize", "reopened", "edited"})
        jobs = workflow["jobs"]
        self.assertEqual(set(jobs), {"hosted-validation"})
        self.assertNotIn("validate", {job.get("name", key) for key, job in jobs.items()})
        job = jobs["hosted-validation"]
        self.assertEqual(job["if"], "${{ vars.CI_FLEET_HOSTED_VALIDATION == 'true' }}")
        self.assertEqual(job["timeout-minutes"], 5)
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        for step in job["steps"]:
            if "uses" in step:
                self.assertRegex(step["uses"], r"@[0-9a-f]{40}$")
                self.assertIs(step["with"]["persist-credentials"], False)
        engine = (ROOT / ".github/workflows/validate.yml").read_text()
        self.assertIn("name: Build without registering a runner", engine)

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

    def git(self, *args, env=None):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], env=env, stderr=subprocess.DEVNULL, text=True).strip()

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

    def test_replacement_refs_cannot_bind_an_unreviewed_tree(self):
        actual_tree = self.git("rev-parse", self.source + "^{tree}")
        reviewed_tree = self.git("rev-parse", self.policy + "^{tree}")
        self.assertNotEqual(actual_tree, reviewed_tree)
        replacement = self.git("commit-tree", reviewed_tree, "-p", self.base, "-m", "fix: substituted source tree")
        self.git("replace", self.source, replacement)
        control_env = dict(os.environ)
        control_env.pop("GIT_NO_REPLACE_OBJECTS", None)
        self.assertEqual(self.git("rev-parse", self.source + "^{commit}", env=control_env), self.source)
        self.assertEqual(self.git("rev-parse", self.source + "^{tree}", env=control_env), reviewed_tree)
        result, report = self.validate("--reviewed-head", self.policy)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("integrated and reviewed source trees differ", result.stderr)
        self.assertIsNone(report)
        self.assertFalse((self.root / "run-1").exists())

    def test_replacement_refs_and_grafts_cannot_invent_base_ancestry(self):
        tree = self.git("rev-parse", self.source + "^{tree}")
        unrelated = self.git("commit-tree", tree, "-m", "fix: unrelated source root")
        replacement = self.git("commit-tree", tree, "-p", self.base, "-m", "fix: substituted source ancestry")
        for kind in ("replacement", "graft"):
            with self.subTest(kind=kind):
                if kind == "replacement":
                    self.git("replace", unrelated, replacement)
                else:
                    (self.repo / ".git/info/grafts").write_text(unrelated + " " + self.base + "\n")
                control_env = {**os.environ, "GIT_GRAFT_FILE": str(self.repo / ".git/info/grafts")}
                control_env.pop("GIT_NO_REPLACE_OBJECTS", None)
                self.git("merge-base", "--is-ancestor", self.base, unrelated, env=control_env)
                result, report = self.validate(source=unrelated)
                self.assertNotEqual(result.returncode, 0)
                self.assertIsNone(report)
                self.assertFalse((self.root / ("run-" + str(self.count))).exists())
                if kind == "replacement":
                    self.git("replace", "-d", unrelated)

    def test_report_and_stage_git_use_real_objects_without_host_overrides(self):
        import argparse
        import local_validate
        actual_tree = self.git("rev-parse", self.source + "^{tree}")
        replacement = self.git("commit-tree", self.git("rev-parse", self.policy + "^{tree}"),
                               "-p", self.base, "-m", "fix: substituted source tree")
        self.git("replace", self.source, replacement)
        unrelated = self.root / "unrelated.git"
        subprocess.run(["git", "init", "--quiet", "--bare", str(unrelated)], check=True)
        overrides = {"GIT_DIR": str(unrelated), "GIT_WORK_TREE": str(self.root),
                     "GIT_COMMON_DIR": str(unrelated), "GIT_CEILING_DIRECTORIES": str(self.repo.parent),
                     "GIT_NO_REPLACE_OBJECTS": "0", "GIT_GRAFT_FILE": str(self.repo / ".git/info/grafts"),
                     "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.bare", "GIT_CONFIG_VALUE_0": "true",
                     "CI_FLEET_TEST_HOST_SECRET": "must not reach validation stages"}
        expected_git = {"GIT_NO_REPLACE_OBJECTS": "1", "GIT_GRAFT_FILE": "/dev/null",
                        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
                        "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0",
                        "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "advice.graftFileDeprecated", "GIT_CONFIG_VALUE_0": "false"}
        observed = []
        execute = local_validate.run
        def stage(command, cwd, env, log, timeout):
            self.assertEqual({key: value for key, value in env.items() if key.startswith("GIT_")}, expected_git)
            self.assertNotIn("CI_FLEET_TEST_HOST_SECRET", env)
            self.assertEqual(env["PATH"], "/usr/sbin:/usr/bin:/sbin:/bin")
            self.assertNotEqual(env["HOME"], os.environ["HOME"])
            tree = subprocess.check_output(["git", "-C", str(cwd), "rev-parse", "HEAD^{tree}"], env=env, text=True).strip()
            self.assertEqual(tree, actual_tree)
            observed.append(command)
            execute(command, cwd, env, log, timeout)
        output = self.root / "real-object-report"
        args = argparse.Namespace(repository=self.repo, policy_repository=None, source=self.source,
            reviewed_head=None, base=self.base, policy=self.policy, main=self.policy, output=output,
            kind="configuration", profile="full", pr_title=None, tag=None, tag_created=False, disposable_host=False, timeout=30)
        with patch.dict(os.environ, overrides), patch("local_validate.run", side_effect=stage):
            self.assertEqual(local_validate.git_environment({"PATH": "fixture", "GIT_DIR": "untrusted"}),
                             {"PATH": "fixture", **expected_git})
            self.assertEqual(local_validate.git_environment({}), expected_git)
            self.assertEqual(local_validate.validate(args), 0)
        report = json.loads((output / "result.json").read_text())
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["tree"], actual_tree)
        self.assertEqual(len(observed), 3)

    def test_output_inside_git_repositories_is_rejected_before_creating_files(self):
        import argparse
        import local_validate
        policy = self.root / "policy"
        subprocess.run(["git", "clone", "--quiet", str(self.repo), str(policy)], check=True)
        unrelated = self.root / "other-worktree"
        subprocess.run(["git", "init", "--quiet", str(unrelated)], check=True)
        trusted = self.root / "trusted-worktree"
        subprocess.run(["git", "init", "--quiet", str(trusted)], check=True)
        (trusted / "scripts").mkdir()
        executable = trusted / "scripts/local_validate.py"
        executable.write_text("# independently selected validator\n")
        alias = self.root / "source-alias"
        alias.symlink_to(self.repo, target_is_directory=True)
        linked = self.root / "linked-worktree"
        self.git("worktree", "add", "--quiet", "--detach", str(linked), self.source)
        broken = self.root / "broken-worktree"
        broken.mkdir()
        (broken / ".git").symlink_to(self.root / "missing-git-directory", target_is_directory=True)
        separate = self.root / "separate-worktree"
        storage = self.root / "git-storage"
        subprocess.run(["git", "init", "--quiet", "--separate-git-dir", str(storage), str(separate)], check=True)
        subprocess.run(["git", "-C", str(separate), "config", "core.worktree", str(separate)], check=True)
        cases = (
            ("source", self.repo / "private-results/run", self.repo),
            ("git-metadata", self.repo / ".git/private-results/run", self.repo),
            ("source-subdirectory", self.repo / "outside-scripts/run", self.repo / "scripts"),
            ("policy", policy / "private-results/run", self.repo),
            ("executing-checkout", trusted / "private-results/run", self.repo),
            ("other-worktree", unrelated / "private-results/run", self.repo),
            ("symlink-alias", alias / "aliased-results/run", self.repo),
            ("linked-worktree", linked / "private-results/run", self.repo),
            ("broken-git-marker", broken / "private-results/run", self.repo),
            ("separate-git-storage", storage / "objects/private-results/run", self.repo),
        )
        before = local_validate.checkout_manifest(self.repo)
        for name, output, repository in cases:
            with self.subTest(location=name):
                args = argparse.Namespace(repository=repository, policy_repository=policy, source=self.source,
                    base=self.base, policy=self.policy, main=self.policy, output=output, kind="configuration", profile="full",
                    pr_title=None, reviewed_head=None, tag=None, tag_created=False, disposable_host=False, timeout=30)
                with patch("local_validate.__file__", str(executable)), \
                     patch("local_validate.checkout") as checkout, patch("local_validate.run") as stage, \
                     self.assertRaisesRegex(ValueError, "--output must be outside Git worktrees"):
                    local_validate.validate(args)
                checkout.assert_not_called()
                stage.assert_not_called()
                self.assertFalse(output.parent.exists())
                self.assertEqual(local_validate.checkout_manifest(self.repo), before)

    def test_external_output_and_bare_origins_remain_supported(self):
        import local_validate
        bare = self.root / "origin.git"
        subprocess.run(["git", "clone", "--quiet", "--bare", str(self.repo), str(bare)], check=True)
        external = self.root / "external-results"
        external.mkdir()
        alias = self.root / "results-alias"
        alias.symlink_to(external, target_is_directory=True)
        output = alias / "new/run"
        with patch("local_validate.__file__", str(external / "local_validate.py")):
            self.assertEqual(local_validate.output_directory(output, self.repo, bare), output.resolve())
        self.assertFalse(output.exists())
        result, _ = self.validate("--repository", str(bare), "--policy-repository", str(bare), "--output", str(output))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((output / "result.json").read_text())["status"], "passed")
        self.assertEqual(output.stat().st_mode & 0o777, 0o700)
        rejected = bare / "private-results/run"
        result, _ = self.validate("--output", str(rejected))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("--output must be outside Git worktrees", result.stderr)
        self.assertFalse(rejected.parent.exists())

    def test_output_discovery_rejects_git_errors_without_creating_files(self):
        import local_validate
        bare = self.root / "unrelated.git"
        subprocess.run(["git", "init", "--quiet", "--bare", str(bare)], check=True)
        installed = self.root / "installed"
        installed.mkdir()
        output = bare / "objects/private-results/run"
        execute = subprocess.run
        def different_owner(command, *args, **kwargs):
            kwargs["env"] = {**kwargs["env"], "GIT_TEST_ASSUME_DIFFERENT_OWNER": "1",
                             "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}
            result = execute(command, *args, **kwargs)
            if command[2] == str(bare / "objects"):
                self.assertIn(b"detected dubious ownership", result.stderr)
            return result
        with patch("local_validate.__file__", str(installed / "local_validate.py")), \
             patch("local_validate.subprocess.run", side_effect=different_owner), \
             self.assertRaisesRegex(ValueError, "cannot determine Git location"):
            local_validate.output_directory(output)
        self.assertFalse(output.parent.exists())

    def test_output_discovery_preserves_path_bytes_and_ignores_git_overrides(self):
        import local_validate
        for suffix in (" ", "\n", "\r\n"):
            with self.subTest(suffix=repr(suffix)):
                bare = self.root / ("unrelated.git" + suffix)
                subprocess.run(["git", "init", "--quiet", "--bare", str(bare)], check=True)
                output = bare / "objects/private-results/run"
                for overrides in ({}, {"GIT_DIR": str(self.repo / ".git"), "GIT_WORK_TREE": str(self.repo)},
                                  {"GIT_CEILING_DIRECTORIES": str(bare), "GIT_DISCOVERY_ACROSS_FILESYSTEM": "0"}):
                    with self.subTest(overrides=tuple(overrides)), patch.dict(os.environ, overrides), \
                         self.assertRaisesRegex(ValueError, "--output must be outside Git worktrees"):
                        local_validate.output_directory(output)
                    self.assertFalse(output.parent.exists())

    def test_checkouts_contain_only_declared_identities_and_their_ancestors(self):
        import argparse
        import local_validate
        (self.repo / "payload").write_text("reviewed tree")
        reviewed = self.commit("fix: prepare reviewed source")
        self.git("checkout", "-b", "integrated", self.base)
        self.git("read-tree", "--reset", "-u", reviewed)
        integrated = self.commit("fix: integrate reviewed source")
        self.git("checkout", "-b", "unrelated", self.base)
        (self.repo / "private-unrelated").write_text("unrelated branch data")
        unrelated = self.commit("fix: unrelated private branch")
        blob = self.git("rev-parse", unrelated + ":private-unrelated")
        self.git("tag", "-a", "v99.0.0", "-m", "unrelated release")
        tag = self.git("rev-parse", "refs/tags/v99.0.0")
        self.git("repack", "-ad")
        calls = []
        def stage(command, cwd, env, log, timeout):
            for target, present, absent in (
                    (cwd, (integrated, reviewed, self.source, self.base, self.policy), (unrelated, blob, tag)),
                    (cwd.parent / "policy", (self.policy, self.base), (integrated, reviewed, self.source, unrelated, blob, tag))):
                for identity in present:
                    subprocess.run(["git", "-C", str(target), "cat-file", "-e", identity], check=True)
                for identity in absent:
                    result = subprocess.run(["git", "-C", str(target), "cat-file", "-e", identity], capture_output=True)
                    self.assertNotEqual(result.returncode, 0, (target, identity))
                refs = local_validate.git(target, "for-each-ref", "--format=%(refname)").splitlines()
                self.assertEqual(refs, ["refs/remotes/origin/main"] if target == cwd else [])
                self.assertNotEqual(subprocess.run(["git", "-C", str(target), "config", "--get", "remote.origin.fetch"],
                                                  capture_output=True).returncode, 0)
            self.assertEqual(local_validate.git(cwd, "rev-parse", "origin/main"), self.policy)
            calls.append(command)
            log.write_text("fixture stage\n")
        args = argparse.Namespace(repository=self.repo, policy_repository=None, source=integrated,
            reviewed_head=reviewed, base=self.base, policy=self.policy, main=self.policy, output=self.root / "bounded-checkouts",
            kind="configuration", profile="full", pr_title=None, tag=None, tag_created=False, disposable_host=False, timeout=30)
        with patch("local_validate.run", side_effect=stage):
            self.assertEqual(local_validate.validate(args), 0)
        self.assertEqual(len(calls), 3)

    def test_shallow_policy_repository_cannot_supply_incomplete_ancestry(self):
        shallow = self.root / "shallow-policy"
        subprocess.run(["git", "clone", "--quiet", "--no-local", "--depth", "1", "--branch", "main",
                        str(self.repo), str(shallow)], check=True)
        result, report = self.validate("--policy-repository", str(shallow))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("complete Git history is required", result.stderr)
        self.assertIsNone(report)

    def test_release_tags_preserve_annotations_frozen_provenance_and_required_bumps(self):
        import argparse
        import local_validate
        self.git("tag", "-a", "v0.1.0", self.base, "-m", "reviewed release")
        selected = self.git("rev-parse", "refs/tags/v0.1.0")
        self.git("tag", "-a", "v0.0.9", self.base, "-m", "older release")
        older = self.git("rev-parse", "refs/tags/v0.0.9")
        self.git("tag", "-a", "v9.0.0-rc.1", self.base, "-m", "prerelease")
        prerelease = self.git("rev-parse", "refs/tags/v9.0.0-rc.1")
        (self.repo / "payload").write_text("release feature")
        head = self.commit("feat: add release behavior")
        self.git("checkout", "-b", "unrelated", self.base)
        (self.repo / "payload").write_text("unrelated release")
        unrelated = self.commit("fix: unrelated release history")
        self.git("tag", "-a", "v99.0.0", "-m", "unrelated release")
        unrelated_tag = self.git("rev-parse", "refs/tags/v99.0.0")
        observed = []
        execute = local_validate.run
        def stage(command, cwd, env, log, timeout):
            self.assertEqual(local_validate.git(cwd, "tag", "--list"), "v0.1.0")
            self.assertEqual(local_validate.git(cwd, "rev-parse", "v0.1.0"), selected)
            self.assertEqual(local_validate.git(cwd, "cat-file", "-t", selected), "tag")
            for target in (cwd, cwd.parent / "policy"):
                for identity in (older, prerelease, unrelated, unrelated_tag):
                    self.assertNotEqual(subprocess.run(["git", "-C", str(target), "cat-file", "-e", identity],
                                                       capture_output=True).returncode, 0, (target, identity))
            self.assertEqual(local_validate.git(cwd.parent / "policy", "tag", "--list"), "")
            self.assertNotEqual(subprocess.run(["git", "-C", str(cwd.parent / "policy"), "cat-file", "-e", selected],
                                              capture_output=True).returncode, 0)
            if not observed:
                self.git("update-ref", "refs/tags/v0.1.0", unrelated)
            observed.append(command)
            execute(command, cwd, env, log, timeout)
        output = self.root / "release-checkouts"
        args = argparse.Namespace(repository=self.repo, policy_repository=None, source=head, reviewed_head=None,
            base=self.base, policy=self.policy, main=head, output=output, kind="configuration", profile="full",
            pr_title=None, tag="v0.2.0", tag_created=True, disposable_host=False, timeout=30)
        with patch("local_validate.run", side_effect=stage):
            self.assertEqual(local_validate.validate(args), 0)
        report = json.loads((output / "result.json").read_text())
        self.assertEqual(report["release_tags"], {"v0.1.0": {"object": selected, "commit": self.base}})
        self.assertEqual(report["history_base"], "v0.1.0")
        self.assertEqual(len(observed), 4)
        self.git("update-ref", "refs/tags/v0.1.0", selected)
        result, report = self.validate("--main", head, "--tag", "v0.1.1", "--tag-created", source=head)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(report["results"][-1]["name"], "tag")
        self.assertIn("required MINOR", (self.root / ("run-" + str(self.count)) / "tag.log").read_text())

    def test_release_checks_include_the_latest_tag_on_declared_main(self):
        self.git("tag", "-a", "v0.1.0", self.base, "-m", "source baseline")
        self.git("checkout", "-b", "declared-main", self.policy)
        (self.repo / "payload").write_text("new declared main")
        main = self.commit("fix: advance declared main")
        self.git("tag", "-a", "v0.2.0", main, "-m", "newer main release")
        self.git("update-ref", "refs/remotes/origin/main", self.base)
        result, report = self.validate("--main", main, "--tag", "v0.1.1-rc.1", "--tag-created")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(set(report["release_tags"]), {"v0.1.0", "v0.2.0"})
        self.assertEqual(report["results"][-1]["name"], "tag")
        self.assertIn("release target must follow latest released commit",
                      (self.root / ("run-" + str(self.count)) / "tag.log").read_text())

    def test_no_prior_base_accepts_root_and_imported_history_without_fallback(self):
        for source in (self.source, self.base):
            with self.subTest(source=source):
                result, report = self.validate("--base", "0" * 40, source=source)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(report["base"], "0" * 40)
                self.assertIs(report["prior_integrated_state"], False)
                self.assertEqual(report["history_base"], "")
                self.assertEqual([entry["name"] for entry in report["results"]], ["commits", "history-secrets", "configuration"])

    def test_no_prior_base_does_not_relax_other_commit_identities(self):
        for name in ("source", "policy", "main", "reviewed-head"):
            with self.subTest(name=name):
                result, report = self.validate("--base", "0" * 40, "--" + name, "0" * 40)
                self.assertNotEqual(result.returncode, 0)
                self.assertIsNone(report)
        for base in ("0" * 39, "missing", "f" * 40):
            with self.subTest(base=base):
                result, report = self.validate("--base", base)
                self.assertNotEqual(result.returncode, 0)
                self.assertIsNone(report)

    def test_no_prior_base_rejects_invalid_imported_commit_before_tip(self):
        (self.repo / "payload").write_text("invalid historical commit")
        self.commit("invalid historical subject")
        (self.repo / "payload").write_text("valid tip")
        head = self.commit("fix: finish imported history")
        result, report = self.validate("--base", "0" * 40, source=head)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(report["results"][-1]["name"], "commits")

    def test_no_prior_base_scans_removed_secrets_in_both_imported_histories(self):
        (self.repo / ".env").write_text("EXAMPLE=placeholder\n")
        self.commit("fix: add forbidden filename")
        (self.repo / ".env").unlink()
        self.commit("fix: remove forbidden filename")
        self.git("tag", "v0.1.0")
        (self.repo / "payload").write_text("clean tip")
        reviewed = self.commit("fix: finish imported history")
        self.git("checkout", "--orphan", "integrated")
        integrated = self.commit("fix: integrate reviewed source tree")
        for source, extra in ((reviewed, ()), (integrated, ("--reviewed-head", reviewed)),
                              (reviewed, ("--tag", "v0.1.1-rc.1", "--tag-created"))):
            with self.subTest(source=source, extra=extra):
                result, report = self.validate("--base", "0" * 40, *extra, source=source)
                self.assertNotEqual(result.returncode, 0, result.stderr)
                self.assertEqual(report["history_base"], "")
                self.assertEqual(report["results"][-1]["name"], "history-secrets")

    def test_embedded_workflow_passes_pr_title_base_and_main_and_preserves_initial_push(self):
        import yaml
        workflow = yaml.safe_load((ROOT / "templates/config-repository/.github/workflows/validate.yml").read_text())
        step = next(step for step in workflow["jobs"]["hosted-validation"]["steps"]
                    if step["name"] == "Run complete local configuration validation")
        self.assertEqual(step["env"]["BASE"], "${{ github.event.pull_request.base.sha || github.event.before }}")
        self.assertEqual(step["env"]["MAIN"], "${{ github.event.pull_request.base.sha || github.sha }}")
        self.assertEqual(step["env"]["PR_TITLE"], "${{ github.event.pull_request.title }}")
        self.assertNotIn("${{", step["run"])
        validator = self.repo / ".ci-fleet-validator"
        subprocess.run(["git", "clone", "--quiet", "--no-local", str(self.repo), str(validator)], check=True)
        subprocess.run(["git", "-C", str(validator), "checkout", "--quiet", self.policy], check=True)
        for name in ("local_validate.py", "local_validation_supervisor.py"):
            shutil.copyfile(ROOT / "scripts" / name, validator / "scripts" / name)
        for event, title, base, main, accepted in (
                ("pull_request", "invalid $(touch title-injected)", self.base, self.base, False),
                ("pull_request", 'fix: keep "$(touch title-injected)"', self.base, self.base, True),
                ("push", "invalid unused title", "0" * 40, self.source, True)):
            with self.subTest(event=event, title=title):
                runtime = self.root / ("workflow-" + str(self.count)); self.count += 1
                runtime.mkdir()
                env = {**os.environ, "RUNNER_TEMP": str(runtime), "SOURCE": self.source, "BASE": base, "MAIN": main,
                       "PR_TITLE": title, "EVENT_NAME": event, "VALIDATOR": self.policy}
                result = subprocess.run(["bash", "-e", "-c", step["run"]], cwd=self.repo, env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode == 0, accepted, result.stdout + result.stderr)
                report = json.loads((runtime / "local-validation/result.json").read_text())
                self.assertEqual((report["base"], report["main"]), (base, main))
                self.assertEqual(report["pr_title"], title if event == "pull_request" else None)
                if not accepted:
                    self.assertEqual(report["results"][-1]["name"], "pr-title")
                self.assertFalse((self.repo / "title-injected").exists())

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
        for base in (self.base, "0" * 40):
            with self.subTest(base=base):
                calls.clear()
                output = self.root / ("fresh-stages-" + base)
                args = argparse.Namespace(repository=self.repo, policy_repository=None, source=self.source,
                    base=base, policy=self.policy, main=self.policy, output=output, kind="engine", profile="quick",
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
        previous_umask = os.umask(0o077)
        try:
            result, report = self.validate()
        finally:
            os.umask(previous_umask)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report["status"], "passed")

    def test_reconcile_fixtures_preserve_restrictive_checkout_permissions(self):
        for name in ("test_remote_reconcile.py", "github-app-token.sh", "remote-reconcile.sh"):
            target = self.repo / "scripts" / name
            target.write_bytes((ROOT / "scripts" / name).read_bytes())
            if name.endswith(".sh"):
                target.chmod(0o700)
        self.source = self.commit("test: include actual reconciliation fixtures")
        self.git("checkout", "main")
        (self.repo / "scripts/validate-configuration.sh").write_text(
            "#!/bin/bash\nset -eu\npython3 scripts/test_remote_reconcile.py "
            "TestGitHubAppToken.test_requires_args TestRemoteReconcile.test_help_exits_0\n")
        self.policy = self.commit("test: run reconciliation permission fixtures")
        previous_umask = os.umask(0o077)
        try:
            result, report = self.validate()
        finally:
            os.umask(previous_umask)
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

    def test_signing_authority_markers_reject_both_kinds_and_profiles_before_execution(self):
        import argparse
        import local_validate
        exists, is_symlink = Path.exists, Path.is_symlink
        markers = ("/etc/ci-fleet-signing", "/var/lib/ci-fleet-signing",
                   "/opt/ci-fleet-signing", "/srv/ci-fleet-signing")
        for kind in ("engine", "configuration"):
            for profile in ("quick", "full"):
                for marker in markers:
                    for dangling in (False, True):
                        with self.subTest(kind=kind, profile=profile, marker=marker, dangling=dangling):
                            output = self.root / (kind + profile + marker.replace("/", "-") + str(dangling))
                            args = argparse.Namespace(repository=self.repo, policy_repository=None, source=self.source,
                                base=self.base, policy=self.policy, main=self.policy, output=output, kind=kind, profile=profile,
                                pr_title=None, reviewed_head=None, tag=None, tag_created=False, disposable_host=True, timeout=30)
                            with patch("local_validate.Path.exists", autospec=True,
                                       side_effect=lambda p: not dangling if str(p) == marker else exists(p)), \
                                 patch("local_validate.Path.is_symlink", autospec=True,
                                       side_effect=lambda p: dangling if str(p) == marker else is_symlink(p)), \
                                 patch("local_validate.run") as stage, \
                                 patch("local_validate.supervise_stage") as supervisor, \
                                 patch("sys.stderr", io.StringIO()):
                                self.assertEqual(local_validate.validate(args), 1)
                            stage.assert_not_called()
                            supervisor.assert_not_called()
                            report = json.loads((output / "result.json").read_text())
                            self.assertIn("signing authority", report["error"])
                            self.assertEqual(report["results"], [])
                            self.assertEqual(report["tools"], {})

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

    @unittest.skipUnless(os.geteuid() == 0, "engine fixtures require root")
    def test_engine_requires_jq_before_running_stages(self):
        import argparse
        import local_validate
        original = subprocess.check_output
        def missing_jq(command, *args, **kwargs):
            if command[:1] == ["jq"]:
                raise FileNotFoundError("jq missing")
            return original(command, *args, **kwargs)
        for profile in ("quick", "full"):
            with self.subTest(profile=profile):
                output = self.root / ("missing-jq-" + profile)
                args = argparse.Namespace(repository=self.repo, policy_repository=None, source=self.source,
                    base=self.base, policy=self.policy, main=self.policy, output=output, kind="engine", profile=profile,
                    pr_title=None, reviewed_head=None, tag=None, tag_created=False, disposable_host=True, timeout=30)
                with patch("local_validate.subprocess.check_output", side_effect=missing_jq), \
                     patch("local_validate.run") as stage, patch("sys.stderr", io.StringIO()):
                    self.assertEqual(local_validate.validate(args), 1)
                stage.assert_not_called()
                report = json.loads((output / "result.json").read_text())
                self.assertEqual(report["status"], "failed")
                self.assertEqual(report["error"], "jq missing")
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
