#!/usr/bin/env python3
"""Keep exact fake-host immutability checks when replacing comparison copies."""
import os
from pathlib import Path
import tempfile
import unittest

from fixture_manifest import manifest


class ManifestTests(unittest.TestCase):
    def test_changes_to_bytes_modes_links_and_entry_set_are_detected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            file = root / 'file'; file.write_text('before')
            file.chmod(0o644)
            link = root / 'link'; link.symlink_to('missing')
            original = manifest(root)
            file.write_text('after')
            self.assertNotEqual(manifest(root), original)
            file.write_text('before'); file.chmod(0o600)
            self.assertNotEqual(manifest(root), original)
            file.chmod(0o644); link.unlink(); link.symlink_to('elsewhere')
            self.assertNotEqual(manifest(root), original)
            link.unlink(); link.symlink_to('missing'); file.unlink(); file.mkdir()
            self.assertNotEqual(manifest(root), original)
            file.rmdir(); file.write_text('before'); (root / 'empty-directory').mkdir()
            self.assertNotEqual(manifest(root), original)

    def test_dangling_links_are_recorded_without_following_their_targets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'outside').symlink_to('/unavailable-target')
            self.assertEqual(manifest(root)['outside'][2], '/unavailable-target')

    def test_special_entries_fail_instead_of_disappearing_from_comparison(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            os.mkfifo(root / 'pipe')
            with self.assertRaisesRegex(RuntimeError, 'unsupported'):
                manifest(root)


if __name__ == '__main__':
    unittest.main()
