#!/usr/bin/env python3
"""Run independent regression groups within the current worker's resource budget."""
from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time

from validation_resources import resources
from local_validation_executor import Progress

# A host-mutating group is a barrier: it runs alone, not alongside other groups.
TASKS = [
    ('diagnostics', ['python3', 'scripts/test_validation_diagnostics.py'], False),
    ('resource-budget', ['python3', 'scripts/test_validation_resources.py'], False),
    ('task-execution', ['python3', 'scripts/test_validation_tasks.py'], False),
    ('fixture-manifest', ['python3', 'scripts/test_fixture_manifest.py'], False),
    ('validation-images', ['python3', 'scripts/test_validation_images.py'], False),
    ('task-plan', ['python3', '.github/actions/plan/test_plan.py'], False),
    ('runner-update', ['python3', 'scripts/test_update_actions_runner.py'], False),
    ('desired-state', ['python3', 'scripts/test_desired_state.py'], False),
    ('image-identity', ['python3', 'tests/test_managed_image_identity.py'], False),
    ('image-lifecycle', ['python3', 'scripts/test_portable_image_lifecycle.py'], False),
    ('apt-cache', ['python3', 'scripts/test_apt_cache.py'], False),
    ('config-approval', ['python3', 'scripts/test_config_approval.py'], False),
    ('local-validation', ['python3', 'scripts/test_local_validation.py'], True),
    ('validation-executor', ['python3', 'scripts/test_local_validation_executor.py'], True),
    ('cleanup-networks', ['python3', 'scripts/test_cleanup_networks.py'], False),
    ('cleanup-storage', ['python3', 'scripts/test_cleanup_storage.py'], False),
    ('network-policy', ['python3', 'scripts/test_apply_docker_network_policy.py'], False),
    ('remote-reconcile', ['python3', 'scripts/test_remote_reconcile.py'], False),
    ('explicit-recovery', ['python3', '-I', '-B', 'scripts/test_explicit_recovery.py'], False),
    ('health', ['python3', 'scripts/test_health.py'], False),
    ('endpoint-snapshot', ['python3', 'scripts/test_endpoint_snapshot.py'], False),
    ('snapshot-sources', ['python3', 'scripts/test_endpoint_snapshot_sources.py'], False),
    ('snapshot-store', ['python3', 'scripts/test_endpoint_snapshot_store.py'], False),
    ('status-receiver', ['python3', 'scripts/test_status_receiver.py'], False),
    ('quickstart', ['python3', 'scripts/test_quickstart.py'], False),
    ('github-bootstrap', ['scripts/test-bootstrap-github.sh'], True),
    ('commit-policy', ['python3', 'scripts/test_validate_commits.py'], False),
    ('workflow-policy', ['python3', 'scripts/test_workflow_tag_validation.py'], False),
    ('status-schema', ['python3', '-m', 'json.tool', 'schemas/status-report-v1.json'], False),
    ('capabilities', ['python3', 'scripts/desired_state.py', 'validate-engine-capabilities', '--manifest', 'engine-capabilities.json', '--require-docker-network-policy-config', '--require-status-reporting-config', '--require-status-reporting'], False),
    ('fast-plan', ['python3', '.github/actions/plan/plan.py', '--plan', 'examples/project/scripts/ci/plan.json', '--group', 'fast'], False),
    ('full-plan', ['python3', '.github/actions/plan/plan.py', '--plan', 'examples/project/scripts/ci/plan.json', '--group', 'full'], False),
    ('capacity-preflight', ['scripts/test-capacity-preflight.sh'], False),
    ('numeric-regressions', ['scripts/test-numeric-regressions.sh'], False),
    ('tester', ['scripts/test-install-tester.sh'], True),
    ('manager-staging', ['bash', 'scripts/test-manager-staging.sh'], True),
    ('image-build', ['bash', 'scripts/test-managed-image-build.sh'], True),
    ('healthcheck', ['scripts/test-healthcheck.sh'], True),
    ('systemd-rollback', ['bash', 'scripts/test-systemd-rollback.sh'], True),
    ('controller-installer', ['scripts/test-install-worker-controller.sh'], True),
    ('deployer-installer', ['scripts/test-install-deployer.sh'], True),
    ('deployer-adapter', ['python3', 'scripts/test_deployer_adapter.py'], False),
    ('deployer-units', ['scripts/test-deployer-units.sh'], True),
    ('status-installer', ['scripts/test-install-status-receiver.sh'], True),
]
PRINT_LOCK = threading.Lock()
SKIPPED = re.compile(rb'(?:skipped=[1-9][0-9]*|\.\.\. skipped )')


def require_registered_task(expected):
    if TASKS.count(expected) != 1:
        raise ValueError('validation registry must contain the exact required task once')
    dispatch = 'validation_command regressions python3 "$(dirname "${BASH_SOURCE[0]}")/validation_tasks.py" "${task_args[@]}"'
    lines = Path(__file__).with_name('validate.sh').read_text().splitlines()
    if dispatch not in (line.strip() for line in lines):
        raise ValueError('validate.sh does not dispatch the validation registry')


def record(event, **fields):
    value = {'event': event, 'timestamp': datetime.now(timezone.utc).isoformat(), **fields}
    with PRINT_LOCK:
        try:
            print('VALIDATION_TASK ' + json.dumps(value, sort_keys=True), flush=True)
        except OSError:
            pass  # Progress delivery cannot change the test's exit status.


def protected(path):
    for entry in (path, *path.parents):
        mode = entry.lstat()
        if entry.is_symlink() or mode.st_uid != os.geteuid() or mode.st_mode & 0o022:
            raise ValueError('unsafe validation resume evidence')


def cached_result(directory, name, key):
    receipt = directory / (name + '.json')
    if not receipt.exists():
        return None
    protected(receipt)
    value = json.loads(receipt.read_text())
    if value.get('key') != key or value.get('status') != 'passed':
        return None
    if value.get('task') != name or value.get('exit_code') != 0:
        raise ValueError('invalid validation resume receipt')
    log = directory / (name + '.log')
    protected(log)
    raw = log.read_bytes()
    if hashlib.sha256(raw).hexdigest() != value.get('sha256') or SKIPPED.search(raw):
        raise ValueError('invalid validation resume log')
    return value, raw


def run_task(task, root, output, context=None, resume=None):
    name, command, _ = task
    key = hashlib.sha256(json.dumps([context, name, command], sort_keys=True).encode()).hexdigest()
    log = output / (name + '.log')
    began = time.monotonic()
    previous = cached_result(resume, name, key) if resume is not None and context else None
    if previous:
        value, raw = previous
        log.write_bytes(raw)
        value = dict(value, reused=True, elapsed_seconds=0)
        record('reused', task=name, sha256=value['sha256'])
    else:
        record('begin', task=name)
        with log.open('wb') as stream, subprocess.Popen(
                command, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as process:
            progress = Progress()
            for chunk in iter(lambda: process.stdout.read1(65536), b''):
                stream.write(chunk)
                # Relay only fixed diagnostic records; complete output stays in the artifact.
                with PRINT_LOCK:
                    progress.feed(chunk)
            code = process.wait()
        raw = log.read_bytes()
        if code == 0 and SKIPPED.search(raw):
            code = 1
        value = {'task': name, 'key': key, 'status': 'passed' if code == 0 else 'failed',
                 'exit_code': code, 'elapsed_seconds': round(time.monotonic() - began, 6),
                 'sha256': hashlib.sha256(raw).hexdigest(), 'reused': False}
        record('end', task=name, status=value['status'], exit_code=code,
               elapsed_seconds=value['elapsed_seconds'], sha256=value['sha256'])
        if code:
            with PRINT_LOCK:
                print(raw[-8192:].decode('utf-8', errors='replace'), file=sys.stderr, flush=True)
    (output / (name + '.json')).write_text(json.dumps(value, indent=2) + '\n')
    return value


def execute(tasks, root, output, *, context=None, resume=None, budget=resources):
    if len({task[0] for task in tasks}) != len(tasks) or any(not re.fullmatch(r'[a-z0-9-]+', task[0]) for task in tasks):
        raise ValueError('invalid or duplicate validation task')
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    pending, running, results = list(tasks), {}, []
    failed = False
    initial = budget()
    record('resources', **initial)
    try:
        with ThreadPoolExecutor(max_workers=initial['workers']) as pool:
            while running or (pending and not failed):
                limit = min(initial['workers'], budget()['workers'])
                while pending and not failed and len(running) < limit:
                    task = pending[0]
                    if any(active[2] for active in running.values()) or (task[2] and running):
                        break
                    pending.pop(0)
                    future = pool.submit(run_task, task, root, output, context, resume)
                    running[future] = task
                    if task[2]:
                        break
                if not running:
                    break
                completed, _ = wait(running, timeout=30, return_when=FIRST_COMPLETED)
                if not completed:
                    record('running', tasks=[task[0] for task in running.values()], **budget())
                for future in completed:
                    running.pop(future)
                    result = future.result()
                    results.append(result)
                    failed |= result['status'] != 'passed'
    finally:
        summary = {'planned': [task[0] for task in tasks], 'results': results,
                   'not_run': [task[0] for task in pending], 'resource_budget': initial,
                   'status': 'passed' if {r['task'] for r in results} == {t[0] for t in tasks}
                   and len(results) == len(tasks) and not failed else 'failed'}
        (output / 'result.json').write_text(json.dumps(summary, indent=2) + '\n')
    return 0 if summary['status'] == 'passed' else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--task', choices=[task[0] for task in TASKS])
    parser.add_argument('--output', type=Path)
    parser.add_argument('--resume-from', type=Path)
    args = parser.parse_args()
    context = os.environ.get('CI_FLEET_VALIDATION_CONTEXT')
    if args.resume_from and not context:
        parser.error('resume requires the trusted exact-source validation context')
    tasks = [task for task in TASKS if args.task is None or task[0] == args.task]
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    output = args.output or Path(tempfile.mkdtemp(prefix='ci-fleet-regression-')) / 'tasks'
    record('artifacts', directory=str(output))
    return execute(tasks, args.root, output, context=context, resume=args.resume_from)


if __name__ == '__main__':
    raise SystemExit(main())
