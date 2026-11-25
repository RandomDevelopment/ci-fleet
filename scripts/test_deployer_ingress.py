#!/usr/bin/env python3
"""Exercise deployer HTTP ingress on a disposable Linux Docker daemon."""

from contextlib import ExitStack
import errno
from http.client import HTTPConnection
import http.server
import json
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import deployer_adapter as adapter


CANDIDATE = 'docker.io/library/python@sha256:9ef1a599f863231c3d91e941389bfdc4a2d6070557243d54df1a4e57015b960e'
RETAINED = 'docker.io/library/python@sha256:c5ba3deef00c7758561a0f03c26f44f10589263d40e732b8748e0d1f38f6dd89'
APP_UID = 10001
PROXY_ID = {'user': 65534, 'group': 65534, 'extra_groups': []}
HTTP_SERVER = '''
import http.server, os, signal, socketserver, sys
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(sys.argv[1].encode())
    def log_message(self, *_):
        pass

if sys.argv[1] == 'legacy':
    server = http.server.HTTPServer(('', 8080), Handler)
else:
    path = '/run/ingress/http.sock'
    if os.path.lexists(path):
        os.unlink(path)
    server = socketserver.UnixStreamServer(path, Handler)
    os.chmod(path, 0o666)
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
with server:
    server.serve_forever()
'''
CONNECT = "import socket,sys; socket.socket(socket.AF_UNIX).connect(sys.argv[1])"
GET_UNIX = (
    "import http.client,socket; "
    "client=http.client.HTTPConnection('localhost',timeout=2); "
    "client.sock=socket.socket(socket.AF_UNIX); client.sock.settimeout(2); "
    "client.sock.connect('/run/ingress/http.sock'); client.request('GET','/health'); "
    "assert client.getresponse().status==200"
)
GET_TCP = "import sys,urllib.request; assert urllib.request.urlopen(sys.argv[1],timeout=2).status==200"
LOCAL_TCP = 'http://127.0.0.1:8080/health'


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=60).stdout.strip()


def docker(*args):
    return run('docker', *args)


def api(policy, method, path, value=None):
    path, body = policy.authorize(method, path, json.dumps(value).encode() if value is not None else b'')
    status, response = policy.backend.request(method, path, body, {'Content-Type': 'application/json'})
    assert status in (201, 204), (status, response)
    return json.loads(response) if response else None


def wait_for_health(container):
    with subprocess.Popen(
        ['docker', 'events', '--since', '0', '--filter', f'container={container}',
         '--filter', 'event=health_status: healthy', '--format', '{{.Action}}'],
        stdout=subprocess.PIPE,
    ) as events:
        try:
            ready, _, _ = select.select([events.stdout], [], [], 30)
            assert ready and events.stdout.readline().strip() == b'health_status: healthy', docker('inspect', '--format', '{{json .State.Health}}', container)
        finally:
            if events.poll() is None:
                events.terminate()


def start_service(cleanup, policy, name, value):
    container = api(policy, 'POST', f'/containers/create?name={name}', value)['Id']
    cleanup.callback(docker, 'rm', '-f', container)
    api(policy, 'POST', f'/containers/{container}/start')
    wait_for_health(container)
    return container


def reverse_proxy(socket_path):
    class Proxy(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            response = subprocess.run(
                ['curl', '--noproxy', '*', '--fail', '--silent', '--max-time', '2',
                 '--unix-socket', str(socket_path), 'http://localhost/health'],
                capture_output=True, timeout=3, **PROXY_ID,
            )
            self.send_response(502 if response.returncode else 200)
            self.end_headers()
            self.wfile.write(response.stdout)

        def log_message(self, *_):
            pass

    return http.server.HTTPServer(('127.0.0.1', 0), Proxy)


def host_http(port, status, body=b''):
    client = HTTPConnection('127.0.0.1', port, timeout=4)
    try:
        client.request('GET', '/health')
        response = client.getresponse()
        assert (response.status, response.read()) == (status, body)
    finally:
        client.close()


def main():
    started = time.monotonic()
    docker_socket = os.environ.get('DOCKER_HOST', 'unix:///var/run/docker.sock')
    assert docker_socket.startswith('unix:///') and not os.environ.get('DOCKER_CONTEXT'), 'a local Docker daemon is required'
    docker_socket = os.path.realpath(docker_socket.removeprefix('unix://'))
    os.environ['DOCKER_HOST'] = 'unix://' + docker_socket
    for image in (CANDIDATE, RETAINED):
        if subprocess.run(['docker', 'image', 'inspect', image], capture_output=True, timeout=30).returncode:
            docker('pull', image)
    os.environ.update(CI_FLEET_DEPLOYER_TESTING='1', CI_FLEET_DEPLOYER_TEST_DOCKER_SOCKET='1')
    backend = adapter.TestingDockerBackend(docker_socket, 180)
    values = {'ARTIFACT_IMAGE': CANDIDATE, 'ENVIRONMENT': 'staging', 'TARGET_ID': 'fixture',
              'DEPLOYER_IDENTITY': 'ingress-' + uuid.uuid4().hex[:10], 'DOCKER_SOCKET': docker_socket,
              'ROLLBACK_EXCLUSIVE_SERVICES': 'web', 'ROLLBACK_STOP_TIMEOUT_SECONDS': '10'}
    deploy = adapter.Policy(values, backend)
    retained = dict(values, ARTIFACT_IMAGE=RETAINED)
    rollback = adapter.Policy(retained, backend, operation='rollback', exclusive_services=('web',))
    bridge, outside, volume = (deploy.prefix + suffix for suffix in ('internal', 'outside', 'ingress'))

    with ExitStack() as cleanup:
        root = Path(cleanup.enter_context(tempfile.TemporaryDirectory(prefix='fleet-ingress-', dir='/tmp')))
        journal_dir = root / 'journal'
        journal_dir.mkdir(mode=0o700)
        root.chmod(0o711)
        for name in (bridge, outside):
            api(deploy, 'POST', '/networks/create', {'Name': name, 'Internal': True, 'Labels': deploy.labels})
            cleanup.callback(docker, 'network', 'rm', name)
        response = api(deploy, 'POST', '/volumes/create', {'Name': volume, 'Labels': deploy.labels})
        cleanup.callback(docker, 'volume', 'rm', volume)
        source = Path(response['Mountpoint'])
        os.chown(source, APP_UID, APP_UID)
        source.chmod(0o755)
        parent = root / 'proxy'
        parent.mkdir(mode=0o710)
        os.chown(parent, 0, PROXY_ID['group'])
        mountpoint = parent / 'upstream'
        mountpoint.mkdir()
        run('mount', '--bind', str(source), str(mountpoint))
        cleanup.callback(run, 'umount', str(mountpoint))
        run('mount', '-o', 'remount,bind,ro,nosuid,nodev,noexec,nosymfollow', str(mountpoint))
        flags = run('findmnt', '-n', '-o', 'VFS-OPTIONS', '--mountpoint', str(mountpoint)).split(',')
        assert {'ro', 'nosuid', 'nodev', 'noexec', 'nosymfollow'} <= set(flags), flags

        def payload(image, revision, network, **host_options):
            probe = ['python', '-c', GET_TCP, LOCAL_TCP] if revision == 'legacy' else ['python', '-c', GET_UNIX]
            return {
                'Image': image, 'User': str(APP_UID), 'Labels': deploy.labels,
                'Cmd': ['python', '-c', HTTP_SERVER, revision], 'StopSignal': 'SIGTERM',
                'Healthcheck': {'Test': ['CMD', *probe], 'Interval': 1_000_000_000,
                                'Timeout': 3_000_000_000, 'Retries': 2},
                'HostConfig': {
                    'CapDrop': ['ALL'], 'ReadonlyRootfs': True, 'SecurityOpt': ['no-new-privileges'],
                    'CgroupnsMode': 'private', 'Memory': 104857600, 'NanoCpus': 1000000000,
                    'PidsLimit': 32, 'NetworkMode': network, **host_options,
                },
            }

        with socket.socket() as free_port:
            free_port.bind(('127.0.0.1', 0))
            old_port = free_port.getsockname()[1]
        for address in ('0.0.0.0', '::', '', '192.0.2.1'):
            unsafe = payload(CANDIDATE, 'legacy', bridge, PortBindings={
                '8080/tcp': [{'HostIp': address, 'HostPort': str(old_port)}],
            })
            try:
                deploy.authorize('POST', f'/containers/create?name={deploy.prefix}public', json.dumps(unsafe).encode())
            except ValueError as error:
                assert 'only loopback application ports' in str(error)
            else:
                raise AssertionError('broker accepted a non-loopback Docker bind')

        with ExitStack() as legacy_cleanup:
            legacy_body = payload(CANDIDATE, 'legacy', bridge, PortBindings={
                '8080/tcp': [{'HostIp': '127.0.0.1', 'HostPort': str(old_port)}],
            })
            legacy_body['ExposedPorts'] = {'8080/tcp': {}}
            legacy = start_service(legacy_cleanup, deploy, deploy.prefix + 'legacy', legacy_body)
            info = backend.inspect('containers', legacy)
            assert info['HostConfig']['PortBindings'] == legacy_body['HostConfig']['PortBindings']
            assert not info['NetworkSettings']['Ports'], 'internal bridge unexpectedly published a port'
            with socket.socket() as client:
                assert client.connect_ex(('127.0.0.1', old_port)) == errno.ECONNREFUSED
            legacy_address = info['NetworkSettings']['Networks'][bridge]['IPAddress']
            peer = start_service(legacy_cleanup, deploy, deploy.prefix + 'outside', payload(CANDIDATE, 'legacy', outside))
            denied = subprocess.run(['docker', 'exec', peer, 'python', '-c', GET_TCP,
                                     f'http://{legacy_address}:8080/health'], capture_output=True, timeout=5)
            assert denied.returncode == 1 and b'URLError' in denied.stderr, denied.stderr
        print(f'Legacy loopback publication absent; separate bridge cannot reach the healthy TCP service ({time.monotonic() - started:.2f}s)', flush=True)

        socket_path = mountpoint / 'http.sock'
        server = cleanup.enter_context(reverse_proxy(socket_path))
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        cleanup.callback(thread.join)
        cleanup.callback(server.shutdown)
        port = server.server_address[1]
        mounts = [{'Type': 'volume', 'Source': volume, 'Target': '/run/ingress'}]
        candidate = start_service(cleanup, deploy, deploy.prefix + 'web', payload(
            CANDIDATE, 'candidate', bridge, Mounts=mounts, RestartPolicy={'Name': 'always'},
        ))
        host_http(port, 200, b'candidate')
        denied = subprocess.run([sys.executable, '-c', CONNECT, str(socket_path)],
                                user=65533, group=65533, extra_groups=[], capture_output=True, timeout=5)
        assert denied.returncode == 1 and b'PermissionError' in denied.stderr, denied.stderr
        info = backend.inspect('containers', candidate)
        assert not info['HostConfig']['PortBindings'] and not info['NetworkSettings']['Ports']
        assert set(info['NetworkSettings']['Networks']) == {bridge}

        policy_path = journal_dir / 'retained.conf'
        adapter.durable_file(policy_path, ''.join(f'{key}={value}\n' for key, value in retained.items()).encode())
        stop_journal = journal_dir / 'exclusive-stop.json'
        stop_started = time.monotonic()
        assert adapter.stop_exclusive_services(
            policy_path, stop_journal, backend_factory=adapter.TestingDockerBackend, seconds=20,
        ) == 0
        assert json.loads(stop_journal.read_text())['status'] == 'verified'
        assert adapter.container_exited_and_non_restartable(backend.inspect('containers', candidate))[0]
        host_http(port, 502)

        with socket.socket(socket.AF_UNIX) as control:
            control_path = root / 'control.sock'
            control.bind(str(control_path))
            control.listen()
            control_path.chmod(0o666)
            subprocess.run([sys.executable, '-c', CONNECT, str(control_path)], check=True, timeout=5, **PROXY_ID)
            (source / 'http.sock').unlink()
            (source / 'http.sock').symlink_to(control_path)
            with socket.socket(socket.AF_UNIX) as client:
                assert client.connect_ex(str(socket_path)) == errno.ELOOP
            host_http(port, 502)

        rollback.capture_rollback_incumbents()
        replacement_body = payload(RETAINED, 'retained', bridge, Mounts=mounts)
        replacement_body['Labels'] = dict(rollback.labels, **{adapter.SERVICE_LABEL: 'web'})
        replacement = start_service(cleanup, rollback, rollback.prefix + 'web-retained', replacement_body)
        rollback.verify_rollback_commit()
        assert rollback.verified_containers == [replacement]
        info = backend.inspect('containers', replacement)
        assert not info['HostConfig']['PortBindings'] and not info['NetworkSettings']['Ports']
        assert set(info['NetworkSettings']['Networks']) == {bridge}
        host_http(port, 200, b'retained')
        print(f'Verified stop and retained HTTP response through the same proxy ({time.monotonic() - stop_started:.2f}s)', flush=True)
    print(f'Deployer ingress passed on Docker {docker("version", "--format", "{{.Server.Version}}")} ({time.monotonic() - started:.2f}s)')


if __name__ == '__main__':
    def terminate(signum, _frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, terminate)
    try:
        main()
    except subprocess.CalledProcessError as error:
        print(error.stderr, file=sys.stderr)
        raise
