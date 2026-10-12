#!/usr/bin/env python3
"""Adversarial checks for the deployment Docker and recovery boundary."""
import copy
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

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

    def test_host_escape_requests(self):
        for key, value in {'Privileged': True, 'CapAdd': ['SYS_ADMIN'], 'PidMode': 'host', 'IpcMode': 'host',
                           'UsernsMode': 'host', 'CgroupnsMode': 'host', 'Devices': [{'PathOnHost': '/dev/sda'}],
                           'SecurityOpt': ['seccomp=unconfined'], 'NetworkMode': 'host', 'CgroupParent': '/system.slice',
                           'Runtime': 'unreviewed', 'ReadonlyRootfs': False, 'PidsLimit': 0}.items():
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

    def test_duplicate_json(self):
        with self.assertRaises(ValueError):
            adapter.decode(b'{"Privileged":false,"Privileged":true}')

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


if __name__ == '__main__':
    unittest.main()
