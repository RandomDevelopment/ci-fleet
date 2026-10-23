#!/usr/bin/env python3
"""Fail-closed policy for explicit, same-engine drained reconciliation.

Called by the locked manager, never a replacement for installer/review validation.
"""
import json
from pathlib import Path
import re
import sys


def verify(candidate_path, baseline_path, state_path, runtime_path, lkg_path):
    candidate, baseline, state = (json.loads(Path(p).read_text()) for p in
                                   (candidate_path, baseline_path, state_path))
    controller = state["controller"]
    old = baseline["controllers"][controller]
    new = candidate["controllers"][controller]
    engine = state["engine_ref"]
    if not re.fullmatch("[0-9a-f]{40}", engine):
        raise ValueError("invalid installed engine")
    if state["controller_state"] != "drained" or old["state"] != "drained" or new["state"] != "drained":
        raise ValueError("explicit recovery requires drained installed and desired state")
    if old != new or old["engine_ref"] != engine:
        raise ValueError("explicit recovery cannot change controller policy or engine")
    if baseline["organization"]["delivery_engine"] != state["engine_repository"]:
        raise ValueError("installed engine repository mismatch")
    if Path(runtime_path, ".ci-fleet-engine-ref").read_text().strip() != engine:
        raise ValueError("current runtime engine mismatch")
    # Only additive image publication for the unchanged engine is recoverable.
    # Existing image identities, other engines, and every other policy stay fixed.
    old_images = baseline.pop("managed_images", {})
    new_images = candidate.pop("managed_images", {})
    if baseline != candidate:
        raise ValueError("explicit recovery cannot change fleet policy")
    if any(new_images.get(key) != value for key, value in old_images.items()):
        raise ValueError("explicit recovery cannot replace reviewed image identities")
    if set(new_images) - set(old_images) - {engine}:
        raise ValueError("explicit recovery cannot publish unrelated engine images")
    metadata = Path(lkg_path)
    if metadata.exists() or metadata.is_symlink():
        lkg = json.loads(metadata.read_text())
        if any(lkg[key] != state[key] for key in ("config_repository", "config_ref", "controller")):
            raise ValueError("explicit recovery requires installed drained baseline as LKG")


def main():
    try:
        verify(*sys.argv[1:])
    except (OSError, ValueError, TypeError, KeyError):
        print("ERROR: explicit drained recovery policy rejected", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
