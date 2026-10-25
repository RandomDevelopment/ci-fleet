#!/usr/bin/env python3
"""Optional APT cache validation, rendering, and endpoint redaction tests."""

import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

from local_validate import run

from apt_cache import apt_config
from desired_state import DesiredStateError, build_rendered_env, load_engine_capabilities, validate_host_values
from test_desired_state import config, host_values, CONFIG_COMMIT, ROOT
from test_apt_cache_e2e import fixture_failure_details


class APTCacheTests(unittest.TestCase):
    def test_timeout_unwinds_owned_fixture_resources_through_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            docker = root / "docker"
            docker.write_text('''#!/usr/bin/python3
import json, os, sys, time
from pathlib import Path
args = sys.argv[1:]
with Path(os.environ["APT_COMMANDS"]).open("a") as log:
    log.write(json.dumps(args) + "\\n")
if args[:2] == ["inspect", "--format"]:
    time.sleep(60)
if args[:2] == ["container", "inspect"]:
    sys.exit(1)
''')
            docker.chmod(0o755)
            commands = root / "commands.jsonl"
            env = os.environ | {"PATH": str(root) + ":" + os.environ["PATH"], "APT_COMMANDS": str(commands)}
            command = ["bash", "-c", 'python3 "$1"; exit "$?"', "bash", str(ROOT / "scripts/test_apt_cache_e2e.py")]
            with self.assertRaises(subprocess.TimeoutExpired):
                run(command, ROOT, env, root / "fixture.log", 2)
            recorded = [json.loads(line) for line in commands.read_text().splitlines()]
            created = next(args[args.index("--name") + 1] for args in recorded if args[:2] == ["run", "-d"])
            self.assertIn(["rm", "-f", "-v", created], recorded)
            self.assertTrue(any(args[:2] == ["image", "rm"] for args in recorded))

    def test_image_timestamp_normalization_preserves_optional_secret(self):
        epoch = 1786752000
        for dockerfile, count in (('runner/Dockerfile', 3), ('controller/Dockerfile', 1)):
            commands = [line.split('&& ', 1)[1] for line in (ROOT / dockerfile).read_text().splitlines()
                        if '&& find /etc ' in line]
            self.assertEqual(len(commands), count)
            for command in commands:
                for enabled in (False, True):
                    with self.subTest(dockerfile=dockerfile, command=command, enabled=enabled), tempfile.TemporaryDirectory() as directory:
                        root = Path(directory)
                        for name in ('etc/apt/apt.conf.d', 'home', 'run', 'usr', 'var'):
                            (root / name).mkdir(parents=True)
                        ordinary = root / 'etc/apt/apt.conf.d/ordinary'
                        ordinary.write_text('normalize this file')
                        secret = root / 'etc/apt/apt.conf.d/99-ci-fleet-cache'
                        if enabled:
                            secret.write_text('temporary BuildKit secret')
                            secret.chmod(0o400)
                            original = secret.stat().st_mtime_ns
                        arguments = shlex.split(command.replace('${SOURCE_DATE_EPOCH}', str(epoch)))
                        arguments = [str(root / value.lstrip('/')) if value.startswith('/') else value for value in arguments]
                        subprocess.run(arguments, check=True, capture_output=True)
                        self.assertEqual(ordinary.stat().st_mtime, epoch)
                        if enabled:
                            self.assertEqual(secret.stat().st_mtime_ns, original)
                        else:
                            self.assertFalse(secret.exists())

    def test_fixture_build_trace_preserves_command_status_and_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mock = root / 'docker'
            mock.write_text('#!/bin/sh\nprintf "%s\\n" "$*"\nprintf "http://192.0.2.1:3142 failed\\n" >&2\nexit "$TRACE_EXIT"\n')
            mock.chmod(0o755)
            trace = root / 'build.log'
            environment = os.environ | {
                'PATH': str(root) + ':' + os.environ['PATH'],
                'BASH_ENV': str(ROOT / 'scripts/fixtures/apt-cache/trace-builds.sh'),
                'CI_FLEET_BUILD_TRACE': str(trace),
            }
            for command, status in (('buildx build --no-cache', 0), ('buildx build --no-cache', 47), ('version', 0)):
                trace.unlink(missing_ok=True)
                result = subprocess.run(['bash', '-c', 'docker ' + command],
                                        env=environment | {'TRACE_EXIT': str(status)}, capture_output=True)
                self.assertEqual(result.returncode, status)
                self.assertIn(command.encode(), result.stdout)
                self.assertIn(b'failed', result.stdout + result.stderr)
                self.assertEqual(trace.exists(), command.startswith('buildx build'))
                if trace.exists():
                    self.assertEqual(trace.stat().st_mode & 0o777, 0o600)
                    self.assertIn('failed', fixture_failure_details(trace.read_bytes(), max_lines=40))
                    self.assertNotIn('192.0.2.1', fixture_failure_details(trace.read_bytes(), max_lines=40))

    def test_fixture_failure_details_omit_endpoints_and_bound_output(self):
        details = fixture_failure_details(
            b'Ignored earlier output\n' * 500 +
            b'Could not connect to 192.0.2.1:3142 or [2001:db8::1]:3142\n'
            b'Failed http://user:password@cache.example.invalid/path\nAPT exited 100\n')
        for value in ('192.0.2.1', '2001:db8::1', 'password', 'cache.example.invalid'):
            self.assertNotIn(value, details)
        self.assertIn('APT exited 100', details)
        self.assertLessEqual(len(details.splitlines()), 10)
        details = fixture_failure_details(
            b'#18 touch: read-only file system at http://192.0.2.1:3142\n'
            b'Dockerfile:24\n' + b'Long repeated Dockerfile instruction\n' * 200,
            max_lines=40)
        self.assertIn('read-only file system', details)
        self.assertNotIn('192.0.2.1', details)
        self.assertNotIn('Long repeated', details)

    def test_shared_validation_cases(self):
        cases = json.loads((ROOT / "scripts/fixtures/apt-cache.json").read_text())
        for proxy, bypass in cases["valid"]:
            self.assertEqual(bool(apt_config(proxy, bypass)), bool(proxy))
        for proxy, bypass in cases["invalid"]:
            with self.subTest(proxy=proxy, bypass=bypass), self.assertRaises(ValueError):
                apt_config(proxy, bypass)
        with self.assertRaises(ValueError):
            apt_config("http://cache.example.invalid", ",".join(["example.invalid"] * 65))

    def test_host_local_capability_removal_and_metadata_redaction(self):
        host = host_values()
        proxy = "http://cache.example.invalid:3142"
        host["CI_FLEET_APT_HTTP_PROXY"] = proxy
        host["CI_FLEET_NO_PROXY"] = "mirror.example.invalid"
        capabilities = load_engine_capabilities(ROOT / "engine-capabilities.json")
        def render():
            return build_rendered_env(config(), "example-ci-01", host, config_repository="example/config",
                                      config_ref=CONFIG_COMMIT, docker_gid=999, engine_capabilities=capabilities)
        environment, metadata = render()
        self.assertEqual(environment["CI_FLEET_APT_HTTP_PROXY"], proxy)
        self.assertNotIn(proxy, json.dumps(metadata))
        self.assertNotIn("mirror.example.invalid", json.dumps(metadata))
        capabilities.remove("apt_cache")
        with self.assertRaisesRegex(DesiredStateError, "optional APT cache"):
            render()
        del host["CI_FLEET_APT_HTTP_PROXY"]
        with self.assertRaisesRegex(DesiredStateError, "requires"):
            validate_host_values(host)
        del host["CI_FLEET_NO_PROXY"]
        environment, _ = render()
        self.assertNotIn("CI_FLEET_APT_HTTP_PROXY", environment)
        self.assertNotIn("CI_FLEET_NO_PROXY", environment)

    def test_cli_private_file_and_safe_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "apt.conf"
            proxy = "http://cache.example.invalid:3142"
            command = ["python3", str(ROOT / "scripts/apt_cache.py"), "--output", str(path)]
            environment = os.environ | {"CI_FLEET_APT_HTTP_PROXY": proxy, "CI_FLEET_NO_PROXY": ""}
            result = subprocess.run(command, env=environment, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn(proxy, result.stdout + result.stderr)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertIn('Acquire::https::Proxy "DIRECT";', path.read_text())
            self.assertIn('APT::Update::Error-Mode "any";', path.read_text())
            path.unlink()
            environment["CI_FLEET_APT_HTTP_PROXY"] = "http://user:password@cache.example.invalid"
            result = subprocess.run(command, env=environment, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("password", result.stdout + result.stderr)
            self.assertNotIn("cache.example.invalid", result.stdout + result.stderr)
            self.assertFalse(path.exists())

    def test_installer_detects_runtime_cache_drift(self):
        source = (ROOT / "scripts/install-worker-controller.sh").read_text()
        function = "controller_environment_matches() {" + source.split("controller_environment_matches() {", 1)[1].split("\n}\n", 1)[0] + "\n}\n"
        capabilities = load_engine_capabilities(ROOT / "engine-capabilities.json")
        environment, _ = build_rendered_env(config(), "example-ci-01", host_values(), config_repository="example/config",
                                            config_ref=CONFIG_COMMIT, docker_gid=999, engine_capabilities=capabilities)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "deploy").mkdir()
            (root / "deploy/compose.yaml").write_text((ROOT / "deploy/compose.yaml").read_text())
            script = root / "check.sh"
            script.write_text('set -euo pipefail\n' + function + '''
current_runtime_release() { printf '%s\\n' "$APT_TEST_ROOT"; }
portable_managed_images() { [[ "$APT_TEST_PORTABLE" == true ]]; }
docker() { cat "$APT_TEST_ROOT/live.env"; }
controller_container=fixture
candidate_env=$APT_TEST_ROOT/expected.env
controller_environment_matches
''')
            for portable in (False, True):
                for configured, stale, duplicate in ((False, False, False), (False, True, False),
                                                      (True, False, False), (True, True, False), (False, False, True)):
                    compose = (ROOT / "deploy/compose.yaml").read_text()
                    if not portable:
                        compose = compose.replace('CI_FLEET_RUNNER_IMAGE: ${CI_FLEET_RUNNER_RUNTIME_IMAGE_ID:',
                                                  'CI_FLEET_RUNNER_IMAGE: ${CI_FLEET_RUNNER_IMAGE_DIGEST:')
                    (root / "deploy/compose.yaml").write_text(compose)
                    expected = dict(environment)
                    if portable:
                        expected['CI_FLEET_RUNNER_RUNTIME_IMAGE_ID'] = 'sha256:' + '7' * 64
                        expected['CI_FLEET_CONTROLLER_RUNTIME_IMAGE_ID'] = 'sha256:' + '8' * 64
                    if configured:
                        expected['CI_FLEET_APT_HTTP_PROXY'] = 'http://cache.example.invalid:3142'
                        expected['CI_FLEET_NO_PROXY'] = 'mirror.example.invalid'
                    live = dict(expected)
                    if 'CI_FLEET_RUNNER_IMAGE_DIGEST' in expected:
                        live['CI_FLEET_RUNNER_IMAGE'] = expected['CI_FLEET_RUNNER_RUNTIME_IMAGE_ID' if portable else 'CI_FLEET_RUNNER_IMAGE_DIGEST']
                    if stale:
                        live['CI_FLEET_APT_HTTP_PROXY'] = 'http://stale.example.invalid:3142'
                    for name, values in (('expected', expected), ('live', live)):
                        text = ''.join(f'{key}={value}\n' for key, value in values.items())
                        if duplicate and name == 'live':
                            text += 'CI_FLEET_APT_HTTP_PROXY=\nCI_FLEET_APT_HTTP_PROXY=\n'
                        (root / f'{name}.env').write_text(text)
                    result = subprocess.run(['bash', str(script)], env=os.environ | {'APT_TEST_ROOT': str(root), 'APT_TEST_PORTABLE': str(portable).lower()}, capture_output=True)
                    self.assertEqual(result.returncode == 0, not stale and not duplicate, (portable, configured, stale, duplicate, result.stderr))


if __name__ == "__main__":
    unittest.main()
