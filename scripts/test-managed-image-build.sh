#!/usr/bin/env bash
set -Eeuo pipefail
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
python3 - "$repo_root" <<'PY'
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

repo = Path(sys.argv[1])
fixture = (repo / 'scripts/test-install-worker-controller.sh').read_text()
docker_source = fixture.split('cat >"$fake_bin/docker" <<\'EOF\'\n', 1)[1].split('\nEOF', 1)[0]
with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    mock = root / 'docker'
    mock.write_text(docker_source)
    mock.chmod(0o755)
    old_revision, revision = '1' * 40, '2' * 40
    runner_id, controller_id, wrong_id = ('sha256:' + value * 64 for value in ('4', '3', '7'))
    old_runner, old_controller = ('sha256:' + value * 64 for value in ('5', '6'))
    environment = root / 'candidate.env'
    values = dict(CI_FLEET_ENGINE_REF=revision, CI_FLEET_COMMIT=revision, CI_FLEET_VERSION=revision[:12],
                  CI_FLEET_RUNNER_IMAGE='ci-fleet-runner:fixture', CI_FLEET_CONTROLLER_IMAGE='ci-fleet-controller:fixture',
                  CI_FLEET_RUNNER_IMAGE_DIGEST=runner_id, CI_FLEET_CONTROLLER_IMAGE_DIGEST=controller_id)
    environment.write_text(''.join(f'{key}={value}\n' for key, value in values.items()))
    env = os.environ | dict(PATH=str(root) + ':' + os.environ['PATH'],
                           FAKE_DOCKER_STATE=str(root / 'running'),
                           FAKE_RUNNER_IMAGE=values['CI_FLEET_RUNNER_IMAGE'],
                           FAKE_CONTROLLER_IMAGE=values['CI_FLEET_CONTROLLER_IMAGE'],
                           FAKE_RUNNER_IMAGE_STATE=str(root / 'runner-state'),
                           FAKE_CONTROLLER_IMAGE_STATE=str(root / 'controller-state'),
                           FAKE_RUNNER_IMAGE_ID_STATE=str(root / 'runner-id'),
                           FAKE_CONTROLLER_IMAGE_ID_STATE=str(root / 'controller-id'),
                           FAKE_AVAILABLE_IMAGE_IDS=str(root / 'available'),
                           FAKE_BUILDER_LOG=str(root / 'builders'),
                           FAKE_PARTIAL_RUNNER_IMAGE_ID=wrong_id)
    release = root / 'opaque-release'
    release.mkdir()
    (release / 'scripts').symlink_to(repo / 'scripts', target_is_directory=True)
    (release / 'engine-capabilities.json').write_text('{"schema_version":1,"capabilities":{"portable_managed_image_ids":false}}')
    builder_names = set()
    for case in ('create_failure', 'bootstrap_failure', 'wrong_backend', 'missing_backend', 'partial', 'runner_mismatch', 'controller_mismatch', 'prepare', 'success'):
        (root / 'builders').write_text('')
        (root / 'running').touch()
        (root / 'runner-state').write_text(old_revision + '\n')
        (root / 'controller-state').write_text(old_revision + '\n')
        (root / 'runner-id').write_text(old_runner + '\n')
        (root / 'controller-id').write_text(old_controller + '\n')
        (root / 'available').write_text(old_runner + '\n' + old_controller + '\n')
        injected = dict(env)
        if case == 'create_failure': injected['FAKE_FAIL_BUILDER_CREATE'] = '1'
        if case == 'bootstrap_failure': injected['FAKE_FAIL_BUILDER_BOOTSTRAP'] = '1'
        if case == 'wrong_backend': injected['FAKE_BUILDKIT_VERSION'] = 'v0.32.1'
        if case == 'missing_backend': injected['FAKE_BUILDKIT_VERSION'] = ''
        if case == 'partial': injected['FAKE_PARTIAL_BUILD_FAIL'] = '1'
        if case == 'runner_mismatch': injected['FAKE_BUILT_RUNNER_IMAGE_ID'] = wrong_id
        if case == 'controller_mismatch': injected['FAKE_BUILT_CONTROLLER_IMAGE_ID'] = wrong_id
        command = ['bash', str(repo / 'scripts/build-managed-images.sh'), str(release), str(environment)]
        if case == 'prepare': command.append(str(root / 'manifest'))
        result = subprocess.run(command,
                                env=injected, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        assert (result.returncode == 0) == (case in ('prepare', 'success')), (case, result.stdout)
        operations = (root / 'builders').read_text().splitlines()
        create = shlex.split(operations[0].split('|', 1)[1])
        builder = create[create.index('--name') + 1]
        assert builder.startswith('ci-fleet-managed-') and builder not in builder_names, operations
        builder_names.add(builder)
        assert operations[1:] == ([] if case == 'create_failure' else [f'rm|buildx rm --force {builder}']), operations
        expected = (runner_id, controller_id) if case == 'success' else (old_runner, old_controller)
        assert tuple((root / name).read_text().strip() for name in ('runner-id', 'controller-id')) == expected, case
        assert (root / 'running').exists(), case
        if case == 'prepare':
            tags = (root / 'manifest').read_text().splitlines()
            assert len(tags) == 2 and all('-staged-' in tag for tag in tags), tags
        if case != 'success':
            assert (root / 'runner-state').read_text().strip() == old_revision, case
            assert (root / 'controller-state').read_text().strip() == old_revision, case
    (root / 'runner-state').write_text(old_revision + '\n')
    (root / 'runner-id').write_text(old_runner + '\n')
    subprocess.run([str(mock), 'image', 'tag', runner_id, values['CI_FLEET_RUNNER_IMAGE']], env=env, check=True)
    assert (root / 'runner-state').read_text().strip() == revision
    assert (root / 'runner-id').read_text().strip() == runner_id
    for case in ('enabled', 'failure', 'invalid', 'removed', 'ambient'):
        secret_copy = root / 'secret-copy'
        secret_copy.unlink(missing_ok=True)
        proxy = 'http://cache.example.invalid:3142'
        extra = '' if case in ('removed', 'ambient') else f'CI_FLEET_APT_HTTP_PROXY={proxy}\n'
        if case == 'invalid': extra = 'CI_FLEET_APT_HTTP_PROXY=http://user:password@cache.example.invalid\n'
        environment.write_text(''.join(f'{key}={value}\n' for key, value in values.items()) + extra)
        injected = dict(env, FAKE_APT_SECRET_CONFIG=str(secret_copy))
        if case == 'ambient': injected['CI_FLEET_APT_HTTP_PROXY'] = proxy
        if case == 'failure': injected['FAKE_FAIL_BUILD'] = '1'
        result = subprocess.run(['bash', str(repo / 'scripts/build-managed-images.sh'), str(release), str(environment)],
                                env=injected, capture_output=True, text=True)
        assert (result.returncode == 0) == (case in ('enabled', 'removed', 'ambient')), (case, result.stderr)
        assert secret_copy.exists() == (case == 'enabled'), case
        if secret_copy.exists():
            assert f'Acquire::http::Proxy "{proxy}";' in secret_copy.read_text()
            assert 'Acquire::https::Proxy "DIRECT";' in secret_copy.read_text()
        assert proxy not in result.stdout + result.stderr and 'password' not in result.stdout + result.stderr
print('MANAGED_IMAGE_STAGING_TESTS_OK')
PY
