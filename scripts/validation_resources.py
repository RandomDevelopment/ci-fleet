#!/usr/bin/env python3
"""Bound local validation concurrency by this invocation's CPU and memory limits."""
from __future__ import annotations

import os
from pathlib import Path

MEMORY_PER_TASK = 512 * 1024 * 1024
PROCESSES_PER_TASK = 128


def resources(proc: Path = Path('/proc'), cgroups: Path = Path('/sys/fs/cgroup')) -> dict:
    try:
        cpus = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        cpus = os.cpu_count() or 1
    available = None
    process_slots = None
    try:
        for line in (proc / 'meminfo').read_text().splitlines():
            if line.startswith('MemAvailable:'):
                available = int(line.split()[1]) * 1024
                break
        location = next(line.split('::', 1)[1] for line in
                        (proc / 'self/cgroup').read_text().splitlines() if line.startswith('0::'))
        if not location.startswith('/') or '..' in Path(location).parts:
            raise ValueError('invalid unified cgroup path')
        current = cgroups / location.lstrip('/')
        if not current.is_dir():
            raise ValueError('unavailable cgroup hierarchy')
        while True:
            try:
                quota, period = (current / 'cpu.max').read_text().split()
                if quota != 'max':
                    quota, period = int(quota), int(period)
                    if quota <= 0 or period <= 0:
                        raise ValueError('invalid cgroup CPU quota')
                    cpus = min(cpus, max(1, quota // period))
            except FileNotFoundError:
                pass
            try:
                maximum = (current / 'pids.max').read_text().strip()
            except FileNotFoundError:
                maximum = 'max'
            if maximum != 'max':
                remaining = max(0, int(maximum) - int((current / 'pids.current').read_text()))
                slots = max(1, (remaining - 32) // PROCESSES_PER_TASK)
                process_slots = slots if process_slots is None else min(process_slots, slots)
            try:
                maximum = (current / 'memory.max').read_text().strip()
            except FileNotFoundError:
                maximum = 'max'
            if maximum != 'max':
                remaining = max(0, int(maximum) - int((current / 'memory.current').read_text()))
                available = remaining if available is None else min(available, remaining)
            if current == cgroups:
                break
            current = current.parent
    except (OSError, StopIteration, ValueError):
        # Unknown restrictions cannot justify parallel execution.
        cpus = 1
    memory_slots = max(1, available * 3 // 4 // MEMORY_PER_TASK) if available is not None else 1
    return {'cpu_slots': max(1, cpus), 'available_memory_bytes': available,
            'memory_slots': memory_slots, 'process_slots': process_slots,
            'workers': max(1, min(cpus, memory_slots, process_slots or cpus)),
            'estimated_memory_per_task_bytes': MEMORY_PER_TASK}
