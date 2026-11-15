#!/usr/bin/env python3
"""Exercise concurrency barriers, original outcomes and protected exact-source reuse."""
import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import validation_tasks as tasks


class RegistryWiringTests(unittest.TestCase):
    REQUIRED = [
        ('network-policy', ['python3', 'scripts/test_apply_docker_network_policy.py'], False),
        ('numeric-regressions', ['scripts/test-numeric-regressions.sh'], False),
        ('deployer-units', ['scripts/test-deployer-units.sh'], True),
    ]

    def test_required_tasks_and_dispatch_are_present(self):
        for expected in self.REQUIRED:
            with self.subTest(task=expected[0]):
                tasks.require_registered_task(expected)

    def test_missing_or_wrong_task_command_is_rejected(self):
        for expected in self.REQUIRED:
            missing = [task for task in tasks.TASKS if task[0] != expected[0]]
            wrong = missing + [(expected[0], ['wrong-command'], expected[2])]
            for case, registry in (('missing', missing), ('wrong', wrong)):
                with self.subTest(task=expected[0], case=case):
                    with patch.object(tasks, 'TASKS', registry), self.assertRaisesRegex(ValueError, 'exact required task'):
                        tasks.require_registered_task(expected)

    def test_missing_or_comment_only_dispatch_is_rejected(self):
        root = Path(__file__).resolve().parents[1]
        validate = (root / 'scripts/validate.sh').read_text()
        dispatch = next(line for line in validate.splitlines() if line.startswith('validation_command regressions '))
        for replacement in ('', '# ' + dispatch):
            with patch.object(Path, 'read_text', return_value=validate.replace(dispatch, replacement)):
                for expected in self.REQUIRED:
                    with self.subTest(task=expected[0], replacement=replacement):
                        with self.assertRaisesRegex(ValueError, 'does not dispatch'):
                            tasks.require_registered_task(expected)

    def test_actual_guards_pass_before_long_fixtures(self):
        root = Path(__file__).resolve().parents[1]
        network = subprocess.run([sys.executable, '-B', str(root / 'scripts/test_apply_docker_network_policy.py'), 'ValidationTests'],
                                 cwd=root, capture_output=True, text=True, timeout=10)
        self.assertEqual(network.returncode, 0, network.stderr)
        header = 'if ! python3 -I -B - "$repo_root" <<\'PY_VALIDATION_TASK\''
        for name in ('test-numeric-regressions.sh', 'test-install-deployer.sh'):
            with self.subTest(script=name):
                source = (root / 'scripts' / name).read_text()
                self.assertIn(header, source.splitlines())
                guard = source.split(header + '\n', 1)[1].split('\nPY_VALIDATION_TASK\n', 1)[0]
                result = subprocess.run([sys.executable, '-I', '-B', '-', str(root)],
                                        input=guard, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)


class TaskTests(unittest.TestCase):
    def setUp(self):
        # Resume receipts deliberately require a protected path, not shared /tmp.
        self.tmp = tempfile.TemporaryDirectory(dir=Path.home())
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def task(self, name, body, exclusive=False):
        return name, [sys.executable, '-I', '-c', 'from pathlib import Path\n' + body], exclusive

    def execute(self, selected, name='output', workers=1, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return tasks.execute(selected, self.root, self.root / name,
                                 budget=lambda: {'workers': workers}, **kwargs)

    def test_independent_groups_overlap_but_exclusive_group_is_a_barrier(self):
        handshake = '''
import time
own, peer = {own!r}, {peer!r}
Path(own).touch()
end = time.monotonic() + 5
while not Path(peer).exists():
    if time.monotonic() > end: raise RuntimeError('groups did not overlap')
    time.sleep(.01)
Path(own + '.done').touch()
'''
        selected = [self.task('first', handshake.format(own='first', peer='second')),
                    self.task('second', handshake.format(own='second', peer='first')),
                    self.task('exclusive', "assert Path('first.done').exists() and Path('second.done').exists()\nPath('exclusive.done').touch()", True),
                    self.task('last', "assert Path('exclusive.done').exists()")]
        self.assertEqual(self.execute(selected, workers=2), 0)
        result = json.loads((self.root / 'output/result.json').read_text())
        self.assertEqual(result['status'], 'passed')
        self.assertEqual({r['task'] for r in result['results']}, {t[0] for t in selected})

    def test_failure_retains_original_status_and_does_not_admit_later_work(self):
        selected = [self.task('failed', "raise SystemExit(37)"),
                    self.task('not-run', "Path('should-not-exist').touch()")]
        self.assertEqual(self.execute(selected), 1)
        result = json.loads((self.root / 'output/result.json').read_text())
        self.assertEqual(result['results'][0]['exit_code'], 37)
        self.assertEqual(result['not_run'], ['not-run'])
        self.assertFalse((self.root / 'should-not-exist').exists())

    def test_resume_reuses_only_passed_groups_in_the_same_source_context(self):
        body = "p=Path({name!r}); p.write_text(str(int(p.read_text())+1) if p.exists() else '1')\n"
        selected = [self.task('first', body.format(name='first.count')),
                    self.task('transient', body.format(name='transient.count') + "raise SystemExit(7 if Path('blocked').exists() else 0)")]
        (self.root / 'blocked').touch()
        self.assertEqual(self.execute(selected, 'attempt1', context='candidate-a'), 1)
        (self.root / 'blocked').unlink()
        self.assertEqual(self.execute(selected, 'attempt2', context='candidate-a', resume=self.root / 'attempt1'), 0)
        self.assertEqual((self.root / 'first.count').read_text(), '1')
        self.assertEqual((self.root / 'transient.count').read_text(), '2')
        self.assertEqual(self.execute(selected, 'attempt3', context='candidate-b', resume=self.root / 'attempt1'), 0)
        self.assertEqual((self.root / 'first.count').read_text(), '2')

    def test_tampered_or_writable_resume_evidence_cannot_pass(self):
        selected = [self.task('first', "print('original')")]
        self.assertEqual(self.execute(selected, 'original', context='candidate'), 0)
        log = self.root / 'original/first.log'
        log.write_text('substituted\n')
        with self.assertRaisesRegex(ValueError, 'resume log'):
            self.execute(selected, 'tampered', context='candidate', resume=self.root / 'original')
        log.write_text('original\n')
        receipt = self.root / 'original/first.json'
        receipt.chmod(0o666)
        with self.assertRaisesRegex(ValueError, 'unsafe'):
            self.execute(selected, 'writable', context='candidate', resume=self.root / 'original')

    def test_unexpected_skip_does_not_pass_even_with_zero_process_exit(self):
        self.assertEqual(self.execute([self.task('skipped', "print('OK (skipped=1)')")]), 1)

    def test_live_progress_filters_payload_but_preserves_complete_log_and_digest(self):
        task = self.task('progress', "print('payload-that-stays-in-artifact')\nprint('VALIDATION fixed-case')")
        output = self.root / 'progress'; output.mkdir()
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            result = tasks.run_task(task, self.root, output)
        self.assertIn('VALIDATION fixed-case', captured.getvalue())
        self.assertNotIn('payload-that-stays-in-artifact', captured.getvalue())
        log = (output / 'progress.log').read_bytes()
        self.assertIn(b'payload-that-stays-in-artifact', log)
        self.assertEqual(result['sha256'], hashlib.sha256(log).hexdigest())

    def test_duplicate_task_names_are_rejected_before_execution(self):
        task = self.task('same', 'pass')
        with self.assertRaises(ValueError):
            self.execute([task, task])

    def test_progress_does_not_recognize_the_middle_of_an_oversized_line(self):
        from local_validation_executor import Progress
        progress = Progress()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            progress.feed(b'x' * 9000)
            progress.feed(b'VALIDATION forged-middle\nVALIDATION valid-record\n')
            progress.feed(b'VALIDATION escape\x1b[31m\n')
        self.assertEqual(output.getvalue(), 'VALIDATION valid-record\n')

    def test_capacity_stop_coverage_is_owned_by_controller_group(self):
        root = Path(__file__).resolve().parents[1]
        self.assertIn('test-capacity-unit-stop.sh', (root / 'scripts/test-install-worker-controller.sh').read_text())
        self.assertNotIn('capacity-stop', {task[0] for task in tasks.TASKS})

    def test_worker_limit_is_detected_for_each_execution(self):
        selected = [self.task('one', 'pass')]
        with patch('validation_tasks.run_task', wraps=tasks.run_task):
            self.assertEqual(self.execute(selected, 'small', workers=1), 0)
            self.assertEqual(self.execute(selected, 'large', workers=8), 0)
        self.assertEqual(json.loads((self.root / 'small/result.json').read_text())['resource_budget']['workers'], 1)
        self.assertEqual(json.loads((self.root / 'large/result.json').read_text())['resource_budget']['workers'], 8)

    def test_source_policy_tools_and_boot_changes_invalidate_reuse(self):
        from local_validate import regression_context
        report = dict.fromkeys(('suite', 'profile', 'source', 'reviewed_head', 'tree', 'base', 'policy', 'main',
                                'executor_revision', 'pr_title', 'tag', 'tag_created', 'tools', 'release_tags'), 'fixture')
        original = regression_context(report)
        for field in report:
            changed = copy.deepcopy(report); changed[field] = 'changed'
            self.assertNotEqual(regression_context(changed), original, field)
        with patch('local_validate.Path.read_text', return_value='different-boot'):
            self.assertNotEqual(regression_context(report), original)


if __name__ == '__main__':
    unittest.main()
