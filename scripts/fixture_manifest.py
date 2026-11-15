#!/usr/bin/env python3
"""Fingerprint fixture state without copying the fake host for comparison."""
import json
from pathlib import Path
import sys

from local_validate import checkout_manifest


def manifest(root):
    entries = checkout_manifest(root)
    for name, value in entries.items():
        metadata = (root / name).lstat()
        entries[name] = (*value, metadata.st_uid, metadata.st_gid)
    return entries


if __name__ == '__main__':
    print(json.dumps(manifest(Path(sys.argv[1])), sort_keys=True))
