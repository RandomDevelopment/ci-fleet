#!/usr/bin/env python3
"""Check concurrency against varying VM, affinity and nested cgroup limits."""
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from validation_resources import resources


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.proc = self.root / 'proc'
        (self.proc / 'self').mkdir(parents=True)
        (self.proc / 'meminfo').write_text('MemAvailable: 8388608 kB\n')
        (self.proc / 'self/cgroup').write_text('0::/parent/job\n')
        self.groups = self.root / 'cgroup'
        (self.groups / 'parent/job').mkdir(parents=True)

    def budget(self, cpus):
        with patch('validation_resources.os.sched_getaffinity', return_value=set(range(cpus))):
            return resources(self.proc, self.groups)

    def test_each_vm_uses_its_own_affinity_and_memory(self):
        self.assertEqual(self.budget(1)['workers'], 1)
        self.assertEqual(self.budget(4)['workers'], 4)
        self.assertEqual(self.budget(16)['workers'], 12)
        (self.proc / 'meminfo').write_text('MemAvailable: 1048576 kB\n')
        self.assertEqual(self.budget(16)['workers'], 1)

    def test_parent_quota_limits_child_even_when_child_is_unlimited(self):
        (self.groups / 'parent/job/cpu.max').write_text('max 100000\n')
        (self.groups / 'parent/cpu.max').write_text('250000 100000\n')
        self.assertEqual(self.budget(16)['workers'], 2)
        (self.groups / 'parent/job/cpu.max').write_text('50000 100000\n')
        self.assertEqual(self.budget(16)['workers'], 1)

    def test_parent_remaining_memory_is_stricter_than_vm_free_memory(self):
        (self.groups / 'parent/memory.max').write_text(str(2 * 1024**3))
        (self.groups / 'parent/memory.current').write_text(str(1024**3))
        self.assertEqual(self.budget(8)['workers'], 1)

    def test_changed_vm_limits_are_read_again_for_the_next_invocation(self):
        self.assertEqual(self.budget(8)['workers'], 8)
        (self.groups / 'parent/cpu.max').write_text('100000 100000\n')
        self.assertEqual(self.budget(8)['workers'], 1)

    def test_process_headroom_limits_concurrency(self):
        (self.groups / 'parent/pids.max').write_text('512')
        (self.groups / 'parent/pids.current').write_text('32')
        self.assertEqual(self.budget(8)['workers'], 3)
        (self.groups / 'parent/pids.current').write_text('400')
        self.assertEqual(self.budget(8)['workers'], 1)

    def test_unknown_or_malformed_limits_use_serial_execution(self):
        (self.proc / 'self/cgroup').unlink()
        self.assertEqual(self.budget(16)['workers'], 1)
        (self.proc / 'self/cgroup').write_text('0::/../../outside\n')
        self.assertEqual(self.budget(16)['workers'], 1)
        (self.proc / 'self/cgroup').write_text('0::/parent/job\n')
        (self.groups / 'parent/cpu.max').write_text('invalid quota\n')
        self.assertEqual(self.budget(16)['workers'], 1)

    def test_finite_limit_without_current_usage_is_unknown(self):
        (self.groups / 'parent/pids.max').write_text('512')
        self.assertEqual(self.budget(16)['workers'], 1)
        (self.groups / 'parent/pids.max').unlink()
        (self.groups / 'parent/memory.max').write_text(str(8 * 1024**3))
        self.assertEqual(self.budget(16)['workers'], 1)


if __name__ == '__main__':
    unittest.main()
