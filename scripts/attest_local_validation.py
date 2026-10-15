#!/usr/bin/env python3
"""Sign an independently verified full local run on a separate trusted signer."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

from verify_config_approval import (
    LOCAL_EVIDENCE_MAX_BYTES, policy_digest, read_policy, require, require_local_evidence_freshness, unique_object,
)


def read_bounded(path: Path) -> bytes:
    with path.open("rb") as handle:
        data = handle.read(LOCAL_EVIDENCE_MAX_BYTES + 1)
    require(len(data) <= LOCAL_EVIDENCE_MAX_BYTES, "local evidence exceeds size limit")
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True, help="independently verified run directory, not a candidate upload")
    parser.add_argument("--key", type=Path, required=True, help="signer-only Ed25519 private key, never present on execution host")
    parser.add_argument("--reviewed-head", required=True)
    parser.add_argument("--pull-request", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    policy = read_policy(args.policy)
    require(policy["schema_version"] == 2, "local evidence requires policy v2")
    local = policy["local_validation"]
    result = json.loads(read_bounded(args.result / "result.json"), object_pairs_hook=unique_object)
    require(result.get("profile") == "full" and result.get("status") == "passed" and result.get("expected_skips") == []
            and result.get("policy") == local["validator_revision"] and result.get("suite") == local["suite"],
            "a full run of the pinned validator is required")
    require(result.get("reviewed_head") == args.reviewed_head, "reviewed head differs from the executed suite")
    require_local_evidence_freshness(result, local["max_age_seconds"])
    checks = result["results"]
    require(len(checks) == len(local["required_checks"]) and {c["name"] for c in checks} == set(local["required_checks"]),
            "local check coverage differs")
    for check in checks:
        require(check["status"] == "passed" and hashlib.sha256(
            read_bounded(args.result / (check["name"] + ".log"))).hexdigest() == check["sha256"], "local result/log differs")
    payload = json.dumps({"schema_version": 1, "policy_sha256": policy_digest(policy),
                          "repository_id": policy["configuration_repository_id"], "branch": policy["branch"],
                          "pull_request": args.pull_request, "reviewed_head": args.reviewed_head, "result": result},
                         sort_keys=True, separators=(",", ":")).encode()
    encoded_payload = base64.b64encode(payload).decode()
    envelope = {"payload": encoded_payload, "signature": base64.b64encode(bytes(64)).decode()}
    require(len((json.dumps(envelope, sort_keys=True) + "\n").encode()) <= LOCAL_EVIDENCE_MAX_BYTES,
            "local evidence envelope exceeds size limit")
    with tempfile.TemporaryDirectory(prefix="ci-fleet-sign-") as temporary:
        root = Path(temporary)
        (root / "payload").write_bytes(payload)
        subprocess.run(["openssl", "pkeyutl", "-sign", "-rawin", "-inkey", str(args.key), "-in", str(root / "payload"),
                        "-out", str(root / "signature")], check=True, timeout=10,
                       env={"PATH": "/usr/bin:/bin", "OPENSSL_CONF": "/dev/null"})
        envelope["signature"] = base64.b64encode((root / "signature").read_bytes()).decode()
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(envelope, handle, sort_keys=True)
        handle.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
