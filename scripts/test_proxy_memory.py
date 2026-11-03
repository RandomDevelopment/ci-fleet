"""Verify large concurrent uploads under the unchanged proxy cgroup envelope.

Run as root only on a disposable Docker/systemd/cgroup-v2 validation worker.
The backend drains fixture bytes, never loads images or runs application code.
"""
import argparse
import datetime
import hashlib
import http.server
import importlib.util
import json
import os
import pathlib
import signal
import socket
import socketserver
import subprocess
import threading
import tempfile
import time

parser = argparse.ArgumentParser()
parser.add_argument('--output')
parser.add_argument('--image', required=True)
args = parser.parse_args()
assert os.geteuid() == 0 and pathlib.Path('/sys/fs/cgroup/cgroup.controllers').is_file(), 'disposable root cgroup-v2 worker required'
temporary = tempfile.TemporaryDirectory(prefix='proxy-memory-', dir='/var/tmp') if not args.output else None
root = pathlib.Path(args.output or temporary.name)
if args.output:
    root.mkdir(mode=0o700)
locks = root / 'locks'
locks.mkdir(mode=0o755)
upstream = root / 'upstream.sock'
name = 'ci-fleet-proxy-memory-' + root.name
image = args.image
upload_size = 1 << 30
events = []
stop = threading.Event()
samples = []
clients = []
phase = {'name': 'start'}

def event(kind, **values):
    events.append(dict(at=datetime.datetime.now(datetime.timezone.utc).isoformat(), monotonic=time.monotonic(), event=kind, **values))

class Backend(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def log_message(self, *unused):
        pass
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Length', '2')
        self.end_headers()
        self.wfile.write(b'OK')
    def do_POST(self):
        total = int(self.headers['Content-Length'])
        event('forward_started', length=total)
        phase['name'] = 'forwarding'
        digest = hashlib.sha256()
        count = 0
        while count < total:
            chunk = self.rfile.read(min(256 * 1024, total - count))
            if not chunk:
                break
            count += len(chunk)
            digest.update(chunk)
            time.sleep(0.001)
        event('forward_complete', bytes=count, sha256=digest.hexdigest())
        body = b'{"stream":"loaded fixture"}\n'
        self.send_response(200 if count == total else 400)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

server = Server(str(upstream), Backend)
threading.Thread(target=server.serve_forever, daemon=True).start()

def docker(*words):
    return subprocess.check_output(['docker', *words], text=True, timeout=30).strip()

def upload(index):
    try:
        data = bytes(256 * 1024)
        digest = hashlib.sha256()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(120)
            sock.connect(str(locks / 'docker.sock'))
            sock.sendall(('POST /v1.55/images/load?quiet=1 HTTP/1.1\r\nHost: docker\r\nContent-Type: application/x-tar\r\nContent-Length: ' + str(upload_size) + '\r\nConnection: close\r\n\r\n').encode())
            count = 0
            while count < upload_size:
                part = data[:min(len(data), upload_size - count)]
                sock.sendall(part)
                count += len(part)
                digest.update(part)
            event('upload_sent', client=index, bytes=count, sha256=digest.hexdigest())
            response = bytearray()
            while chunk := sock.recv(8192):
                response.extend(chunk)
            event('client_complete', client=index, response=response.decode(errors='replace')[:1024])
    except Exception as error:
        event('client_failed', client=index, error=type(error).__name__ + ': ' + str(error))

def terminate(signum, _frame):
    raise SystemExit(128 + signum)


signal.signal(signal.SIGTERM, terminate)
container = None
try:
    cmd = ['run', '-d', '--name', name, '--memory=128m', '--cpus=1', '--user=0:0', '--network=none', '--security-opt=no-new-privileges',
           '--health-cmd', '/usr/local/bin/ci-fleet-controller --check-docker-socket-proxy',
           '--health-interval=5s', '--health-timeout=5s', '--health-retries=6',
           '--mount', 'type=bind,src=' + str(upstream) + ',dst=/var/run/docker.sock',
           '--mount', 'type=bind,src=' + str(locks) + ',dst=/run/ci-fleet/locks']
    cmd += [image, '--docker-socket-proxy']
    container = docker(*cmd)
    initial = json.loads(docker('inspect', '--format', '{"pid":{{.State.Pid}},"image":{{json .Image}},"memory":{{.HostConfig.Memory}}}', container))
    cgroup_line = next(line for line in pathlib.Path('/proc', str(initial['pid']), 'cgroup').read_text().splitlines() if line.startswith('0::'))
    group = pathlib.Path('/sys/fs/cgroup') / cgroup_line[3:].lstrip('/')
    event('started', container=container, image=initial['image'], memory_limit=initial['memory'], cgroup=str(group), size_per_upload=upload_size)
    def monitor():
        while not stop.wait(0.01):
            try:
                values = {key: int(value) for key, value in (line.split() for line in (group / 'memory.stat').read_text().splitlines())}
                sample = {'at':time.monotonic(), 'phase':phase['name'], 'current':int((group / 'memory.current').read_text()), 'peak':int((group / 'memory.peak').read_text()),
                          'events':dict(line.split() for line in (group / 'memory.events').read_text().splitlines())}
                sample.update({key:values[key] for key in ['anon','file','kernel','sock','shmem','file_dirty','file_writeback','active_file','inactive_file'] if key in values})
                samples.append(sample)
            except FileNotFoundError:
                return
    monitor_thread = threading.Thread(target=monitor)
    monitor_thread.start()
    limit = time.monotonic() + 20
    while not (locks / 'docker.sock').exists() and time.monotonic() < limit:
        time.sleep(0.02)
    assert (locks / 'docker.sock').exists(), 'fixture socket did not appear'
    phase['name'] = 'spooling'
    threads = [threading.Thread(target=upload, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(150)
        assert not thread.is_alive(), 'fixture upload deadline exceeded'
    final = json.loads(docker('inspect', '--format', '{"state":{{json .State}},"restart_count":{{.RestartCount}}}', container))
    event('container_result', state=final)
finally:
    stop.set()
    if 'monitor_thread' in locals():
        monitor_thread.join(3)
    server.shutdown()
    server.server_close()
    if container:
        try:
            (root / 'proxy.log').write_text(docker('logs', container))
        finally:
            docker('rm', '-f', container)
    upstream.unlink(missing_ok=True)
    (root / 'events.json').write_text(json.dumps(events, indent=2) + '\n')
    (root / 'memory.json').write_text(json.dumps(samples) + '\n')
    summary = {'at':datetime.datetime.now(datetime.timezone.utc).isoformat(), 'image':image,
               'uploads':len([e for e in events if e['event'] == 'client_complete']),
               'failures':len([e for e in events if e['event'] == 'client_failed']),
               'memory_peak':max((s['peak'] for s in samples), default=None),
               'file_peak':max((s.get('file', 0) for s in samples), default=None),
               'file_dirty_peak':max((s.get('file_dirty', 0) for s in samples), default=None),
               'anon_peak':max((s.get('anon', 0) for s in samples), default=None),
               'oom_kill':max((int(s['events'].get('oom_kill', '0')) for s in samples), default=None),
               'container_oom_killed':final['state']['OOMKilled'] if 'final' in locals() else None,
               'fixture_container_removed':True}
    (root / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary, indent=2))

assert summary['uploads'] == 2 and summary['failures'] == 0, 'concurrent full-size upload failed'
assert summary['oom_kill'] == 0 and summary['container_oom_killed'] is False and summary['memory_peak'] < 96 * 1024**2, 'upload exceeded proxy memory headroom'
completed = [e for e in events if e['event'] == 'forward_complete']
sent = [e for e in events if e['event'] == 'upload_sent']
assert len(completed) == len(sent) == 2
assert all(e['bytes'] == upload_size and e['sha256'] == sent[0]['sha256'] for e in completed), 'forwarding changed upload bytes'
assert all(' 200 ' in e['response'] for e in events if e['event'] == 'client_complete')
assert any(s['phase'] == 'spooling' for s in samples) and any(s['phase'] == 'forwarding' for s in samples), 'both memory phases must be observed'
if temporary:
    temporary.cleanup()
