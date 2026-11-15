#!/usr/bin/env python3
"""Nonprivileged diagnostics regressions; never execute the fleet fixtures."""
import ast
import io
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from validation_diagnostics import TimedTestRunner

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / 'scripts/validation_diagnostics.sh'


class ShellDiagnosticsTests(unittest.TestCase):
    def shell(self, body):
        return subprocess.run(['bash', '-c', 'set -Eeuo pipefail\nsource "$1"\n' + body,
                               'diagnostic-test', str(HELPER)], text=True, capture_output=True)

    def test_success_streams_and_utc_timestamp(self):
        result = self.shell("validation_command fixed-id bash -c 'printf payload; printf child-error >&2' private-argument")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, 'payload')
        self.assertRegex(result.stderr, r'VALIDATION \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ BEGIN fixed-id')
        self.assertIn('END fixed-id exit=0', result.stderr)
        self.assertIn('child-error', result.stderr)
        self.assertNotIn('private-argument', result.stderr)

    def test_nonzero_status_and_parent_errexit_preserved(self):
        result = self.shell("validation_command failed bash -c 'exit 37'\nprintf should-not-run")
        self.assertEqual(result.returncode, 37)
        self.assertEqual(result.stdout, '')
        self.assertIn('END failed exit=37', result.stderr)

    def test_external_script_errexit_and_pipefail_preserved(self):
        result = self.shell("validation_command failed bash -ec 'false; printf should-not-run'")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, '')
        result = self.shell("validation_command failed bash -c 'exit 23' | validation_command sink tr a b\nprintf should-not-run")
        self.assertEqual(result.returncode, 23)
        self.assertEqual(result.stdout, '')
        self.assertIn('END failed exit=23', result.stderr)
        self.assertIn('END sink exit=0', result.stderr)

    def test_conditional_failure_and_signal_status(self):
        result = self.shell("if validation_command expected bash -c 'exit 7'; then exit 99; fi\nvalidation_command signalled bash -c 'kill -TERM $$'")
        self.assertEqual(result.returncode, 143)
        self.assertIn('END expected exit=7', result.stderr)
        self.assertIn('END signalled exit=143', result.stderr)

    def test_case_survives_child_redirection(self):
        result = self.shell("validation_case tester.fixed\nbash -c 'printf secret; printf secret >&2' >/dev/null 2>&1")
        self.assertEqual(result.returncode, 0)
        self.assertIn('CASE tester.fixed', result.stderr)
        self.assertNotIn('secret', result.stderr)

    def test_whole_process_group_termination_does_not_invent_end(self):
        # Synchronize on BEGIN rather than sleeping or running a privileged fixture.
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / 'log'
            with log.open('w+') as output:
                process = subprocess.Popen(['bash', '-c',
                    'source "$1"; validation_command interrupted sleep 60', 'test', str(HELPER)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, start_new_session=True)
                try:
                    begin = process.stderr.readline()
                    self.assertIn('BEGIN interrupted', begin)
                    os.killpg(process.pid, signal.SIGTERM)
                    _, rest = process.communicate(timeout=5)
                    output.write(begin + rest)
                    self.assertNotEqual(process.returncode, 0)
                    self.assertNotIn('END interrupted', rest)
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.communicate()

    def test_quick_plan_records_commands_and_stops_at_failure_without_running_fixtures(self):
        plan = (ROOT / 'scripts/validate.sh').read_text()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'scripts').mkdir()
            (root / 'examples/project/scripts/ci').mkdir(parents=True)
            bin_dir = root / 'bin'; bin_dir.mkdir()
            fake = '#!/bin/bash\n[[ ${1:-} != "${FAIL_TARGET:-never-match}" ]] || exit 37\n'
            for name in ('python3', 'bash'):
                p = bin_dir / name; p.write_text(fake); p.chmod(0o755)
            for name in re.findall(r'\bscripts/[\w-]+\.sh', plan.split('\nfi\n')[0]):
                p = root / name
                p.write_text('#!/bin/bash\nexit 0\n'); p.chmod(0o755)
            env = dict(os.environ, CI_FLEET_VALIDATION_ROOT=str(root), PATH=str(bin_dir) + ':/usr/bin:/bin')
            for failure in ('never-match', str(ROOT / 'scripts/validation_tasks.py')):
                result = subprocess.run(['/bin/bash', str(ROOT / 'scripts/validate.sh'), '--quick'],
                                        env=dict(env, FAIL_TARGET=failure), text=True, capture_output=True)
                self.assertEqual(result.returncode, 0 if failure == 'never-match' else 37, result.stderr)
                if failure == 'never-match':
                    for command in ('python-compile', 'regressions'):
                        self.assertIn('BEGIN ' + command + '\n', result.stderr)
                        self.assertIn('END ' + command + ' exit=0', result.stderr)
                else:
                    self.assertIn('END regressions exit=37', result.stderr)
                    self.assertNotIn('BEGIN runner-build', result.stderr)


class PythonDiagnosticsTests(unittest.TestCase):
    def test_results_skips_failfast_and_ids_preserved(self):
        class Cases(unittest.TestCase):
            def test_a_pass(self): pass
            def test_b_failure(self): self.fail('expected failure')
            def test_c_error(self): raise ValueError('expected error')
            @unittest.skip('fixture skip')
            def test_d_skip(self): pass
            @unittest.expectedFailure
            def test_e_expected(self): self.fail('expected')
            def test_f_subtest(self):
                with self.subTest(private='do-not-log-parameter'):
                    self.assertTrue(True)
        stream = io.StringIO()
        result = TimedTestRunner(stream=stream).run(unittest.defaultTestLoader.loadTestsFromTestCase(Cases))
        self.assertEqual((result.testsRun, len(result.failures), len(result.errors), len(result.skipped), len(result.expectedFailures)), (6, 1, 1, 1, 1))
        records = [json.loads(line.removeprefix('VALIDATION_TEST ')) for line in stream.getvalue().splitlines() if line.startswith('VALIDATION_TEST ')]
        self.assertEqual(len(records), 12)
        for begin, end in zip(records[::2], records[1::2]):
            self.assertEqual((begin['event'], end['event']), ('begin', 'end'))
            self.assertEqual(begin['test'], end['test'])
            self.assertGreaterEqual(end['elapsed_seconds'], 0)
            self.assertTrue(begin['timestamp'].endswith('+00:00'))
            self.assertNotIn('do-not-log-parameter', json.dumps(begin))
        result = TimedTestRunner(stream=io.StringIO(), failfast=True).run(unittest.defaultTestLoader.loadTestsFromTestCase(Cases))
        self.assertEqual(result.testsRun, 2)

    def test_elapsed_uses_monotonic_clock(self):
        class Case(unittest.TestCase):
            def test_ok(self): pass
        stream = io.StringIO()
        with patch('validation_diagnostics.time.monotonic', side_effect=[10, 12.5]):
            TimedTestRunner(stream=stream).run(unittest.defaultTestLoader.loadTestsFromTestCase(Case))
        self.assertIn('"elapsed_seconds": 2.5', stream.getvalue())

    def test_cli_exit_codes_unchanged(self):
        for body, expected in (('pass', 0), ('self.fail("expected")', 1)):
            code = ('import sys, unittest\nsys.path.insert(0, ' + repr(str(ROOT / 'scripts')) + ')\n'
                    'from validation_diagnostics import TimedTestRunner\n'
                    'class Case(unittest.TestCase):\n    def test_case(self): ' + body + '\n'
                    'unittest.main(testRunner=TimedTestRunner)\n')
            result = subprocess.run([sys.executable, '-I', '-c', code], capture_output=True, text=True)
            self.assertEqual(result.returncode, expected, result.stderr)
            self.assertIn('VALIDATION_TEST ', result.stderr)

    def test_instrumented_entrypoints_load_without_executing_tests(self):
        for name in ('test_local_validation.py', 'test_apply_docker_network_policy.py', 'test_remote_reconcile.py', 'test_explicit_recovery.py'):
            flags = ['-I', '-B'] if name == 'test_explicit_recovery.py' else ['-B']
            result = subprocess.run([sys.executable, *flags, str(ROOT / 'scripts' / name), '--help'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('usage:', result.stdout)

    def test_copied_reconcile_entrypoint_has_timing_dependency(self):
        # Consume the real copy list without invoking privileged PolicyExecutionTests
        # setup or its systemd executor. --help imports the entrypoint, not fixtures.
        tree = ast.parse((ROOT / 'scripts/test_local_validation.py').read_text())
        method = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                      and node.name == 'test_reconcile_fixtures_preserve_restrictive_checkout_permissions')
        names = ast.literal_eval(method.body[0].iter)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in names:
                target = root / name
                target.write_bytes((ROOT / 'scripts' / name).read_bytes())
                target.chmod(0o700)
            result = subprocess.run([sys.executable, '-E', '-s', '-B', str(root / 'test_remote_reconcile.py'), '--help'],
                                    cwd=root, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('usage:', result.stdout)

    def test_tester_case_labels_are_fixed_unique_and_cover_waits(self):
        source = (ROOT / 'scripts/test-install-tester.sh').read_text()
        labels = re.findall(r'^validation_case (\S+)$', source, re.M)
        self.assertEqual(len(labels), len(set(labels)))
        self.assertGreater(len(labels), 50)
        self.assertTrue(all(re.fullmatch(r'tester\.[a-z-]+', label) for label in labels))
        for wait in ('probe-readiness-wait', 'probe-completion-wait', 'lock-readiness-wait', 'lock-completion-wait'):
            self.assertIn('tester.' + wait, labels)
        self.assertNotIn('set -x', source)


if __name__ == '__main__':
    unittest.main()
