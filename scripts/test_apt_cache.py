#!/usr/bin/env python3
"""Optional APT cache validation, rendering, and endpoint redaction tests."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from apt_cache import apt_config
from desired_state import DesiredStateError, build_rendered_env, load_engine_capabilities, validate_host_values
from test_desired_state import config, host_values, CONFIG_COMMIT, ROOT


class APTCacheTests(unittest.TestCase):
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
docker() { cat "$APT_TEST_ROOT/live.env"; }
controller_container=fixture
candidate_env=$APT_TEST_ROOT/expected.env
controller_environment_matches
''')
            for configured, stale, duplicate in ((False, False, False), (False, True, False),
                                                  (True, False, False), (True, True, False), (False, False, True)):
                expected = dict(environment)
                if configured:
                    expected['CI_FLEET_APT_HTTP_PROXY'] = 'http://cache.example.invalid:3142'
                    expected['CI_FLEET_NO_PROXY'] = 'mirror.example.invalid'
                live = dict(expected)
                if 'CI_FLEET_RUNNER_IMAGE_DIGEST' in expected:
                    live['CI_FLEET_RUNNER_IMAGE'] = expected['CI_FLEET_RUNNER_IMAGE_DIGEST']
                if stale:
                    live['CI_FLEET_APT_HTTP_PROXY'] = 'http://stale.example.invalid:3142'
                for name, values in (('expected', expected), ('live', live)):
                    text = ''.join(f'{key}={value}\n' for key, value in values.items())
                    if duplicate and name == 'live':
                        text += 'CI_FLEET_APT_HTTP_PROXY=\nCI_FLEET_APT_HTTP_PROXY=\n'
                    (root / f'{name}.env').write_text(text)
                result = subprocess.run(['bash', str(script)], env=os.environ | {'APT_TEST_ROOT': str(root)}, capture_output=True)
                self.assertEqual(result.returncode == 0, not stale and not duplicate, (configured, stale, duplicate, result.stderr))


if __name__ == "__main__":
    unittest.main()
