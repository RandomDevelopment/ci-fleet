#!/usr/bin/env python3
"""Expose a verified deployer volume to a host-local proxy on Linux."""
import argparse
import os
from pathlib import Path
import stat
import subprocess


MOUNT_FLAGS = {'ro', 'nosuid', 'nodev', 'noexec', 'nosymfollow'}


def mount_ingress(source: Path, target: Path) -> None:
    if any(path.anchor != '/' or '..' in path.parts for path in (source, target)):
        raise ValueError('mount paths must be absolute and contain no traversal')
    if source.name != '_data' or not source.parent.name.startswith('ci-fleet-deploy-') or source.parent.parent.name != 'volumes':
        raise ValueError('source must be the data directory of a scoped Docker volume')
    for path in (*reversed(target.parents), target):
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError('proxy mount target and ancestors must be root-owned directories without group or other write access')
    mounted = subprocess.run(['mountpoint', '-q', '--', str(target)], check=False)
    if mounted.returncode != 32:
        raise ValueError('proxy mount target must be an unmounted directory')
    parent_propagation = subprocess.check_output(
        ['findmnt', '-n', '-o', 'PROPAGATION', '--target', str(target.parent)], text=True,
    ).strip()
    if parent_propagation != 'private':
        raise ValueError('proxy route must be private before attaching the volume')

    # The rootless daemon can replace path components. Open each without
    # following symlinks and bind the pinned directory, never the original path.
    flags = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open('/', flags)
    try:
        for part in source.parts[1:]:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        original = os.fstat(fd)
        subprocess.run(['mount', '--no-canonicalize', '--bind', f'/proc/self/fd/{fd}', str(target)],
                       check=True, pass_fds=(fd,))
    finally:
        os.close(fd)

    try:
        subprocess.run(['mount', '--make-private', str(target)], check=True)
        subprocess.run(['mount', '-o', 'remount,bind,ro,nosuid,nodev,noexec,nosymfollow', str(target)], check=True)
        options, propagation = subprocess.check_output(
            ['findmnt', '-n', '-o', 'VFS-OPTIONS,PROPAGATION', '--mountpoint', str(target)], text=True,
        ).split()
        current = target.stat()
        if (not MOUNT_FLAGS <= set(options.split(',')) or propagation != 'private'
                or (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino)):
            raise ValueError('proxy mount source or flags could not be verified')
    except BaseException:
        subprocess.run(['umount', str(target)], check=True)
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path, help='operator-verified scoped volume Mountpoint')
    parser.add_argument('target', type=Path, help='unmounted directory below the protected proxy route')
    args = parser.parse_args()
    try:
        mount_ingress(args.source, args.target)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, f'{error}\n')
