#!/usr/bin/env python3
"""Check conservative image selection and one compatible direct reference pair."""
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import tarfile
import unittest

from local_validate import image_comparison_required, NO_PRIOR
from test_apt_cache_e2e import release_archive

ROOT = Path(__file__).resolve().parents[1]


class ImageSelectionTests(unittest.TestCase):
    def test_only_known_documentation_can_omit_the_pair_comparison(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            def git(*args):
                return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()
            def commit():
                git('add', '.')
                git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'test: fixture')
                return git('rev-parse', 'HEAD')
            git('init', '-q')
            (root / 'controller').mkdir(); (root / 'controller/Dockerfile').write_text('FROM fixture\n')
            base = commit()
            (root / 'docs').mkdir(); (root / 'docs/guide.md').write_text('guide\n')
            docs = commit()
            self.assertFalse(image_comparison_required(root, base, docs))
            self.assertTrue(image_comparison_required(root, NO_PRIOR, docs))
            (root / 'unknown-input').write_text('unknown\n')
            unknown = commit()
            self.assertTrue(image_comparison_required(root, docs, unknown))
            (root / 'controller/Dockerfile').rename(root / 'docs/former-dockerfile.md')
            renamed = commit()
            self.assertTrue(image_comparison_required(root, unknown, renamed))


class ReferenceBuildTests(unittest.TestCase):
    def test_cache_fixture_contains_the_portable_identity_capability_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'engine-capabilities.json').write_text('{"portable_managed_image_ids": true}')
            (root / 'runner').mkdir(); (root / 'runner/Dockerfile').write_text('FROM fixture')
            (root / 'unrelated').write_text('outside build fixture')
            raw = release_archive(root, b'engine-capabilities.json\0runner/Dockerfile\0unrelated\0')
            with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
                self.assertEqual(set(archive.getnames()), {'engine-capabilities.json', 'runner/Dockerfile'})
                self.assertEqual(archive.extractfile('engine-capabilities.json').read(), (root / 'engine-capabilities.json').read_bytes())

    def test_direct_pair_uses_managed_arguments_and_cleans_its_builder_on_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            docker = root / 'docker'
            docker.write_text('''#!/usr/bin/python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with Path(os.environ['COMMANDS']).open('a') as log: log.write(json.dumps(args) + '\\n')
if args[:2] == ['buildx', 'inspect']: print('BuildKit version: v0.32.2')
if args[:2] == ['buildx', 'build'] and 'controller/Dockerfile' in args:
    raise SystemExit(int(os.environ.get('FAIL_BUILD', '0')))
''')
            docker.chmod(0o755)
            for failure in ('0', '37'):
                log = root / ('commands-' + failure)
                env = dict(os.environ, PATH=str(root) + ':' + os.environ['PATH'], COMMANDS=str(log),
                           CI_FLEET_COMMIT='a' * 40, CI_FLEET_VERSION='a' * 12,
                           CI_FLEET_RUNNER_IMAGE='fixture-runner', CI_FLEET_CONTROLLER_IMAGE='fixture-controller', FAIL_BUILD=failure)
                result = subprocess.run(['bash', str(ROOT / 'scripts/build-validation-images.sh')],
                                        cwd=ROOT, env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, int(failure), result.stderr)
                commands = [json.loads(line) for line in log.read_text().splitlines()]
                builds = [command for command in commands if command[:2] == ['buildx', 'build']]
                self.assertEqual(len(builds), 2)
                for build in builds:
                    for argument in ('--no-cache', '--load', '--provenance=false', '--sbom=false',
                                     'CI_FLEET_COMMIT=' + 'a' * 40, 'SOURCE_DATE_EPOCH=1786752000'):
                        self.assertIn(argument, build)
                    self.assertNotIn('--secret', build)
                self.assertIn('CI_FLEET_VERSION=' + 'a' * 12, builds[1])
                self.assertEqual(commands[-1][:3], ['buildx', 'rm', '--force'])


if __name__ == '__main__':
    unittest.main()
