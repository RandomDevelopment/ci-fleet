#!/usr/bin/env python3
"""Adversarial checks for the deployment Docker and recovery boundary."""
import base64
import copy
import errno
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import deployer_adapter as adapter


class Backend:
    def __init__(self):
        self.resources = {}
        self.calls = []

    def inspect(self, kind, name):
        return self.resources[kind, name]

    def request(self, method, path, body=b'', headers=None):
        self.calls.append((method, path, body))
        return 200, b'{}'


class Containment(unittest.TestCase):
    def setUp(self):
        self.backend = Backend()
        self.values = {'ARTIFACT_IMAGE': 'registry.example.invalid/app@sha256:' + 'a' * 64,
                       'DEPLOYER_IDENTITY': 'example', 'ENVIRONMENT': 'staging', 'TARGET_ID': 'app'}
        self.policy = adapter.Policy(self.values, self.backend, '/approved/application')
        self.backend.resources['images', self.policy.image] = {'Config': {'Volumes': None}}
        self.network = 'ci-fleet-deploy-example-network'
        self.volume = 'ci-fleet-deploy-example-volume'
        self.backend.resources['networks', self.network] = {'Labels': self.policy.labels, 'Driver': 'bridge', 'Internal': True}
        self.backend.resources['volumes', self.volume] = {'Labels': self.policy.labels, 'Driver': 'local', 'Options': None}
        self.body = {'Image': self.policy.image, 'Labels': self.policy.labels,
                     'HostConfig': {'CapDrop': ['ALL'], 'ReadonlyRootfs': True, 'SecurityOpt': ['no-new-privileges'],
                                    'CgroupnsMode': 'private', 'Memory': 104857600, 'NanoCpus': 1000000000,
                                    'PidsLimit': 32, 'NetworkMode': self.network,
                                    'Mounts': [{'Type': 'volume', 'Source': self.volume, 'Target': '/data'}]}}

    def test_allowed_container_and_cli_neutral_fields(self):
        value = copy.deepcopy(self.body)
        value.update({'Hostname': '', 'Domainname': '', 'Volumes': None})
        value['HostConfig'].update({'Links': None, 'Devices': None, 'ShmSize': 67108864, 'ConsoleSize': [0, 0]})
        value['NetworkingConfig'] = {'EndpointsConfig': {self.network: {'IPAMConfig': None, 'Aliases': None}}}
        path, _ = self.policy.authorize('POST', '/v1.47/containers/create?name=ci-fleet-deploy-example-app', json.dumps(value).encode())
        self.assertEqual(path, '/containers/create?name=ci-fleet-deploy-example-app')

    def test_pinned_chromium_profile_through_real_authorization(self):
        profile = json.loads((Path(__file__).resolve().parents[1] / 'deploy/seccomp/chromium-v1.json').read_text())
        canonical = json.dumps(profile, sort_keys=True, separators=(',', ':'))
        self.assertEqual(hashlib.sha256(canonical.encode()).hexdigest(), adapter.CHROMIUM_SECCOMP_V1_SHA256)
        for no_new_privileges in ('no-new-privileges', 'no-new-privileges:true'):
            for encoded in (canonical, json.dumps(profile, indent=2)):
                body = copy.deepcopy(self.body)
                body['User'] = '1000'
                body['HostConfig']['SecurityOpt'] = [no_new_privileges, 'seccomp=' + encoded]
                _, forwarded = self.policy.authorize('POST', '/containers/create?name=ci-fleet-deploy-example-browser', json.dumps(body).encode())
                self.assertEqual(json.loads(forwarded)['HostConfig']['SecurityOpt'], body['HostConfig']['SecurityOpt'])
        for field, value in (('CapAdd', ['SYS_ADMIN']), ('ReadonlyRootfs', False), ('NetworkMode', 'host')):
            with self.subTest(field=field), self.assertRaises((ValueError, KeyError)):
                body['HostConfig'][field] = value
                self.policy.container(body)
            body = copy.deepcopy(self.body)
            body['User'] = '1000'
            body['HostConfig']['SecurityOpt'] = ['no-new-privileges', 'seccomp=' + canonical]

    def test_chromium_profile_requires_declared_non_root_user(self):
        profile = (Path(__file__).resolve().parents[1] / 'deploy/seccomp/chromium-v1.json').read_text()
        body = copy.deepcopy(self.body)
        body['HostConfig']['SecurityOpt'] = ['no-new-privileges', 'seccomp=' + profile]
        image = self.backend.resources['images', self.policy.image]['Config']
        for user in ('', 'root', '0', '000:1000', 'root:1000', '+0', '-0', '+000:1000', '-000:1000', '-1'):
            with self.subTest(user=user), self.assertRaises(ValueError):
                image['User'] = user
                self.policy.container(body)
        image['User'] = '1000'
        self.policy.container(body)
        for user in ('root', '+0', '-0'):
            body['User'] = user
            with self.subTest(override=user), self.assertRaises(ValueError):
                self.policy.container(body)
        body['User'] = '+1001'
        self.policy.container(body)

    def test_reject_unreviewed_seccomp_and_missing_privilege_restriction(self):
        profile = json.loads((Path(__file__).resolve().parents[1] / 'deploy/seccomp/chromium-v1.json').read_text())
        changed = copy.deepcopy(profile)
        changed['defaultAction'] = 'SCMP_ACT_ALLOW'
        added = copy.deepcopy(profile)
        added['syscalls'][0]['names'].append('mount')
        encoded = 'seccomp=' + json.dumps(profile)
        invalid = ([encoded], ['no-new-privileges', encoded, encoded], [encoded, 'no-new-privileges'],
                   ['no-new-privileges', 'seccomp=unconfined'], ['no-new-privileges', 'seccomp=/tmp/profile.json'],
                   ['no-new-privileges', 'seccomp=' + json.dumps(changed)],
                   ['no-new-privileges', 'seccomp=' + json.dumps(added)],
                   ['no-new-privileges', 'seccomp={"defaultAction":1,"defaultAction":2}'],
                   ['no-new-privileges', 'seccomp={"value":NaN}'], ['no-new-privileges', 'seccomp=[]'],
                   ['no-new-privileges', None])
        for options in invalid:
            with self.subTest(options=options), self.assertRaises(ValueError):
                body = copy.deepcopy(self.body)
                body['HostConfig']['SecurityOpt'] = options
                self.policy.authorize('POST', '/containers/create?name=ci-fleet-deploy-example-browser', json.dumps(body).encode())

    def test_empty_request_user_inherits_reviewed_image_user(self):
        profile = (Path(__file__).resolve().parents[1] / 'deploy/seccomp/chromium-v1.json').read_text()
        body = copy.deepcopy(self.body)
        body['User'] = ''
        body['HostConfig']['SecurityOpt'] = ['no-new-privileges', 'seccomp=' + profile]
        image = self.backend.resources['images', self.policy.image]['Config']
        image['User'] = '1000'
        self.policy.authorize('POST', '/containers/create?name=ci-fleet-deploy-example-browser', json.dumps(body).encode())
        for user in ('', 'root', '0', '+0', '-0'):
            with self.subTest(image_user=user), self.assertRaises(ValueError):
                image['User'] = user
                self.policy.container(body)

    def test_image_volumes_cannot_create_unlabelled_resources(self):
        image = self.backend.resources['images', self.policy.image]
        image['Config']['Volumes'] = {'/data': {}}
        self.policy.container(self.body)
        body = copy.deepcopy(self.body)
        body['HostConfig'].pop('Mounts')
        with self.assertRaises(ValueError):
            self.policy.container(body)
        body['HostConfig']['Binds'] = [self.volume + ':/data:rw']
        self.policy.container(body)
        image['Config']['Volumes']['/anonymous'] = {}
        with self.assertRaises(ValueError):
            self.policy.container(body)
        image['Config']['Volumes'] = ['/malformed']
        with self.assertRaises(ValueError):
            self.policy.container(body)

    def test_host_escape_requests(self):
        for key, value in {'Privileged': True, 'CapAdd': ['SYS_ADMIN'], 'PidMode': 'host', 'IpcMode': 'host',
                           'UsernsMode': 'host', 'CgroupnsMode': 'host', 'Devices': [{'PathOnHost': '/dev/sda'}],
                           'SecurityOpt': ['seccomp=unconfined'], 'NetworkMode': 'host', 'CgroupParent': '/system.slice',
                           'Runtime': 'unreviewed', 'ReadonlyRootfs': False, 'PidsLimit': 0, 'GroupAdd': ['1']}.items():
            with self.subTest(key=key), self.assertRaises((ValueError, KeyError)):
                body = copy.deepcopy(self.body)
                body['HostConfig'][key] = value
                self.policy.container(body)

    def test_host_journal_binds_and_volume_driver_options(self):
        for source in ('/etc', '/var/lib/ci-fleet-deployer', '/opt/ci-fleet-deployer/.installer-transactions', '/run/docker.sock'):
            with self.subTest(source=source), self.assertRaises(ValueError):
                self.policy.mounts({'Binds': [source + ':/data:rw']})
        self.backend.resources['volumes', self.volume]['Options'] = {'device': '/', 'o': 'bind'}
        with self.assertRaises(ValueError):
            self.policy.mounts({'Mounts': [{'Type': 'volume', 'Source': self.volume, 'Target': '/data'}]})
        self.policy.mounts({'Binds': ['/approved/application:/run/secrets/application:ro']})
        with self.assertRaises(ValueError):
            self.policy.mounts({'Binds': ['/approved/application:/run/secrets/application:rw']})

    def test_other_target_and_alternate_network(self):
        self.backend.resources['networks', self.network]['Labels'] = {}
        with self.assertRaises(ValueError):
            self.policy.container(self.body)
        for endpoint in ({'another-network': {}}, {self.network: {'Aliases': ['other-app']}}):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                self.policy.container(dict(self.body, NetworkingConfig={'EndpointsConfig': endpoint}))

    def test_inventory_filters_cannot_remove_scope(self):
        path, _ = self.policy.authorize('GET', '/containers/json?all=1&filters=%7B%22label%22%3A%5B%22attacker%22%5D%7D', b'')
        filters = json.loads(adapter.urllib.parse.parse_qs(adapter.urllib.parse.urlsplit(path).query)['filters'][0])
        self.assertEqual(set(filters['label']), {f'{k}={v}' for k, v in self.policy.labels.items()})

    def test_forbidden_api_and_paths(self):
        for method, path in [('POST', '/build'), ('POST', '/containers/app/exec'), ('POST', '/containers/prune'),
                             ('POST', '/volumes/prune'), ('POST', '/networks/app/connect'), ('GET', '/containers/app/archive'),
                             ('POST', '/plugins/create'), ('GET', 'http://daemon/info'), ('GET', '/v1.47/../info'),
                             ('GET', '/%252finfo'), ('GET', '/info?x=1&x=2')]:
            with self.subTest(path=path), self.assertRaises((ValueError, KeyError)):
                self.policy.authorize(method, path, b'')

    def test_read_only_operations_and_cleanup(self):
        for operation in ('validate', 'health', 'cleanup'):
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                adapter.Policy(self.values, self.backend, operation=operation).authorize('POST', '/containers/create?name=ci-fleet-deploy-example-app', json.dumps(self.body).encode())

    def test_network_and_volume_creation(self):
        for path, body in [('/networks/create', {'Name': self.network, 'Labels': self.policy.labels, 'Internal': True}),
                           ('/volumes/create', {'Name': self.volume, 'Labels': self.policy.labels})]:
            self.policy.authorize('POST', path, json.dumps(body).encode())
            body['DriverOpts'] = {'device': '/', 'o': 'bind'}
            with self.assertRaises(ValueError):
                self.policy.authorize('POST', path, json.dumps(body).encode())

    def test_image_pull_identity(self):
        query = adapter.urllib.parse.urlencode({'fromImage': self.policy.image})
        self.policy.authorize('POST', '/images/create?' + query, b'')
        with self.assertRaises(ValueError):
            self.policy.authorize('POST', '/images/create?fromImage=unreviewed:latest', b'')

    def test_rootful_socket_acl_fails_closed(self):
        peer = MagicMock()
        peer.getsockopt.return_value = adapter.struct.pack('3i', 123, 45678, 45678)
        status = 'Uid: 45678 45678 45678 45678\nGid: 45678 45678 45678 45678\nGroups: 45678\n'
        for acl, rejected in [(OSError(errno.ENODATA, 'absent'), False),
                              (OSError(errno.ENOTSUP, 'unsupported'), False),
                              (b'named-user-acl', True), (OSError(errno.EACCES, 'denied'), True)]:
            def access_acl(path, *_args, **_kwargs):
                if str(path) != '/run/docker.sock':
                    raise OSError(errno.ENODATA, 'absent')
                if isinstance(acl, OSError):
                    raise acl
                return acl
            with self.subTest(acl=acl), patch.object(adapter.socket, 'socket', return_value=peer), \
                 patch.object(adapter.os, 'lstat', side_effect=lambda path: SimpleNamespace(st_mode=stat.S_IFSOCK | 0o600, st_uid=45678, st_gid=45678) if str(path) == '/run/rootless/docker.sock' else SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0, st_gid=0)), \
                 patch.object(adapter.Path, 'read_text', return_value=status), \
                 patch.object(adapter.grp, 'getgrnam', side_effect=KeyError), \
                 patch.object(adapter.grp, 'getgrgid', return_value=SimpleNamespace(gr_mem=[])), \
                 patch.object(adapter.pwd, 'getpwall', return_value=[SimpleNamespace(pw_uid=45678, pw_gid=45678)]), \
                 patch.object(adapter.Path, 'exists', return_value=True), \
                 patch.object(adapter.Path, 'resolve', return_value=Path('/run/docker.sock')), \
                 patch.object(adapter.Path, 'stat', return_value=SimpleNamespace(st_uid=0, st_gid=0, st_mode=stat.S_IFSOCK | 0o660)), \
                 patch.object(adapter.os, 'getxattr', side_effect=access_acl):
                connection = adapter.UnixConnection('/run/rootless/docker.sock')
                if rejected:
                    with self.assertRaises(ValueError):
                        connection.connect()
                else:
                    connection.connect()

    def test_backend_socket_rejects_other_principals(self):
        for case in ('safe', 'world', 'wrong-group', 'acl', 'parent-writable', 'parent-owner', 'member', 'primary-member'):
            with self.subTest(case=case):
                def metadata(path):
                    if str(path) == '/run/private/docker.sock':
                        return SimpleNamespace(st_mode=stat.S_IFSOCK | (0o666 if case == 'world' else 0o660),
                                               st_uid=45678, st_gid=12345 if case == 'wrong-group' else 45678)
                    return SimpleNamespace(st_mode=stat.S_IFDIR | (0o777 if case == 'parent-writable' else 0o755),
                                           st_uid=12345 if case == 'parent-owner' else 0)
                with patch.object(adapter.os, 'lstat', side_effect=metadata), \
                     patch.object(adapter.os, 'getxattr', **({'return_value': b'extended'} if case == 'acl' else {'side_effect': OSError(errno.ENODATA, 'absent')})), \
                     patch.object(adapter.grp, 'getgrgid', return_value=SimpleNamespace(gr_mem=['outsider'] if case == 'member' else [])), \
                     patch.object(adapter.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=12345)), \
                     patch.object(adapter.pwd, 'getpwall', return_value=[SimpleNamespace(pw_uid=12345 if case == 'primary-member' else 45678, pw_gid=45678)]):
                    if case == 'safe':
                        adapter.check_backend_socket('/run/private/docker.sock', 45678, 45678)
                    else:
                        with self.assertRaises(ValueError):
                            adapter.check_backend_socket('/run/private/docker.sock', 45678, 45678)

    def test_pull_ownership_requires_success_and_confirmed_digest(self):
        for status, response, recorded in [(404, b'{"message":"absent"}', False),
                                           (200, b'{"errorDetail":{"message":"denied"},"error":"denied"}\n', False),
                                           (200, b'{"status":"Pull complete"}\n', True),
                                           (200, b'not-json', False), (200, b'', False)]:
            with self.subTest(status=status, response=response), tempfile.TemporaryDirectory() as directory, \
                 patch.dict(os.environ, {'CI_FLEET_DEPLOYER_TESTING': '1'}):
                ledger = Path(directory) / 'artifacts.json'
                policy = adapter.Policy(self.values, self.backend, artifact_store=ledger)
                self.backend.request = lambda *_args: (status, response)
                self.backend.resources['images', policy.image] = {'RepoDigests': [policy.image]}
                path = str(Path(directory) / 'broker.sock')
                with adapter.ProxyServer(path, adapter.handler(policy)) as server:
                    thread = threading.Thread(target=server.serve_forever)
                    thread.start()
                    try:
                        connection = adapter.UnixConnection(path, rootless=False)
                        query = adapter.urllib.parse.urlencode({'fromImage': policy.image})
                        connection.request('POST', '/images/create?' + query)
                        result = connection.getresponse()
                        result.read()
                        connection.close()
                    finally:
                        server.shutdown()
                        thread.join()
                self.assertEqual(policy.artifact_owned(policy.image), recorded)

    def test_registry_auth_is_not_forwarded(self):
        auth = base64.b64encode(json.dumps({'username': 'application', 'password': 'secret-canary'}).encode()).decode()
        for operation in ('deploy', 'rollback'):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as directory:
                policy = adapter.Policy(self.values, self.backend, operation=operation)
                self.backend.resources['images', policy.image] = {'RepoDigests': [policy.image]}
                request = MagicMock(return_value=(200, b'{"status":"Pull complete"}\n'))
                self.backend.request = request
                path = str(Path(directory) / 'broker.sock')
                with adapter.ProxyServer(path, adapter.handler(policy)) as server:
                    thread = threading.Thread(target=server.serve_forever)
                    thread.start()
                    try:
                        connection = adapter.UnixConnection(path, rootless=False)
                        query = adapter.urllib.parse.urlencode({'fromImage': policy.image})
                        connection.request('POST', '/images/create?' + query,
                                           headers={'x-registry-auth': auth, 'X-Registry-Config': auth})
                        result = connection.getresponse()
                        self.assertEqual(result.status, 200)
                        result.read()
                        connection.close()
                    finally:
                        server.shutdown()
                        thread.join()
                self.assertEqual(request.call_args.args[3], {'Content-Type': 'application/json'})

    def test_adapter_has_no_caller_stdin(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            credentials, runtime = root / 'credentials', root / 'runtime'
            credentials.mkdir(); runtime.mkdir()
            (credentials / 'policy').write_text('CREDENTIAL_PROVIDER=file\n')
            (credentials / 'adapter').write_text('#!/bin/sh\nread value && exit 1\nexit 0\n')
            with patch.dict(os.environ, {'CREDENTIALS_DIRECTORY':str(credentials), 'RUNTIME_DIRECTORY':str(runtime),
                                        'CI_FLEET_DEPLOYER_CONTAINER_CREDENTIAL':'/approved/application'}):
                result = subprocess.run([adapter.sys.executable, adapter.__file__, 'execute', 'health'],
                                        input=b'caller-input\n', capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_sandbox_properties(self):
        command = adapter.sandbox_command('ci-fleet-deployer-adapter-1-2.service', 120, Path('/run/broker.sock'),
                                         Path('/policy'), Path('/adapter'), Path('/credential'), Path('/helper'),
                                         Path('/request'), True, 'rollback', '/run/rootless/docker.sock', '/approved/application')
        for setting in ('DynamicUser=yes', 'PrivateNetwork=yes', 'NoNewPrivileges=yes', 'ProtectSystem=strict',
                        'CapabilityBoundingSet=', 'KillMode=control-group', 'RestrictNamespaces=yes'):
            self.assertIn('--property=' + setting, command)
        inaccessible = next(x for x in command if x.startswith('--property=InaccessiblePaths='))
        self.assertIn('/run/rootless/docker.sock', inaccessible)
        self.assertEqual(command[-1], 'rollback')
        self.assertFalse(any('ReadWritePaths=/var' in item for item in command))

    def test_unit_status_fails_closed(self):
        for code, stdout, expected in [(0, 'LoadState=loaded\nActiveState=inactive\n', True),
                                       (1, 'LoadState=not-found\nActiveState=inactive\n', True),
                                       (0, 'LoadState=loaded\nActiveState=active\n', False),
                                       (0, 'LoadState=loaded\nActiveState=inactive\nActiveState=active\n', False),
                                       (1, '', False), (2, 'LoadState=not-found\nActiveState=inactive\n', False)]:
            with self.subTest(stdout=stdout), patch.object(adapter.subprocess, 'run', return_value=subprocess.CompletedProcess([], code, stdout)):
                self.assertEqual(adapter.unit_inactive('unit'), expected)

    def test_durable_file_atomic_protected_and_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'journal'
            adapter.durable_file(path, b'original')
            self.assertEqual(adapter.protected_file(path, os.getuid()), b'original')
            adapter.durable_file(path, b'replacement')
            self.assertEqual(path.read_bytes(), b'replacement')
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            link = Path(directory) / 'link'
            link.symlink_to(path)
            with self.assertRaises(ValueError):
                adapter.protected_file(link, os.getuid())
            with self.assertRaises(ValueError):
                adapter.trusted_directory(Path(directory), os.getuid(), 0o700)

    def test_recovery_survives_dispatch_failure(self):
        # The production directory check is separate; this fixture has a /tmp parent.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = root / '.adapter-recovery'
            runtime = root / 'runtime'
            store.mkdir(mode=0o700)
            runtime.mkdir(mode=0o711)
            credential_store = root / 'container-credentials'
            credential_store.mkdir(mode=0o711)
            source = root / 'adapter'
            source.write_bytes(b'#!/bin/sh\nexit 0\n')
            source.chmod(0o700)
            credential = root / 'application'
            adapter.durable_file(credential, b'fixture-only')
            values = dict(self.values, CREDENTIAL_PROVIDER='file', CREDENTIAL_REF=str(credential),
                          ADAPTER_SHA256=adapter.hashlib.sha256(source.read_bytes()).hexdigest(), DOCKER_SOCKET='/run/rootless/docker.sock')
            policy = root / 'policy'
            adapter.durable_file(policy, ''.join(f'{k}={v}\n' for k, v in values.items()).encode())
            adapter.durable_file(root / 'install-state.json', b'{"fixture":"incumbent"}')
            deployed = root / 'deployed'
            snapshot = deployed / '.snapshot.fixture'
            snapshot.mkdir(parents=True, mode=0o700)
            adapter.durable_file(snapshot / 'state.json', b'{"fixture":"deployed"}')
            adapter.durable_file(snapshot / 'policy.conf', ('ARTIFACT_IMAGE=' + self.values['ARTIFACT_IMAGE'] + '\n').encode())
            (deployed / 'current').symlink_to(snapshot.name)
            args = SimpleNamespace(policy=policy, adapter=source, store=store, runtime=runtime,
                                   credential_store=credential_store, unit='ci-fleet-deployer-adapter-1-2.service', seconds=120, request=None,
                                   marker=None, operation='deploy')
            backend = SimpleNamespace(uid=os.getuid(), gid=os.getgid(), path='/run/rootless/docker.sock')
            with patch.dict(os.environ, {'CI_FLEET_DEPLOYER_TESTING': '1'}), \
                 patch.object(adapter, 'Backend', return_value=backend), patch.object(adapter, 'trusted_directory'), \
                 patch.object(adapter.subprocess, 'run', side_effect=OSError('interrupted dispatcher')):
                with self.assertRaises(OSError):
                    adapter.run_adapter(args)
            journal = store / args.unit
            self.assertEqual(json.loads((journal / 'status.json').read_text())['status'], 'inflight')
            self.assertEqual((journal / 'install-state.json').read_bytes(), b'{"fixture":"incumbent"}')
            self.assertEqual((journal / 'deployed/state.json').read_bytes(), b'{"fixture":"deployed"}')
            self.assertFalse(any('credential-' in path.name for path in runtime.iterdir()))
            self.assertFalse(any(path.name == 'application' for path in journal.rglob('*')))
            advertised = []
            def successful_dispatch(command, **kwargs):
                if command[0] == 'systemd-run':
                    source = next(item.split('=', 2)[2] for item in command if item.startswith('--setenv=CI_FLEET_DEPLOYER_CONTAINER_CREDENTIAL='))
                    self.assertEqual(kwargs.get('stdin'), subprocess.DEVNULL)
                    advertised.append(Path(source))
                    self.assertEqual(Path(source).read_bytes(), b'fixture-only')
                return subprocess.CompletedProcess(command, 0, 'LoadState=loaded\nActiveState=inactive\n')
            with patch.dict(os.environ, {'CI_FLEET_DEPLOYER_TESTING': '1'}), \
                 patch.object(adapter, 'Backend', return_value=backend), patch.object(adapter, 'trusted_directory'), \
                 patch.object(adapter.subprocess, 'run', side_effect=successful_dispatch):
                args.unit = 'ci-fleet-deployer-adapter-1-3.service'
                self.assertEqual(adapter.run_adapter(args), 0)
                self.assertEqual(advertised[-1].read_bytes(), b'fixture-only')
                adapter.shutil.rmtree(runtime)
                runtime.mkdir(mode=0o711)
                args.unit = 'ci-fleet-deployer-adapter-1-4.service'
                self.assertEqual(adapter.run_adapter(args), 0)
            self.assertEqual(advertised[0], advertised[1])
            self.assertEqual(advertised[0].read_bytes(), b'fixture-only')
            self.assertFalse(list(runtime.iterdir()))

    def test_retire_completed_journal_preserves_inflight(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Path(directory)
            unit = 'ci-fleet-deployer-adapter-1-2.service'
            journal = store / unit
            journal.mkdir(mode=0o700)
            status = journal / 'status.json'
            with patch.object(adapter, 'trusted_directory'):
                adapter.durable_file(status, json.dumps({'unit': unit, 'operation': 'health', 'status': 'inflight'}).encode())
                with self.assertRaises(ValueError):
                    adapter.retire_journal(store, unit, os.getuid())
                self.assertTrue(journal.exists())
                adapter.durable_file(status, json.dumps({'unit': unit, 'operation': 'health', 'status': 'completed', 'exit_code': 0}).encode())
                adapter.retire_journal(store, unit, os.getuid())
                self.assertFalse(journal.exists())

    def test_artifact_cleanup_is_owned_unforced_and_protected(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'CI_FLEET_DEPLOYER_TESTING': '1'}):
            ledger = Path(directory) / 'artifacts.json'
            old = self.policy.image.replace('a' * 64, 'b' * 64)
            retained = self.policy.image.replace('a' * 64, 'c' * 64)
            adapter.durable_file(ledger, json.dumps({old: self.policy.labels, retained: self.policy.labels}).encode())
            policy = adapter.Policy(self.values, self.backend, operation='cleanup', artifact_store=ledger, protected_images={retained})
            path, _ = policy.authorize('DELETE', '/images/' + old, b'')
            self.assertEqual(path, '/images/' + old + '?force=false&noprune=true')
            for image in (self.policy.image, retained, old.replace('b' * 64, 'd' * 64)):
                with self.assertRaises(ValueError):
                    policy.authorize('DELETE', '/images/' + image, b'')
            with self.assertRaises(ValueError):
                policy.authorize('DELETE', '/images/' + old + '?force=1', b'')

    def test_artifact_inventory_hides_unrelated_images(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'CI_FLEET_DEPLOYER_TESTING': '1'}):
            ledger = Path(directory) / 'artifacts.json'
            adapter.durable_file(ledger, json.dumps({self.policy.image: self.policy.labels}).encode())
            policy = adapter.Policy(self.values, self.backend, artifact_store=ledger)
            self.backend.request = lambda *_args: (200, json.dumps([
                {'Id':'owned', 'RepoDigests':[self.policy.image], 'RepoTags':['unrelated-alias']},
                {'Id':'foreign', 'RepoDigests':['registry.example.invalid/other@sha256:'+'b'*64]}]).encode())
            path = str(Path(directory) / 'broker.sock')
            with adapter.ProxyServer(path, adapter.handler(policy)) as server:
                thread = threading.Thread(target=server.serve_forever)
                thread.start()
                try:
                    connection = adapter.UnixConnection(path, rootless=False)
                    connection.request('GET', '/v1.47/images/json')
                    response = connection.getresponse()
                    inventory = json.loads(response.read())
                    connection.close()
                finally:
                    server.shutdown()
                    thread.join()
            self.assertEqual([item['Id'] for item in inventory], ['owned'])
            self.assertEqual(inventory[0]['RepoTags'], [])

    def test_credential_store_rejects_inherited_acl_before_exposing_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Path(directory)
            with patch.object(adapter.os, 'getxattr', return_value=b'default-named-user-acl'):
                with self.assertRaises(ValueError):
                    adapter.materialize_credential(store, self.values, b'secret-canary',
                                                   os.getuid(), os.getuid(), os.getgid())
            self.assertEqual(list(store.iterdir()), [])

    def test_consumption_recovers_marker_audit_and_partial_write_crashes(self):
        line = ('time=2026-10-13T00:00:00Z environment=staging target=app source=' + 'a' * 40
                + ' artifact=sha256:' + 'b' * 64
                + ' approval=approval-1 approver=reviewer policy=policy checkpoint=checkpoint authorized_by=none gate=none result=consumed phase=pre-adapter status=none\n')
        for boundary in ('marker', 'partial-audit', 'retirement'):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as directory:
                store = Path(directory)
                ledger = store / 'consumed-requests'
                ledger.mkdir(mode=0o700)
                audit = store / 'audit.log'
                adapter.durable_file(audit, b'prior history\n')
                real_durable, real_write, real_unlink = adapter.durable_file, os.write, Path.unlink
                def durable(path, body):
                    if boundary == 'marker' and path.parent == ledger:
                        raise OSError('simulated power loss before marker publication')
                    real_durable(path, body)
                writes = []
                def write(descriptor, body):
                    if boundary == 'partial-audit':
                        if writes:
                            raise OSError('simulated power loss during audit publication')
                        writes.append(True)
                        return real_write(descriptor, body[:len(body) // 2])
                    return real_write(descriptor, body)
                def unlink(path, *args, **kwargs):
                    if boundary == 'retirement' and path.name == 'pending-consumption.json':
                        raise OSError('simulated power loss after audit publication')
                    return real_unlink(path, *args, **kwargs)
                with patch.object(adapter, 'durable_file', side_effect=durable), \
                     patch.object(adapter.os, 'write', side_effect=write), \
                     patch.object(Path, 'unlink', unlink):
                    with self.assertRaises(OSError):
                        adapter.consume_approval(store, line, os.getuid())
                self.assertTrue((store / 'pending-consumption.json').exists())
                adapter.consume_approval(store, uid=os.getuid())
                self.assertEqual(audit.read_bytes(), b'prior history\n' + line.encode())
                self.assertEqual(len(list(ledger.iterdir())), 1)
                self.assertEqual(next(ledger.iterdir()).read_bytes(), b'')
                self.assertFalse((store / 'pending-consumption.json').exists())
                adapter.consume_approval(store, uid=os.getuid())
                self.assertEqual(audit.read_bytes().count(line.encode()), 1)

    def test_installer_check_requires_root(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([str(Path(__file__).with_name('install-deployer.sh')),
                                     '--check', '--config', directory + '/absent.conf'],
                                    env=dict(os.environ, CI_FLEET_DEPLOYER_TESTING='1',
                                             CI_FLEET_DEPLOYER_ROOT=directory,
                                             CI_FLEET_DEPLOYER_EUID_OVERRIDE='1000'),
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('run this mode as root', result.stderr)

    def test_rollback_success_audit_is_idempotent_after_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            audit = Path(directory) / 'audit.log'
            fields = {'transaction': '.transaction.first', 'action': 'rollback', 'result': 'success'}
            adapter.append_audit_record(audit, dict(fields, time='initial'), os.getuid(), once=True)
            adapter.append_audit_record(audit, dict(fields, time='recovered'), os.getuid(), once=True)
            adapter.append_audit_record(audit, dict(fields, transaction='.transaction.second'), os.getuid(), once=True)
            self.assertEqual(len(audit.read_text().splitlines()), 2)
            self.assertNotIn('time=recovered', audit.read_text())

    def test_rollback_recovery_requires_durable_completion_after_reboot(self):
        installer = Path(__file__).with_name('install-deployer.sh')
        functions = ''.join(subprocess.check_output(['sed', '-n', f'/^{name}() {{$/,/^}}$/p',
                                                     str(installer)], text=True)
                            for name in ('acquire_lock', 'recover_adapter_guard',
                                         'recover_interrupted_transaction', 'retire_adapter_guard'))
        shell = functions + r'''
block() { printf '%s\n' "$*" >&2; exit 42; }
secure_file() { test -f "$1"; }
secure_directory() { if [[ ${3:-0} == 1 ]]; then mkdir -p "$1"; chmod "$2" "$1"; fi; test -d "$1"; }
root_path() { printf '%s%s' "$fixture_root" "$1"; }
reject_other_role_state() { return 0; }
adapter_unit_inactive() { return 0; }
rollback_commit_marker_uncommitted() { test ! -e "$1"; }
rollback_commit_marker_safe() { test -f "$1"; }
restore_transaction() { printf restored >"$result_file"; rm -rf "$transaction_dir"; transaction_dir=; }
finalize_committed_rollback() { printf finalized >"$result_file"; rm -rf "$transaction_dir"; transaction_dir=; }
python3() { command "$test_python" -c "$journal_probe" "$@"; }
acquire_lock
'''
        # Directory ancestry is covered separately; this fixture lives in /tmp.
        probe = "import sys; from unittest.mock import patch; import deployer_adapter as a; sys.argv=sys.argv[1:]; " \
                "patch.object(a,'trusted_directory').start(); patch.object(a,'unit_inactive',return_value=True).start(); " \
                "raise SystemExit(a.main())"
        for committed, volatile in ((False, False), (False, True), (True, False), (True, True)):
            cases = [('missing', False, None), ('inflight', False, None),
                     ('completed', True, 'committed' if committed else 'failed'),
                     ('completed', False, None), ('completed', False, 'committed' if committed else 'failed')]
            if committed:
                cases.append(('health-inflight', False, None))
            for state, uncertain, outcome in cases:
                with self.subTest(committed=committed, volatile=volatile, state=state,
                                  uncertain=uncertain, outcome=outcome), \
                        tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    transactions, store = root / 'transactions', root / 'state'
                    transactions.mkdir()
                    store.mkdir()
                    lock, logs = root / 'locks', root / 'logs'
                    lock.mkdir(mode=0o700)
                    logs.mkdir(mode=0o700)
                    transaction = transactions / '.transaction.rollback'
                    transaction.mkdir(mode=0o700)
                    unit = 'ci-fleet-deployer-adapter-1-2.service'
                    adapter.durable_file(transaction / 'kind', b'rollback\n')
                    adapter.durable_file(transaction / 'adapter-unit', (unit + '\n').encode())
                    if state == 'health-inflight':
                        adapter.durable_file(transaction / 'rollback-health-unit', (unit + '\n').encode())
                    if volatile:
                        adapter.durable_file(lock / 'adapter-unit', (unit + '\n').encode())
                    if committed:
                        adapter.durable_file(transaction / 'application-rollback-committed', b'committed\n')
                    if state != 'missing':
                        journal = store / '.adapter-recovery' / unit
                        journal.mkdir(parents=True, mode=0o700)
                        adapter.durable_file(journal / 'status.json', json.dumps({
                            'unit': unit, 'operation': 'health' if state == 'health-inflight' else 'rollback',
                            'status': 'inflight' if state == 'health-inflight' else state,
                            'exit_code': 1, 'rollback_outcome': outcome,
                            'rollback_marker': str(transaction / 'application-rollback-committed')}).encode())
                        if uncertain:
                            adapter.durable_file(journal / 'docker-uncertain', b'uncertain\n')
                    result_file = root / 'result'
                    result = subprocess.run(['bash', '-c', shell], capture_output=True, text=True,
                                            env=dict(os.environ, transaction_root=str(transactions),
                                                     state_root=str(store), expected_uid=str(os.getuid()),
                                                     repo_root=str(installer.parent.parent),
                                                     fixture_root=str(root), install_root=str(root),
                                                     lock_root=str(lock), log_root=str(logs), mode='repair',
                                                     adapter_guard=str(lock / 'adapter-unit'), testing='0',
                                                     active_operation=str(store / 'active-operation'),
                                                     releases=str(root / 'absent-releases'),
                                                     CI_FLEET_DEPLOYER_TESTING='1', result_file=str(result_file),
                                                     test_python=sys.executable, journal_probe=probe,
                                                     PYTHONPATH=str(installer.parent)))
                    if state != 'health-inflight' and (state != 'completed' or uncertain or outcome is None):
                        self.assertEqual(result.returncode, 42, result.stderr)
                        self.assertIn('rollback adapter journal completion is unverified', result.stderr)
                        self.assertFalse(result_file.exists())
                        self.assertTrue(transaction.exists())
                    else:
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(result_file.read_text(), 'finalized' if committed else 'restored')
                        self.assertFalse((lock / 'adapter-unit').exists())
                        self.assertFalse((store / '.adapter-recovery' / unit).exists())

    def test_failed_rollback_commit_publication_keeps_uncertainty(self):
        for published in (False, True):
            with self.subTest(published=published), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                marker, uncertain = root / 'rollback-commit', root / 'docker-uncertain'
                policy = adapter.Policy(self.values, self.backend, marker=marker, operation='rollback',
                                        uncertain_marker=uncertain)
                durable = adapter.durable_file
                def failing_publication(path, body):
                    if path == marker:
                        if published:
                            durable(path, body)
                        raise OSError('commit publication failed')
                    durable(path, body)
                with adapter.ProxyServer(str(root / 'broker.sock'), adapter.handler(policy)) as server:
                    thread = threading.Thread(target=server.serve_forever)
                    thread.start()
                    try:
                        with patch.object(adapter, 'durable_file', side_effect=failing_publication), \
                                patch.object(adapter, 'executor_peer', return_value=True):
                            connection = adapter.UnixConnection(str(root / 'broker.sock'), rootless=False)
                            connection.request('POST', '/ci-fleet/rollback-commit', b'committed')
                            response = connection.getresponse()
                            self.assertEqual(response.status, 403)
                            response.read()
                            connection.close()
                    finally:
                        server.shutdown()
                        thread.join()
                self.assertTrue(policy.uncertain)
                self.assertEqual(uncertain.read_bytes(), b'uncertain\n')

    def test_rollback_completion_requires_executor_outcome_confirmation(self):
        for code, outcome in [(0, None), (1, None), (1, 'failed'), (0, 'committed'), (1, 'committed')]:
            with self.subTest(code=code, outcome=outcome), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                store, runtime, credentials = root / '.adapter-recovery', root / 'runtime', root / 'credentials'
                store.mkdir(mode=0o700)
                runtime.mkdir(mode=0o711)
                credentials.mkdir(mode=0o711)
                source, secret, policy_path = root / 'adapter', root / 'application', root / 'policy'
                adapter.durable_file(source, b'#!/bin/sh\nexit 0\n')
                source.chmod(0o700)
                adapter.durable_file(secret, b'fixture-only')
                values = dict(self.values, CREDENTIAL_PROVIDER='file', CREDENTIAL_REF=str(secret),
                              ADAPTER_SHA256=adapter.hashlib.sha256(source.read_bytes()).hexdigest(),
                              DOCKER_SOCKET='/run/rootless/docker.sock')
                adapter.durable_file(policy_path, ''.join(f'{k}={v}\n' for k, v in values.items()).encode())
                unit = 'ci-fleet-deployer-adapter-1-2.service'
                args = SimpleNamespace(policy=policy_path, adapter=source, store=store, runtime=runtime,
                                       credential_store=credentials, unit=unit, seconds=120,
                                       request=None, marker=root / 'commit', operation='rollback')
                backend = SimpleNamespace(uid=os.getuid(), gid=os.getgid(), path='/run/rootless/docker.sock')
                def dispatch(command, **_kwargs):
                    if command[0] == 'systemd-run':
                        if outcome:
                            bind = next(item for item in command if item.startswith('--property=BindReadOnlyPaths='))
                            proxy = bind.split('=', 2)[2].split(':', 1)[0]
                            connection = adapter.UnixConnection(proxy, rootless=False)
                            connection.request('POST', '/ci-fleet/rollback-commit', outcome.encode())
                            response = connection.getresponse()
                            self.assertEqual(response.status, 204)
                            response.read()
                            connection.close()
                        return subprocess.CompletedProcess(command, code)
                    if '--property=MainPID' in command:
                        return subprocess.CompletedProcess(command, 0, str(os.getpid()))
                    return subprocess.CompletedProcess(command, 0, 'LoadState=loaded\nActiveState=inactive\n')
                with patch.dict(os.environ, {'CI_FLEET_DEPLOYER_TESTING': '1'}), \
                        patch.object(adapter, 'Backend', return_value=backend), \
                        patch.object(adapter, 'trusted_directory'), \
                        patch.object(adapter.subprocess, 'run', side_effect=dispatch):
                    if outcome is None:
                        with self.assertRaises(ValueError):
                            adapter.run_adapter(args)
                    else:
                        self.assertEqual(adapter.run_adapter(args), code)
                    status = json.loads((store / unit / 'status.json').read_bytes())
                    self.assertEqual(status['status'], 'completed' if outcome else 'inflight')
                    self.assertEqual(adapter.journal_complete(store, unit, os.getuid()), bool(outcome))
                    if outcome:
                        self.assertEqual(status['rollback_outcome'], outcome)

    def test_executor_confirms_rollback_outcome(self):
        for code, committed in [(42, False), (0, True), (1, True), (0, False), (-9, False), (-9, True)]:
            with self.subTest(code=code, committed=committed), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                credentials, runtime = root / 'credentials', root / 'runtime'
                credentials.mkdir()
                runtime.mkdir()
                adapter.durable_file(credentials / 'policy', b'CREDENTIAL_REF=fixture\n')
                adapter.durable_file(credentials / 'adapter', b'#!/bin/sh\nexit 0\n')
                def application(command, **kwargs):
                    if committed:
                        Path(kwargs['env']['CI_FLEET_DEPLOYER_ROLLBACK_COMMIT']).write_bytes(b'committed\n')
                    return subprocess.CompletedProcess(command, code)
                connection = MagicMock()
                connection.getresponse.return_value.status = 204
                with patch.dict(os.environ, {'CREDENTIALS_DIRECTORY': str(credentials),
                                             'RUNTIME_DIRECTORY': str(runtime),
                                             'CI_FLEET_DEPLOYER_CONTAINER_CREDENTIAL': '/approved/application',
                                             'CI_FLEET_DEPLOYER_COMMIT_ALLOWED': '1'}), \
                        patch.object(adapter.subprocess, 'run', side_effect=application), \
                        patch.object(adapter.ctypes, 'CDLL') as library, \
                        patch.object(adapter, 'UnixConnection', return_value=connection):
                    library.return_value.prctl.return_value = 0
                    if committed and code != 0 or not committed and code <= 0:
                        with self.assertRaises(ValueError):
                            adapter.execute_adapter('rollback')
                        connection.request.assert_not_called()
                    else:
                        self.assertEqual(adapter.execute_adapter('rollback'), code)
                        connection.request.assert_called_once_with('POST', '/ci-fleet/rollback-commit',
                                                                  b'committed' if committed else b'failed')

    def test_adapter_cannot_publish_executor_outcome(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / 'commit'
            policy = adapter.Policy(self.values, self.backend, marker=marker, operation='rollback',
                                    unit='ci-fleet-deployer-adapter-1-2.service')
            path = str(root / 'broker.sock')
            child = "import sys,http.client,socket; s=socket.socket(socket.AF_UNIX); s.connect(sys.argv[1]); " \
                    "c=http.client.HTTPConnection('localhost'); c.sock=s; " \
                    "c.request('POST','/ci-fleet/rollback-commit',b'committed'); r=c.getresponse(); " \
                    "print(r.status); r.read(); c.close()"
            with adapter.ProxyServer(path, adapter.handler(policy)) as server, \
                    patch.object(adapter.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, str(os.getpid()))):
                thread = threading.Thread(target=server.serve_forever)
                thread.start()
                try:
                    process = subprocess.Popen([sys.executable, '-c', child, path], stdout=subprocess.PIPE, text=True)
                    output, _ = process.communicate(timeout=10)
                    self.assertEqual(process.returncode, 0)
                    self.assertEqual(output.strip(), '403')
                    self.assertFalse(marker.exists())
                    self.assertIsNone(policy.rollback_outcome)
                    connection = adapter.UnixConnection(path, rootless=False)
                    connection.request('POST', '/ci-fleet/rollback-commit', b'failed')
                    response = connection.getresponse()
                    self.assertEqual(response.status, 204)
                    response.read()
                    connection.close()
                    self.assertEqual(policy.rollback_outcome, 'failed')
                finally:
                    server.shutdown()
                    thread.join()

    def test_executor_peer_requires_current_main_pid(self):
        connection = MagicMock()
        connection.getsockopt.return_value = struct.pack('3i', os.getpid(), os.getuid(), os.getgid())
        for code, value, expected in [(0, str(os.getpid()), True), (0, '0', False),
                                      (0, str(os.getpid() + 1), False), (1, str(os.getpid()), False),
                                      (0, '', False), (0, 'unavailable', False)]:
            with self.subTest(code=code, value=value), \
                    patch.object(adapter.subprocess, 'run', return_value=subprocess.CompletedProcess([], code, value)):
                self.assertEqual(adapter.executor_peer(connection, 'ci-fleet-deployer-adapter-1-2.service'), expected)
        with patch.object(adapter.subprocess, 'run', side_effect=subprocess.TimeoutExpired('systemctl', 10)):
            self.assertFalse(adapter.executor_peer(connection, 'ci-fleet-deployer-adapter-1-2.service'))

    def test_executor_disables_dumping_before_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            credentials, runtime = root / 'credentials', root / 'runtime'
            credentials.mkdir()
            runtime.mkdir()
            adapter.durable_file(credentials / 'policy', b'CREDENTIAL_REF=fixture\n')
            adapter.durable_file(credentials / 'adapter', b'#!/bin/sh\nexit 0\n')
            code = '''import ctypes,subprocess
from unittest.mock import patch
import deployer_adapter as a
def application(command, **kwargs):
    assert ctypes.CDLL(None).prctl(3,0,0,0,0) == 0
    assert kwargs['close_fds'] is True
    return subprocess.CompletedProcess(command,0)
with patch.object(a.subprocess,'run',side_effect=application):
    assert a.execute_adapter('health') == 0
'''
            result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True,
                                    env=dict(os.environ, CREDENTIALS_DIRECTORY=str(credentials),
                                             RUNTIME_DIRECTORY=str(runtime), CI_FLEET_DEPLOYER_COMMIT_ALLOWED='0',
                                             CI_FLEET_DEPLOYER_CONTAINER_CREDENTIAL='/approved/application',
                                             PYTHONPATH=str(Path(__file__).parent)))
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_guard_requires_completed_protected_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Path(directory)
            unit = 'ci-fleet-deployer-adapter-1-2.service'
            journal = store / unit
            journal.mkdir(mode=0o700)
            status = journal / 'status.json'
            marker = store / 'rollback-commit'
            with patch.object(adapter, 'trusted_directory'):
                for operation, state, code, expected in [('rollback', 'inflight', 0, False),
                                                        ('rollback', 'completed', 1, True),
                                                        ('rollback', 'completed', 0, True),
                                                        ('health', 'completed', 1, True),
                                                        ('cleanup', 'completed', 1, False)]:
                    if code == 0:
                        adapter.durable_file(marker, b'committed\n')
                    else:
                        marker.unlink(missing_ok=True)
                    adapter.durable_file(status, json.dumps({'unit': unit, 'operation': operation,
                                                            'status': state, 'exit_code': code,
                                                            'rollback_marker': str(marker),
                                                            'rollback_outcome': 'committed' if code == 0 else 'failed'}).encode())
                    self.assertEqual(adapter.journal_complete(store, unit, os.getuid()), expected)
                for code in (0, 1):
                    adapter.durable_file(status, json.dumps({'unit': unit, 'operation': 'rollback',
                                                            'status': 'completed', 'exit_code': code}).encode())
                    self.assertFalse(adapter.journal_complete(store, unit, os.getuid()))
                adapter.durable_file(status, json.dumps({'unit': unit, 'operation': 'rollback', 'status': 'completed',
                                                        'exit_code': 0, 'rollback_marker': str(marker),
                                                        'rollback_outcome': 'committed'}).encode())
                self.assertFalse(adapter.journal_complete(store, unit, os.getuid()))
                adapter.durable_file(marker, b'committed\n')
                self.assertTrue(adapter.journal_complete(store, unit, os.getuid()))
                marker.chmod(0o700)
                self.assertFalse(adapter.journal_complete(store, unit, os.getuid()))
                marker.chmod(0o600)
                adapter.durable_file(status, json.dumps({'unit': unit, 'operation': 'rollback', 'status': 'completed',
                                                        'exit_code': 1, 'rollback_marker': str(marker),
                                                        'rollback_outcome': 'failed'}).encode())
                self.assertFalse(adapter.journal_complete(store, unit, os.getuid()))
                marker.unlink()
                adapter.durable_file(status, json.dumps({'unit': unit, 'operation': 'deploy', 'status': 'completed', 'exit_code': 0}).encode())
                adapter.durable_file(journal / 'docker-uncertain', b'uncertain')
                self.assertFalse(adapter.journal_complete(store, unit, os.getuid()))
                adapter.durable_file(status, json.dumps({'unit': unit, 'operation': 'rollback', 'status': 'completed',
                                                        'exit_code': 1, 'rollback_marker': str(marker),
                                                        'rollback_outcome': 'failed'}).encode())
                self.assertFalse(adapter.journal_complete(store, unit, os.getuid()))

    def test_failed_forwarded_mutation_keeps_durable_uncertainty(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'docker-uncertain'
            policy = adapter.Policy(self.values, self.backend, uncertain_marker=marker)
            self.backend.request = lambda *_args: (_ for _ in ()).throw(OSError('daemon disconnected'))
            path = str(Path(directory) / 'broker.sock')
            with adapter.ProxyServer(path, adapter.handler(policy)) as server:
                thread = threading.Thread(target=server.serve_forever)
                thread.start()
                try:
                    connection = adapter.UnixConnection(path, rootless=False)
                    connection.request('POST', '/networks/create', json.dumps({'Name': self.network, 'Internal': True, 'Labels': policy.labels}).encode())
                    response = connection.getresponse()
                    self.assertEqual(response.status, 403)
                    response.read()
                    connection.close()
                finally:
                    server.shutdown()
                    thread.join()
            self.assertTrue(policy.uncertain)
            self.assertEqual(marker.read_bytes(), b'uncertain\n')

    def test_persistent_credential_survives_runtime_removal_and_rotation(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Path(directory) / 'credential-store'
            store.mkdir(mode=0o711)
            first = adapter.materialize_credential(store, self.values, b'fixture-one', os.getuid(), os.getuid(), os.getgid())
            second = adapter.materialize_credential(store, self.values, b'fixture-one', os.getuid(), os.getuid(), os.getgid())
            self.assertEqual(first, second)
            self.assertEqual(first.read_bytes(), b'fixture-one')
            self.assertEqual(stat.S_IMODE(first.stat().st_mode), 0o440)
            self.assertEqual(first.stat().st_uid, os.getuid())
            self.assertEqual(first.stat().st_gid, os.getgid())
            rotated = adapter.materialize_credential(store, self.values, b'fixture-two', os.getuid(), os.getuid(), os.getgid())
            self.assertNotEqual(first, rotated)
            self.assertEqual(first.read_bytes(), b'fixture-one')
            self.assertEqual(rotated.read_bytes(), b'fixture-two')
            first.chmod(0o600)
            with self.assertRaises(ValueError):
                adapter.materialize_credential(store, self.values, b'fixture-one', os.getuid(), os.getuid(), os.getgid())

    def test_unsafe_systemd_path_is_rejected(self):
        with self.assertRaises(ValueError):
            adapter.sandbox_command('ci-fleet-deployer-adapter-1-2.service', 120, Path('/run/proxy'),
                                    Path('/policy%h'), Path('/adapter'), Path('/credential'), Path('/helper'),
                                    None, False, 'health', '/run/rootless/socket', '/credential-copy')

    def test_duplicate_json(self):
        with self.assertRaises(ValueError):
            adapter.decode(b'{"Privileged":false,"Privileged":true}')

    def test_rootful_workload_is_not_hidden_by_rootless_backend(self):
        from unittest.mock import MagicMock
        connection = MagicMock()
        response = connection.getresponse.return_value
        response.status = 200
        response.read.return_value = b'[{"Id":"unrelated"}]'
        with patch.object(adapter.Path, 'exists', return_value=True), \
             patch.object(adapter.Path, 'resolve', return_value=Path('/run/docker.sock')), \
             patch.object(adapter, 'UnixConnection', return_value=connection):
            with self.assertRaises(ValueError):
                adapter.reject_rootful_workloads('/run/rootless/docker.sock')
        connection.close.assert_called_once()

    def test_rootful_inventory_has_one_startup_deadline(self):
        connection = MagicMock()
        with patch.object(adapter.Path, 'exists', return_value=True), \
             patch.object(adapter.Path, 'resolve', return_value=Path('/run/docker.sock')), \
             patch.object(adapter, 'UnixConnection', return_value=connection), \
             patch.object(adapter.time, 'monotonic', return_value=31):
            with self.assertRaises(ValueError):
                adapter.reject_rootful_workloads('/run/rootless/docker.sock', deadline=30)
        connection.request.assert_not_called()
        connection.close.assert_called_once()

    def test_confirmed_image_uses_docker_inspect_endpoint(self):
        backend = adapter.Backend.__new__(adapter.Backend)
        backend.request = MagicMock(return_value=(200, json.dumps({'RepoDigests':[self.policy.image]}).encode()))
        self.assertEqual(backend.inspect('images', self.policy.image)['RepoDigests'], [self.policy.image])
        path = '/images/' + adapter.urllib.parse.quote(self.policy.image, safe='') + '/json'
        backend.request.assert_called_once_with('GET', path)

    def test_rootful_socket_peer_rejected(self):
        if os.getuid() != 0:
            self.skipTest('root peer fixture requires root test user')
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'docker.sock')
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(path)
            server.listen()
            client = adapter.UnixConnection(path)
            try:
                with self.assertRaises(ValueError):
                    client.connect()
            finally:
                client.close()
                server.close()

    def test_proxy_rejects_malformed_and_preserves_backend(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'broker.sock')
            with adapter.ProxyServer(path, adapter.handler(self.policy)) as server:
                thread = threading.Thread(target=server.serve_forever)
                thread.start()
                try:
                    for body in (b'{"Labels":null}', b'{"Labels":[]}'):
                        connection = adapter.UnixConnection(path, rootless=False)
                        connection.request('POST', '/networks/create', body)
                        response = connection.getresponse()
                        self.assertEqual(response.status, 403)
                        response.read()
                        connection.close()
                    self.assertFalse(self.backend.calls)
                finally:
                    server.shutdown()
                    thread.join()


class RollbackExclusiveStop(unittest.TestCase):
    def setUp(self):
        self.base_values = {
            'ARTIFACT_IMAGE': 'registry.example.invalid/app@sha256:' + 'a' * 64,
            'DEPLOYER_IDENTITY': 'example',
            'ENVIRONMENT': 'staging',
            'TARGET_ID': 'app',
            'SOURCE_COMMIT': '1' * 40,
            'DOCKER_SOCKET': '/run/rootless/docker.sock',
            'CREDENTIAL_PROVIDER': 'file',
            'CREDENTIAL_REF': '/etc/ci-fleet-deployer/credentials/application.credential',
            'CREDENTIAL_SCOPE': 'staging',
            'ADAPTER_PATH': '/etc/ci-fleet-deployer/adapters/app',
            'ADAPTER_SHA256': '2' * 64,
        }
        self.labels = adapter.ownership_labels(self.base_values)

    def _write_policy(self, path: Path, overrides: dict[str, str] | None = None) -> Path:
        values = dict(self.base_values)
        if overrides:
            values.update(overrides)
        adapter.durable_file(path, ''.join(f'{k}={v}\n' for k, v in values.items()).encode())
        return path

    def test_parse_rollback_stop_policy_validation_and_merge(self):
        parsed = adapter.parse_rollback_stop_policy(
            dict(self.base_values, ROLLBACK_EXCLUSIVE_SERVICES='scheduler', ROLLBACK_STOP_TIMEOUT_SECONDS='90', ROLLBACK_STOP_SIGNAL='SIGQUIT'),
            dict(self.base_values, ROLLBACK_EXCLUSIVE_SERVICES='worker,scheduler', ROLLBACK_STOP_TIMEOUT_SECONDS='45', ROLLBACK_STOP_SIGNAL='SIGTERM'),
        )
        self.assertEqual(parsed, {'services': ['worker', 'scheduler'], 'timeout_seconds': 45, 'signal': 'SIGTERM'})
        defaults = adapter.parse_rollback_stop_policy(dict(self.base_values, ROLLBACK_EXCLUSIVE_SERVICES='scheduler'))
        self.assertEqual(defaults, {'services': ['scheduler'], 'timeout_seconds': 120, 'signal': None})
        empty = adapter.parse_rollback_stop_policy(self.base_values)
        self.assertEqual(empty, {'services': [], 'timeout_seconds': 0, 'signal': None})

        invalid_cases = [
            {'ROLLBACK_EXCLUSIVE_SERVICES': ''},
            {'ROLLBACK_EXCLUSIVE_SERVICES': 'scheduler,'},
            {'ROLLBACK_EXCLUSIVE_SERVICES': 'BadName'},
            {'ROLLBACK_EXCLUSIVE_SERVICES': 'scheduler,scheduler'},
            {'ROLLBACK_EXCLUSIVE_SERVICES': ','.join(f'svc-{i}' for i in range(33))},
            {'ROLLBACK_EXCLUSIVE_SERVICES': 'a' * 65},
            {'ROLLBACK_EXCLUSIVE_SERVICES': 'a' * 64, 'DEPLOYER_IDENTITY': 'x' * 55},
            {'ROLLBACK_STOP_TIMEOUT_SECONDS': '30'},
            {'ROLLBACK_STOP_SIGNAL': 'SIGTERM'},
            {'ROLLBACK_EXCLUSIVE_SERVICES': 'scheduler', 'ROLLBACK_STOP_TIMEOUT_SECONDS': '0'},
            {'ROLLBACK_EXCLUSIVE_SERVICES': 'scheduler', 'ROLLBACK_STOP_TIMEOUT_SECONDS': '601'},
            {'ROLLBACK_EXCLUSIVE_SERVICES': 'scheduler', 'ROLLBACK_STOP_TIMEOUT_SECONDS': 'abc'},
            {'ROLLBACK_EXCLUSIVE_SERVICES': 'a,b,c,d', 'ROLLBACK_STOP_TIMEOUT_SECONDS': '600'},
            {'ROLLBACK_EXCLUSIVE_SERVICES': 'scheduler', 'ROLLBACK_STOP_SIGNAL': 'SIGKILL'},
            {'ROLLBACK_EXCLUSIVE_SERVICES': 'scheduler', 'ROLLBACK_STOP_SIGNAL': '9'},
            {'ROLLBACK_EXCLUSIVE_SERVICES': 'scheduler', 'ROLLBACK_STOP_SIGNAL': 'TERM'},
        ]
        for bad in invalid_cases:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                adapter.parse_rollback_stop_policy(dict(self.base_values, **bad))

        with self.assertRaises(ValueError):
            adapter.parse_rollback_stop_policy(
                dict(self.base_values, ROLLBACK_EXCLUSIVE_SERVICES='scheduler'),
                dict(self.base_values, TARGET_ID='other', ROLLBACK_EXCLUSIVE_SERVICES='scheduler'),
            )
        with self.assertRaises(ValueError):
            adapter.parse_rollback_stop_policy(
                dict(self.base_values, ROLLBACK_EXCLUSIVE_SERVICES='scheduler'),
                dict(self.base_values, DOCKER_SOCKET='/run/other/docker.sock', ROLLBACK_EXCLUSIVE_SERVICES='scheduler'),
            )
        with self.assertRaises(ValueError):
            adapter.parse_rollback_stop_policy(
                dict(self.base_values, ROLLBACK_EXCLUSIVE_SERVICES='a,b', ROLLBACK_STOP_TIMEOUT_SECONDS='500'),
                dict(self.base_values, ROLLBACK_EXCLUSIVE_SERVICES='c,d'),
            )

    def _fake_clock(self):
        now = [0.0]
        def mono():
            return now[0]
        def slp(dt):
            now[0] += dt
        return mono, slp

    def _make_docker_mock(self, containers: dict[str, dict], *, on_kill=None, on_update=None):
        calls = []
        class MockDocker:
            def inspect(self, kind, name):
                assert kind == 'containers'
                if name not in containers:
                    raise ValueError('not found')
                return copy.deepcopy(containers[name])

            def request(self, method, path, body=b'', headers=None):
                calls.append((method, path, body))
                parsed = adapter.urllib.parse.urlsplit(path)
                if method == 'GET' and parsed.path == '/containers/json':
                    items = []
                    for cid, info in containers.items():
                        items.append({
                            'Id': cid,
                            'Names': [info['Name']],
                            'Labels': dict(info['Config']['Labels']),
                            'State': info['State']['Status'],
                        })
                    return 200, json.dumps(items).encode()
                parts = parsed.path.strip('/').split('/')
                if len(parts) >= 2 and parts[0] == 'containers':
                    cid = adapter.urllib.parse.unquote(parts[1])
                    action = parts[2] if len(parts) == 3 else 'json'
                    if cid not in containers:
                        return 404, b'{"message":"no such container"}'
                    if method == 'GET' and action == 'json':
                        return 200, json.dumps(containers[cid]).encode()
                    if method == 'POST' and action == 'update':
                        if on_update:
                            res = on_update(cid, json.loads(body))
                            if res is not None:
                                return res
                        containers[cid]['HostConfig']['RestartPolicy'] = json.loads(body)['RestartPolicy']
                        return 200, b'{}'
                    if method == 'POST' and action == 'kill':
                        sig = adapter.urllib.parse.parse_qs(parsed.query).get('signal', ['SIGTERM'])[0]
                        if on_kill:
                            res = on_kill(cid, sig)
                            if res is not None:
                                return res
                        return 204, b''
                return 500, b'{}'
        return MockDocker(), calls

    def test_stop_exclusive_resolution_and_ambiguity_fails_pre_signal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lkg = self._write_policy(root / 'lkg.conf', {'ROLLBACK_EXCLUSIVE_SERVICES': 'scheduler'})
            # Two containers matching the same exclusive service fails pre-signal.
            containers = {
                'cid1': {'Id': 'cid1', 'Name': '/ci-fleet-deploy-example-scheduler',
                         'Config': {'Labels': self.labels, 'Image': self.base_values['ARTIFACT_IMAGE']},
                         'HostConfig': {'RestartPolicy': {'Name': 'always'}},
                         'State': {'Status': 'running', 'Running': True, 'Restarting': False, 'Paused': False, 'Pid': 10}},
                'cid2': {'Id': 'cid2', 'Name': '/ci-fleet-deploy-example-scheduler-alt',
                         'Config': {'Labels': dict(self.labels, **{adapter.SERVICE_LABEL: 'scheduler'}), 'Image': self.base_values['ARTIFACT_IMAGE']},
                         'HostConfig': {'RestartPolicy': {'Name': 'always'}},
                         'State': {'Status': 'running', 'Running': True, 'Restarting': False, 'Paused': False, 'Pid': 11}},
            }
            backend, calls = self._make_docker_mock(containers)
            journal = root / 'exclusive-stop.json'
            mono, slp = self._fake_clock()
            rc = adapter.stop_exclusive_services(lkg, journal, uid=os.getuid(), backend_factory=lambda *_: backend, monotonic=mono, sleep=slp)
            self.assertEqual(rc, 3)
            self.assertFalse(any(m == 'POST' for m, _, _ in calls))
            jdata = json.loads(journal.read_text())
            self.assertEqual((jdata['status'], jdata['signaled'], jdata['reason']), ('blocked', False, 'ambiguous_service_identity'))

            # Conflicting name vs service label fails pre-signal.
            containers = {
                'cid1': {'Id': 'cid1', 'Name': '/ci-fleet-deploy-example-scheduler',
                         'Config': {'Labels': dict(self.labels, **{adapter.SERVICE_LABEL: 'worker'}), 'Image': self.base_values['ARTIFACT_IMAGE']},
                         'HostConfig': {'RestartPolicy': {'Name': 'no'}},
                         'State': {'Status': 'running', 'Running': True, 'Restarting': False, 'Paused': False, 'Pid': 10}},
            }
            backend, calls = self._make_docker_mock(containers)
            journal2 = root / 'exclusive-stop2.json'
            rc = adapter.stop_exclusive_services(lkg, journal2, uid=os.getuid(), backend_factory=lambda *_: backend, monotonic=mono, sleep=slp)
            self.assertEqual(rc, 3)
            self.assertFalse(any(m == 'POST' for m, _, _ in calls))

            # Unsupported StopSignal or Paused container fails pre-signal.
            for bad_state, bad_sig in [({'Paused': True}, 'SIGTERM'), ({'Paused': False}, 'SIGKILL')]:
                containers = {
                    'cid1': {'Id': 'cid1', 'Name': '/ci-fleet-deploy-example-scheduler',
                             'Config': {'Labels': self.labels, 'Image': self.base_values['ARTIFACT_IMAGE'], 'StopSignal': bad_sig},
                             'HostConfig': {'RestartPolicy': {'Name': 'no'}},
                             'State': {'Status': 'running', 'Running': True, 'Restarting': False, 'Pid': 10, **bad_state}},
                }
                backend, calls = self._make_docker_mock(containers)
                journal3 = root / f'exclusive-{bad_sig}-{bad_state["Paused"]}.json'
                rc = adapter.stop_exclusive_services(lkg, journal3, uid=os.getuid(), backend_factory=lambda *_: backend, monotonic=mono, sleep=slp)
                self.assertEqual(rc, 3)
                self.assertFalse(any(m == 'POST' for m, _, _ in calls))

            # Absent exclusive container succeeds.
            backend, _ = self._make_docker_mock({})
            journal4 = root / 'exclusive-empty.json'
            rc = adapter.stop_exclusive_services(lkg, journal4, uid=os.getuid(), backend_factory=lambda *_: backend, monotonic=mono, sleep=slp)
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(journal4.read_text())['status'], 'verified')

    def test_stop_exclusive_graceful_order_already_stopped_and_signals(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lkg = self._write_policy(root / 'lkg.conf', {'ROLLBACK_EXCLUSIVE_SERVICES': 'scheduler', 'ROLLBACK_STOP_TIMEOUT_SECONDS': '5'})
            inc = self._write_policy(root / 'inc.conf', {'ROLLBACK_EXCLUSIVE_SERVICES': 'scheduler', 'ROLLBACK_STOP_TIMEOUT_SECONDS': '5'})
            containers = {
                'cid-sched': {'Id': 'cid-sched', 'Name': '/ci-fleet-deploy-example-scheduler',
                              'Config': {'Labels': self.labels, 'Image': self.base_values['ARTIFACT_IMAGE'], 'StopSignal': 'QUIT'},
                              'HostConfig': {'RestartPolicy': {'Name': 'always'}},
                              'State': {'Status': 'running', 'Running': True, 'Restarting': False, 'Paused': False, 'Pid': 42}},
                'cid-sibling': {'Id': 'cid-sibling', 'Name': '/ci-fleet-deploy-example-web',
                                'Config': {'Labels': self.labels, 'Image': self.base_values['ARTIFACT_IMAGE']},
                                'HostConfig': {'RestartPolicy': {'Name': 'always'}},
                                'State': {'Status': 'running', 'Running': True, 'Restarting': False, 'Paused': False, 'Pid': 43}},
            }
            events = []
            def on_update(cid, payload):
                events.append(('update', cid, payload['RestartPolicy']['Name']))
                return None
            def on_kill(cid, sig):
                events.append(('kill', cid, sig))
                containers[cid]['State'].update({'Status': 'exited', 'Running': False, 'Pid': 0})
                return None
            backend, calls = self._make_docker_mock(containers, on_kill=on_kill, on_update=on_update)
            journal = root / 'exclusive-stop.json'
            mono, slp = self._fake_clock()
            rc = adapter.stop_exclusive_services(lkg, journal, incumbent_policy_path=inc, uid=os.getuid(),
                                                 backend_factory=lambda *_: backend, monotonic=mono, sleep=slp)
            self.assertEqual(rc, 0)
            self.assertEqual(events, [('update', 'cid-sched', 'no'), ('kill', 'cid-sched', 'SIGQUIT')])
            self.assertFalse(any('/stop' in p for _, p, _ in calls))
            self.assertEqual(containers['cid-sibling']['HostConfig']['RestartPolicy']['Name'], 'always')
            self.assertTrue(containers['cid-sibling']['State']['Running'])
            jdata = json.loads(journal.read_text())
            self.assertEqual((jdata['status'], jdata['escalation']), ('verified', 'none'))

            # Already-exited container with RestartPolicy=always is updated to no.
            containers['cid-exited'] = {
                'Id': 'cid-exited', 'Name': '/ci-fleet-deploy-example-worker',
                'Config': {'Labels': dict(self.labels, **{adapter.SERVICE_LABEL: 'worker'}), 'Image': self.base_values['ARTIFACT_IMAGE']},
                'HostConfig': {'RestartPolicy': {'Name': 'unless-stopped'}},
                'State': {'Status': 'exited', 'Running': False, 'Restarting': False, 'Paused': False, 'Pid': 0},
            }
            lkg2 = self._write_policy(root / 'lkg2.conf', {'ROLLBACK_EXCLUSIVE_SERVICES': 'worker', 'ROLLBACK_STOP_SIGNAL': 'SIGINT'})
            events.clear()
            backend2, _ = self._make_docker_mock(containers, on_kill=on_kill, on_update=on_update)
            journal2 = root / 'exclusive-stop-exited.json'
            rc = adapter.stop_exclusive_services(lkg2, journal2, uid=os.getuid(), backend_factory=lambda *_: backend2, monotonic=mono, sleep=slp)
            self.assertEqual(rc, 0)
            self.assertEqual(events, [('update', 'cid-exited', 'no')])
            self.assertEqual(containers['cid-exited']['HostConfig']['RestartPolicy']['Name'], 'no')

    def test_stop_exclusive_timeout_unverified_escalation_and_reconcile(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lkg = self._write_policy(root / 'lkg.conf', {'ROLLBACK_EXCLUSIVE_SERVICES': 'scheduler', 'ROLLBACK_STOP_TIMEOUT_SECONDS': '2'})
            audit = root / 'audit.log'
            containers = {
                'cid-stuck': {'Id': 'cid-stuck', 'Name': '/ci-fleet-deploy-example-scheduler',
                              'Config': {'Labels': self.labels, 'Image': self.base_values['ARTIFACT_IMAGE']},
                              'HostConfig': {'RestartPolicy': {'Name': 'always'}},
                              'State': {'Status': 'running', 'Running': True, 'Restarting': False, 'Paused': False, 'Pid': 99}},
            }
            signals = []
            def on_kill(cid, sig):
                signals.append((cid, sig))
                if sig == 'SIGKILL':
                    containers[cid]['State'].update({'Status': 'exited', 'Running': False, 'Pid': 0})
                return None
            backend, _ = self._make_docker_mock(containers, on_kill=on_kill)
            journal = root / 'exclusive-stop.json'
            mono, slp = self._fake_clock()

            # Default timeout does not send SIGKILL, records timeout + audit, returns 4.
            rc = adapter.stop_exclusive_services(lkg, journal, audit_log=audit, uid=os.getuid(),
                                                 backend_factory=lambda *_: backend, monotonic=mono, sleep=slp)
            self.assertEqual(rc, 4)
            self.assertEqual(signals, [('cid-stuck', 'SIGTERM')])
            jdata = json.loads(journal.read_text())
            self.assertEqual((jdata['status'], jdata['signaled'], jdata['adapter_dispatched'], jdata['escalation']),
                             ('timeout', True, False, 'none'))
            self.assertIn('result=stop_failed reason=timeout', audit.read_text())

            # Docker can reject signals during restart backoff. The helper still
            # waits to the deadline and permits a verified explicit escalation.
            crash = containers['cid-stuck']
            crash['State'].update(Status='restarting', Restarting=True, Pid=0)
            crash['HostConfig']['RestartPolicy']['Name'] = 'always'
            crash_journal = root / 'crash-stop.json'
            def signal_crash(cid, sig):
                if sig != 'SIGKILL':
                    return 409, b''
                crash['State']['Restarting'] = False
                return on_kill(cid, sig)
            crash_backend, _ = self._make_docker_mock(containers, on_kill=signal_crash)
            self.assertEqual(adapter.stop_exclusive_services(
                lkg, crash_journal, uid=os.getuid(), backend_factory=lambda *_: crash_backend,
                monotonic=mono, sleep=slp), 4)
            self.assertEqual(json.loads(crash_journal.read_text())['reason'], 'timeout')
            self.assertEqual(adapter.stop_exclusive_services(
                lkg, crash_journal, reconcile=True, force_stop_timeout=True, uid=os.getuid(),
                backend_factory=lambda *_: crash_backend, monotonic=mono, sleep=slp), 0)
            crash['State'].update(Status='running', Running=True, Pid=99)

            # Reconcile while still running without --force-stop-timeout refuses without re-signaling.
            signals.clear()
            rc = adapter.stop_exclusive_services(lkg, journal, reconcile=True, audit_log=audit, uid=os.getuid(),
                                                 backend_factory=lambda *_: backend, monotonic=mono, sleep=slp)
            self.assertEqual(rc, 4)
            self.assertEqual(signals, [])

            # Reconcile pre-signal failure retains signaled=True and timed_out_containers.
            failing_inv = MagicMock()
            failing_inv.request.side_effect = OSError('inventory unavailable')
            rc_inv = adapter.stop_exclusive_services(lkg, journal, reconcile=True, audit_log=audit, uid=os.getuid(),
                                                     backend_factory=lambda *_: failing_inv, monotonic=mono, sleep=slp)
            self.assertEqual(rc_inv, 3)
            j_inv = json.loads(journal.read_text())
            self.assertTrue(j_inv['signaled'])
            self.assertEqual(j_inv['timed_out_containers'], {'scheduler': 'cid-stuck'})
            self.assertTrue(adapter.read_stop_status(journal, uid=os.getuid())[0])

            # Reconcile after natural exit succeeds without --force-stop-timeout and without escalation.
            containers['cid-stuck']['State'].update({'Status': 'exited', 'Running': False, 'Pid': 0})
            rc_nat = adapter.stop_exclusive_services(lkg, journal, reconcile=True, audit_log=audit, uid=os.getuid(),
                                                     backend_factory=lambda *_: backend, monotonic=mono, sleep=slp)
            self.assertEqual(rc_nat, 0)
            self.assertEqual(json.loads(journal.read_text())['escalation'], 'none')

            # Reset running container -> timeout -> Reconcile with --force-stop-timeout escalates to SIGKILL.
            containers['cid-stuck']['State'].update({'Status': 'running', 'Running': True, 'Pid': 99})
            journal_esc = root / 'exclusive-stop-esc.json'
            rc = adapter.stop_exclusive_services(lkg, journal_esc, audit_log=audit, uid=os.getuid(),
                                                 backend_factory=lambda *_: backend, monotonic=mono, sleep=slp)
            self.assertEqual(rc, 4)
            signals.clear()
            rc = adapter.stop_exclusive_services(lkg, journal_esc, reconcile=True, force_stop_timeout=True, audit_log=audit,
                                                 uid=os.getuid(), backend_factory=lambda *_: backend, monotonic=mono, sleep=slp)
            self.assertEqual(rc, 0)
            self.assertEqual(signals, [('cid-stuck', 'SIGKILL')])
            self.assertEqual(json.loads(journal_esc.read_text())['escalation'], 'sigkill')
            self.assertIn('result=stop_escalated', audit.read_text())

            # Mid-stop Docker disconnect during kill returns exit 4 with docker_communication_failed and audit.
            containers['cid-disc'] = {
                'Id': 'cid-disc', 'Name': '/ci-fleet-deploy-example-scheduler',
                'Config': {'Labels': self.labels, 'Image': self.base_values['ARTIFACT_IMAGE']},
                'HostConfig': {'RestartPolicy': {'Name': 'no'}},
                'State': {'Status': 'running', 'Running': True, 'Restarting': False, 'Paused': False, 'Pid': 100},
            }
            del containers['cid-stuck']
            def on_kill_disconnect(cid, sig):
                raise OSError('docker socket disconnected')
            backend_disc, _ = self._make_docker_mock(containers, on_kill=on_kill_disconnect)
            journal_disc = root / 'exclusive-stop-disconnect.json'
            audit_disc = root / 'audit-disc.log'
            rc_disc = adapter.stop_exclusive_services(lkg, journal_disc, audit_log=audit_disc, uid=os.getuid(),
                                                      backend_factory=lambda *_: backend_disc, monotonic=mono, sleep=slp)
            self.assertEqual(rc_disc, 4)
            self.assertEqual(json.loads(journal_disc.read_text())['reason'], 'docker_communication_failed')
            self.assertIn('result=stop_failed reason=docker_communication_failed', audit_disc.read_text())
            del containers['cid-disc']

            # Escalation where container still refuses to exit fails closed.
            containers['cid-unkillable'] = {
                'Id': 'cid-unkillable', 'Name': '/ci-fleet-deploy-example-scheduler',
                'Config': {'Labels': self.labels, 'Image': self.base_values['ARTIFACT_IMAGE']},
                'HostConfig': {'RestartPolicy': {'Name': 'no'}},
                'State': {'Status': 'running', 'Running': True, 'Restarting': False, 'Paused': False, 'Pid': 101},
            }
            backend_unkillable, _ = self._make_docker_mock(containers)
            journal2 = root / 'exclusive-stop-unkillable.json'
            rc = adapter.stop_exclusive_services(lkg, journal2, force_stop_timeout=True, audit_log=audit,
                                                 uid=os.getuid(), backend_factory=lambda *_: backend_unkillable, monotonic=mono, sleep=slp)
            self.assertEqual(rc, 4)
            self.assertEqual(json.loads(journal2.read_text())['reason'], 'forced_stop_exit_unverified')

            # Interrupted inflight stop reconciles via graceful signal.
            containers['cid-inflight'] = {
                'Id': 'cid-inflight', 'Name': '/ci-fleet-deploy-example-scheduler',
                'Config': {'Labels': self.labels, 'Image': self.base_values['ARTIFACT_IMAGE']},
                'HostConfig': {'RestartPolicy': {'Name': 'no'}},
                'State': {'Status': 'running', 'Running': True, 'Restarting': False, 'Paused': False, 'Pid': 102},
            }
            del containers['cid-unkillable']
            journal3 = root / 'exclusive-stop-inflight.json'
            adapter.durable_file(journal3, json.dumps({
                'schema_version': 1, 'status': 'inflight', 'signaled': True, 'adapter_dispatched': False,
                'environment': 'staging', 'target': 'app', 'deployer_identity': 'example',
                'service': 'scheduler', 'container_id': 'cid-inflight',
            }).encode())
            signals.clear()
            def kill_graceful(cid, sig):
                signals.append((cid, sig))
                containers[cid]['State'].update({'Status': 'exited', 'Running': False, 'Pid': 0})
                return None
            backend_inf, _ = self._make_docker_mock(containers, on_kill=kill_graceful)
            rc = adapter.stop_exclusive_services(lkg, journal3, reconcile=True, uid=os.getuid(),
                                                 backend_factory=lambda *_: backend_inf, monotonic=mono, sleep=slp)
            self.assertEqual(rc, 0)
            self.assertEqual(signals, [('cid-inflight', 'SIGTERM')])

    def test_removed_container_inventory_must_complete_before_stop_deadline(self):
        monotonic, sleep = self._fake_clock()
        backend = MagicMock()

        def request(_method, path):
            if "/containers/json?" in path:
                sleep(2)
                return 200, b"[]"
            return 404, b"{}"

        backend.request.side_effect = request
        result = adapter._wait_container_exited(
            backend, "removed", "ci-fleet-deploy-example-", "scheduler",
            self.labels, 2, 0, monotonic, sleep,
        )
        self.assertEqual(result, (False, 2.0, "stop_deadline_unverified"))

    def test_late_graceful_signal_does_not_authorize_forced_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = self._write_policy(root / 'policy.conf', {
                'ROLLBACK_EXCLUSIVE_SERVICES': 'scheduler', 'ROLLBACK_STOP_TIMEOUT_SECONDS': '2',
            })
            monotonic, sleep = self._fake_clock()
            journal = root / 'stop.json'
            containers = {'current': {
                'Id': 'current', 'Name': '/ci-fleet-deploy-example-scheduler',
                'Config': {'Labels': self.labels},
                'HostConfig': {'RestartPolicy': {'Name': 'no'}},
                'State': {'Status': 'running', 'Running': True, 'Restarting': False, 'Paused': False, 'Pid': 3},
            }}
            signals = []

            def late_sig(_cid, sig):
                signals.append(sig)
                sleep(2)
                return 409, b"{}"

            backend, _ = self._make_docker_mock(containers, on_kill=late_sig)
            self.assertEqual(adapter.stop_exclusive_services(
                policy, journal, force_stop_timeout=True, uid=os.getuid(),
                backend_factory=lambda *_: backend, monotonic=monotonic, sleep=sleep,
            ), 4)
            self.assertEqual(signals, ['SIGTERM'])
            self.assertEqual(json.loads(journal.read_text())['reason'], 'stop_deadline_unverified')

            journal = root / 'incomplete-stop.json'
            signals.clear()
            def incomplete_state(cid, sig):
                signals.append(sig)
                containers[cid]['State'] = {'Status': 'running'}
                return 204, b''

            backend, _ = self._make_docker_mock(containers, on_kill=incomplete_state)
            self.assertEqual(adapter.stop_exclusive_services(
                policy, journal, force_stop_timeout=True, uid=os.getuid(),
                backend_factory=lambda *_: backend, monotonic=monotonic, sleep=sleep,
            ), 4)
            self.assertEqual(signals, ['SIGTERM'])
            self.assertEqual(json.loads(journal.read_text())['reason'], 'container_state_unverified')

    def test_rollback_broker_guards_and_real_socket_ordering(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lkg_img = self.base_values['ARTIFACT_IMAGE']
            bad_img = lkg_img.replace('a' * 64, 'b' * 64)
            net_name = 'ci-fleet-deploy-example-net'
            containers = {
                'old-sched': {'Id': 'old-sched', 'Name': '/ci-fleet-deploy-example-scheduler',
                              'Config': {'Labels': self.labels, 'Image': bad_img},
                              'HostConfig': {'RestartPolicy': {'Name': 'always'}},
                              'State': {'Status': 'running', 'Running': True, 'Restarting': False, 'Paused': False, 'Pid': 50}},
            }
            containers['web'] = {
                'Id': 'web', 'Name': '/ci-fleet-deploy-example-web',
                'Config': {'Labels': self.labels, 'Image': bad_img},
                'HostConfig': {'RestartPolicy': {'Name': 'always'}},
                'State': {'Status': 'running', 'Running': True, 'Restarting': False,
                          'Paused': False, 'Pid': 51, 'StartedAt': '2026-11-22T01:00:00Z'},
            }
            event_log = []
            def on_update(cid, payload):
                event_log.append(f'update:{cid}:{payload["RestartPolicy"]["Name"]}')
                return None
            def on_kill(cid, sig):
                event_log.append(f'kill:{cid}:{sig}')
                containers[cid]['State'].update({'Status': 'exited', 'Running': False, 'Pid': 0})
                return None

            # Serve over a real local Unix socket for full end-to-end HTTP verification.
            sock_path = str(root / 'docker.sock')
            class DockerHandler(adapter.http.server.BaseHTTPRequestHandler):
                def log_message(self, *_):
                    pass
                def do_GET(self):
                    p = adapter.urllib.parse.urlsplit(self.path).path
                    if p == '/containers/json':
                        items = [{'Id': cid, 'Names': [c['Name']], 'Labels': dict(c['Config']['Labels']), 'State': c['State']['Status']}
                                 for cid, c in containers.items()]
                        data = json.dumps(items).encode()
                        self.send_response(200)
                    elif p.startswith('/containers/') and p.endswith('/json'):
                        cid = adapter.urllib.parse.unquote(p[len('/containers/'):-len('/json')])
                        if cid not in containers:
                            self.send_response(404); data = b'{}'
                        else:
                            event_log.append(f'inspect:{cid}:{containers[cid]["State"]["Status"]}')
                            data = json.dumps(containers[cid]).encode()
                            self.send_response(200)
                    elif p.startswith('/networks/'):
                        data = json.dumps({'Labels': self.server.labels, 'Driver': 'bridge', 'Internal': True}).encode()
                        self.send_response(200)
                    elif p.startswith('/images/') and p.endswith('/json'):
                        data = json.dumps({'Id': 'sha256:' + 'a' * 64, 'Config': {'Volumes': None}}).encode()
                        self.send_response(200)
                    else:
                        self.send_response(404); data = b'{}'
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    if data:
                        self.wfile.write(data)
                def do_POST(self):
                    parsed = adapter.urllib.parse.urlsplit(self.path)
                    length = int(self.headers.get('Content-Length', '0'))
                    body = self.rfile.read(length) if length else b''
                    if parsed.path == '/containers/create':
                        name = adapter.urllib.parse.parse_qs(parsed.query)['name'][0]
                        payload = json.loads(body)
                        cid = 'new-' + name
                        containers[cid] = {'Id': cid, 'Name': '/' + name, 'Image': 'sha256:' + 'a' * 64, 'Config': {'Labels': payload['Labels'], 'Image': payload['Image']},
                                           'HostConfig': {'RestartPolicy': {'Name': 'no'}},
                                           'State': {'Status': 'created', 'Running': False, 'Restarting': False, 'Paused': False, 'Pid': 0}}
                        event_log.append(f'create:{cid}')
                        data = json.dumps({'Id': cid}).encode()
                        self.send_response(201)
                    else:
                        parts = parsed.path.strip('/').split('/')
                        cid, action = adapter.urllib.parse.unquote(parts[1]), parts[2]
                        if action == 'update':
                            on_update(cid, json.loads(body))
                            containers[cid]['HostConfig']['RestartPolicy'] = json.loads(body)['RestartPolicy']
                            self.send_response(200); data = b'{}'
                        elif action == 'kill':
                            sig = adapter.urllib.parse.parse_qs(parsed.query).get('signal', ['SIGTERM'])[0]
                            on_kill(cid, sig)
                            self.send_response(204); data = b''
                        elif action == 'start':
                            event_log.append(f'start:{cid}')
                            containers[cid]['State'].update({'Status': 'running', 'Running': True, 'Pid': 200, 'Health': {'Status': 'healthy'}})
                            self.send_response(204); data = b''
                        else:
                            self.send_response(404); data = b'{}'
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    if data:
                        self.wfile.write(data)

            class LocalDocker(adapter.socketserver.UnixStreamServer):
                labels = self.labels

            with LocalDocker(sock_path, DockerHandler) as docker_srv:
                dthread = threading.Thread(target=docker_srv.serve_forever, daemon=True)
                dthread.start()
                try:
                    with patch.dict(os.environ, {'CI_FLEET_DEPLOYER_TESTING': '1', 'CI_FLEET_DEPLOYER_TEST_DOCKER_SOCKET': '1'}):
                        lkg = self._write_policy(root / 'lkg.conf', {'DOCKER_SOCKET': sock_path, 'ROLLBACK_EXCLUSIVE_SERVICES': 'scheduler'})
                        backend = adapter.TestingDockerBackend(sock_path, 30)
                        marker = root / 'rollback-commit'
                        policy = adapter.Policy(json.loads(json.dumps(dict(self.base_values, DOCKER_SOCKET=sock_path, ROLLBACK_EXCLUSIVE_SERVICES='scheduler'))), backend,
                                                marker=marker, operation='rollback', exclusive_services=('scheduler',),
                                                unit='ci-fleet-deployer-adapter-1-2.service')
                        policy.capture_rollback_incumbents()
                        proxy_sock = str(root / 'proxy.sock')
                        with adapter.ProxyServer(proxy_sock, adapter.handler(policy)) as proxy, \
                             patch.object(adapter, 'executor_peer', return_value=True):
                            pthread = threading.Thread(target=proxy.serve_forever, daemon=True)
                            pthread.start()
                            try:
                                # Broker blocks creating/starting replacement while old-sched is still running.
                                conn = adapter.UnixConnection(proxy_sock, rootless=False)
                                req_body = json.dumps({
                                    'Image': lkg_img, 'Labels': dict(self.labels, **{adapter.SERVICE_LABEL: 'scheduler'}),
                                    'HostConfig': {'CapDrop': ['ALL'], 'ReadonlyRootfs': True, 'SecurityOpt': ['no-new-privileges'],
                                                   'CgroupnsMode': 'private', 'Memory': 104857600, 'NanoCpus': 1000000000,
                                                   'PidsLimit': 32, 'NetworkMode': net_name},
                                }).encode()
                                conn.request('POST', '/containers/create?name=ci-fleet-deploy-example-scheduler-new', req_body)
                                resp = conn.getresponse(); resp.read(); conn.close()
                                self.assertEqual(resp.status, 403)

                                # Broker blocks starting old broken-image container.
                                conn = adapter.UnixConnection(proxy_sock, rootless=False)
                                conn.request('POST', '/containers/old-sched/start')
                                resp = conn.getresponse(); resp.read(); conn.close()
                                self.assertEqual(resp.status, 403)

                                # Core stops old-sched and verifies exit before adapter starts replacement.
                                journal = root / 'exclusive-stop.json'
                                rc = adapter.stop_exclusive_services(lkg, journal, uid=os.getuid())
                                self.assertEqual(rc, 0)

                                # The stopped incumbent cannot satisfy commit readiness.
                                with self.assertRaisesRegex(ValueError, 'scheduler is missing'):
                                    policy.verify_rollback_commit()

                                # Restartable stopped containers still block replacement.
                                containers['old-sched']['HostConfig']['RestartPolicy']['Name'] = 'always'
                                with self.assertRaisesRegex(ValueError, 'active or restartable'):
                                    policy._require_no_active_service_container('scheduler')
                                containers['old-sched']['HostConfig']['RestartPolicy']['Name'] = 'no'

                                # Replacement create + start succeeds after exit verified.
                                conn = adapter.UnixConnection(proxy_sock, rootless=False)
                                conn.request('POST', '/containers/create?name=ci-fleet-deploy-example-scheduler-new', req_body)
                                resp = conn.getresponse(); new_id = json.loads(resp.read())['Id']; conn.close()
                                self.assertEqual(resp.status, 201)

                                conn = adapter.UnixConnection(proxy_sock, rootless=False)
                                conn.request('POST', f'/containers/{new_id}/start')
                                resp = conn.getresponse(); resp.read(); conn.close()
                                self.assertEqual(resp.status, 204)

                                # Unhealthy, restarting, or paused active container prevents rollback-commit.
                                for bad_patch in (
                                    {'Health': {'Status': 'unhealthy'}},
                                    {'Health': {'Status': 'starting'}},
                                    {'Health': {'Status': 'healthy'}, 'Restarting': True},
                                    {'Health': {'Status': 'healthy'}, 'Restarting': False, 'Paused': True},
                                ):
                                    containers[new_id]['State'].update(bad_patch)
                                    conn = adapter.UnixConnection(proxy_sock, rootless=False)
                                    conn.request('POST', '/ci-fleet/rollback-commit', b'committed')
                                    resp = conn.getresponse(); resp.read(); conn.close()
                                    self.assertEqual(resp.status, 403)
                                containers[new_id]['State'].update({'Restarting': False, 'Paused': False})

                                replacement = containers[new_id]
                                replacement['Image'] = 'sha256:' + 'b' * 64
                                with self.assertRaisesRegex(ValueError, 'does not match approved artifact'):
                                    policy.verify_rollback_commit()
                                replacement['Image'] = 'sha256:' + 'a' * 64
                                replacement['Config']['Healthcheck'] = {'Test': ['CMD', 'check']}
                                replacement['State'].pop('Health')
                                with self.assertRaisesRegex(ValueError, 'health is unverified'):
                                    policy.verify_rollback_commit()
                                replacement['State']['Health'] = {'Status': 'healthy'}

                                # A durable successful adapter outcome can wait for
                                # Docker health without dispatching the adapter again.
                                store = root / 'recovery'
                                journal_dir = store / policy.unit
                                store.mkdir(mode=0o700)
                                journal_dir.mkdir(mode=0o700)
                                adapter.durable_file(journal_dir / 'policy.conf', lkg.read_bytes())
                                policy.rollback_ready = journal_dir / 'rollback-ready.json'
                                containers[new_id]['State']['Health']['Status'] = 'unhealthy'
                                conn = adapter.UnixConnection(proxy_sock, rootless=False)
                                conn.request('POST', '/ci-fleet/rollback-commit', b'committed')
                                resp = conn.getresponse(); resp.read(); conn.close()
                                self.assertEqual(resp.status, 204)
                                conn = adapter.UnixConnection(proxy_sock, rootless=False)
                                conn.request('POST', f'/containers/{new_id}/kill?signal=SIGTERM')
                                resp = conn.getresponse(); resp.read(); conn.close()
                                self.assertEqual(resp.status, 403)
                                with patch.object(adapter, 'Backend', return_value=backend), \
                                     patch.object(adapter, 'trusted_directory'), \
                                     patch.object(adapter, 'unit_inactive', return_value=True):
                                    with self.assertRaisesRegex(ValueError, 'broker drain is unverified'):
                                        adapter.complete_rollback_readiness(store, policy.unit, os.getuid())
                                    adapter.durable_file(journal_dir / 'rollback-drained',
                                                         adapter.hashlib.sha256(policy.rollback_ready.read_bytes()).hexdigest().encode())
                                    with self.assertRaisesRegex(ValueError, 'health is unverified'):
                                        adapter.complete_rollback_readiness(store, policy.unit, os.getuid())
                                    self.assertFalse(marker.exists())
                                    containers[new_id]['State']['Health']['Status'] = 'healthy'
                                    monotonic, sleep = self._fake_clock()
                                    def verify_late(_policy):
                                        sleep(31)
                                    with patch.object(adapter.time, 'monotonic', monotonic), \
                                         patch.object(adapter.Policy, 'verify_rollback_commit', verify_late):
                                        with self.assertRaisesRegex(ValueError, 'readiness deadline elapsed'):
                                            adapter.complete_rollback_readiness(store, policy.unit, os.getuid())
                                    self.assertFalse(marker.exists())
                                    self.assertTrue(adapter.complete_rollback_readiness(store, policy.unit, os.getuid()))
                                status = json.loads((journal_dir / 'status.json').read_text())
                                self.assertEqual(status['verified_containers'], [new_id])
                                self.assertEqual(containers['web']['Config']['Image'], bad_img)
                                self.assertTrue(containers['web']['State']['Running'])

                                # A later rollback ignores the retained, inert old container.
                                self.assertEqual(adapter.stop_exclusive_services(
                                    lkg, root / 'repeat-stop.json', uid=os.getuid()), 0)
                            finally:
                                proxy.shutdown()
                                pthread.join()
                    # Exact order proof: update -> kill -> exited inspect precedes create -> start.
                    upd_idx = event_log.index('update:old-sched:no')
                    kill_idx = event_log.index('kill:old-sched:SIGTERM')
                    exit_idx = event_log.index('inspect:old-sched:exited')
                    create_idx = event_log.index('create:new-ci-fleet-deploy-example-scheduler-new')
                    start_idx = event_log.index('start:new-ci-fleet-deploy-example-scheduler-new')
                    self.assertLess(upd_idx, kill_idx)
                    self.assertLess(kill_idx, exit_idx)
                    self.assertLess(exit_idx, create_idx)
                    self.assertLess(create_idx, start_idx)
                finally:
                    docker_srv.shutdown()
                    dthread.join()


if __name__ == '__main__':
    unittest.main()
