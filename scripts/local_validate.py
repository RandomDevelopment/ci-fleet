#!/usr/bin/env python3
"""Run a pinned validation suite against explicit Git identities without Actions."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time

SHA = re.compile(r"[0-9a-f]{40}")
SUITE = "ci-fleet-local-v1"


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def run(command: list[str], cwd: Path, env: dict, log: Path, timeout: int) -> None:
    with log.open("wb") as output:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdout=output, stderr=subprocess.STDOUT,
                                   start_new_session=True)
        try:
            code = process.wait(timeout=timeout)
            if code:
                raise RuntimeError(f"check exited {code}; see {log}")
        finally:
            # Descendants must not outlive a failed or cancelled check.
            try:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                finally:
                    os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def validate(args: argparse.Namespace) -> int:
    repo = args.repository.resolve()
    policy_repo = (args.policy_repository or repo).resolve()
    reviewed_head = args.reviewed_head or args.source
    for origin, value in ((repo, args.source), (repo, reviewed_head), (repo, args.base), (policy_repo, args.policy), (repo, args.main)):
        if not SHA.fullmatch(value) or git(origin, "rev-parse", value + "^{commit}") != value:
            raise ValueError("source, base, policy and main must be full available commit SHAs")
    if git(repo, "rev-parse", "--is-shallow-repository") != "false":
        raise ValueError("complete Git history is required")
    subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", args.base, args.source], check=True)
    if reviewed_head != args.source:
        subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", args.base, reviewed_head], check=True)
        if git(repo, "rev-parse", args.source + "^{tree}") != git(repo, "rev-parse", reviewed_head + "^{tree}"):
            raise ValueError("integrated and reviewed source trees differ")
    output = args.output.resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    report = {"schema_version": 1, "suite": SUITE if args.kind == "engine" else "ci-fleet-config-local-v1", "profile": args.profile,
              "source": args.source, "reviewed_head": reviewed_head, "tree": git(repo, "rev-parse", args.source + "^{tree}"),
              "base": args.base, "policy": args.policy, "main": args.main,
              "pr_title": args.pr_title, "tag": args.tag, "tag_created": args.tag_created,
              "started_at": int(time.time()), "expected_skips": [], "tools": {}, "results": [], "status": "incomplete"}
    if args.profile == "quick" and args.kind == "engine":
        report["expected_skips"] = ["container-build-and-cleanup"]
    try:
        with tempfile.TemporaryDirectory(prefix="ci-fleet-validation-", dir=Path.home()) as temporary, \
                tempfile.TemporaryDirectory(prefix="ci-fleet-fixtures-", dir="/tmp") as fixture_temporary:
            root = Path(temporary)
            # Permission fixtures deliberately drop UID and chmod their own directories.
            Path(fixture_temporary).chmod(0o755)
            report["run_id"] = root.name.lower()
            source, policy = root / "source", root / "policy"
            for target, commit, origin in ((source, args.source, repo), (policy, args.policy, policy_repo)):
                subprocess.run(["git", "clone", "--quiet", "--no-local", "--no-checkout", str(origin), str(target)], check=True)
                identities = [args.source, args.base, args.main, reviewed_head] if target == source else [args.policy]
                subprocess.run(["git", "-C", str(target), "fetch", "--quiet", "--no-tags", "origin", *identities], check=True)
                subprocess.run(["git", "-C", str(target), "checkout", "--quiet", "--detach", commit], check=True)
                if target == source:
                    subprocess.run(["git", "-C", str(target), "update-ref", "refs/remotes/origin/main", args.main], check=True)
            # Do not pass host credentials, Git overrides or language injection variables.
            env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": str(root / "home"), "LANG": "C.UTF-8",
                   "TMPDIR": fixture_temporary, "PYTHONNOUSERSITE": "1", "GIT_CONFIG_NOSYSTEM": "1",
                   "CI_FLEET_VALIDATION_ROOT": str(source), "CI_FLEET_VALIDATION_ID": root.name.lower(),
                   "CI_FLEET_RUNNER_IMAGE": "ci-fleet-runner:" + root.name.lower(),
                   "CI_FLEET_CONTROLLER_IMAGE": "ci-fleet-controller:" + root.name.lower()}
            Path(env["HOME"]).mkdir()
            tools = {"git": ["git", "--version"], "python": ["python3", "--version"],
                     "bash": ["bash", "--version"], "openssl": ["openssl", "version"], "shellcheck": ["shellcheck", "--version"]}
            if args.kind == "engine":
                if not args.disposable_host:
                    raise ValueError("engine validation requires --disposable-host on an isolated disposable Docker host")
                if args.profile == "full":
                    tools.update(docker=["docker", "version"], compose=["docker", "compose", "version"])
                # Fixtures and Docker access must never reach an active installed fleet.
                if any(Path(p).exists() for p in ("/etc/ci-fleet", "/var/lib/ci-fleet/install-state.json")):
                    raise ValueError("validation refuses an installed fleet host")
            if args.kind == "engine" and (os.geteuid() != 0 or not Path("/dev/shm").is_dir()):
                raise ValueError("complete fixture coverage requires root and /dev/shm on an isolated execution host")
            for name, command in tools.items():
                report["tools"][name] = subprocess.check_output(command, env=env, text=True, stderr=subprocess.STDOUT).strip()
            validator = str(policy / "scripts/validate_commits.py")
            scanner = str(policy / "scripts/scan_committed_secrets.py")
            checks = []
            if args.pr_title is not None:
                checks.append(("pr-title", ["python3", validator, "--pr-title", args.pr_title]))
            history_base = args.base
            if args.tag:
                if not args.tag_created:
                    raise ValueError("published tags must not be moved; supply --tag-created for a new tag")
                history_base = subprocess.check_output(["python3", validator, "--release-base-for", args.source,
                                                       "--exclude-tag", args.tag], cwd=source, env=env, text=True).strip()
                checks.append(("tag", ["python3", validator, "--version", args.tag, "--tag-commit", args.source,
                                       "--base", history_base, "--head", args.source, "--exclude-tag", args.tag]))
            report["history_base"] = history_base
            heads = list(dict.fromkeys((args.source, reviewed_head)))
            checks.append(("commits", ["python3", "-c",
                "import subprocess, sys; [subprocess.run([sys.executable, sys.argv[1], '--base', sys.argv[2], '--head', h], check=True) for h in sys.argv[3:]]",
                validator, history_base, *heads]))
            commits = []
            for head in heads:
                commits.extend(git(source, "rev-list", "--reverse", history_base + ".." + head if history_base else head).splitlines())
                commits.append(head)
            commits = list(dict.fromkeys(commits))
            checks.append(("history-secrets", ["python3", "-c",
                "import subprocess, sys; [subprocess.run([sys.executable, sys.argv[1], '--repository', sys.argv[2], '--commit', c], check=True) for c in sys.argv[3:]]",
                scanner, str(source), *commits]))
            if args.kind == "engine":
                checks.extend([
                    ("shellcheck", ["bash", "-c", "shellcheck scripts/*.sh examples/project/scripts/ci/*.sh templates/config-repository/scripts/*.sh"]),
                    ("documentation", ["python3", str(policy / "scripts/check_doc_links.py")]),
                    ("tests-and-fixtures", ["bash", str(policy / "scripts/validate.sh"), "--quick"]),
                    ("embedded-template", ["bash", str(policy / "scripts/validate-template.sh")]),
                ])
                if args.profile == "full":
                    checks.append(("container-build-and-cleanup", ["bash", str(policy / "scripts/validate.sh"), "--containers"]))
            else:
                env["CI_FLEET_VALIDATION_BASE"] = args.base
                checks.append(("configuration", ["bash", str(policy / "scripts/validate-configuration.sh")]))
            for name, command in checks:
                entry = {"name": name, "status": "incomplete", "log": name + ".log", "started_at": int(time.time())}
                report["results"].append(entry)
                print("Running " + name, flush=True)
                try:
                    run(command, source, env, output / entry["log"], args.timeout)
                    if re.search(rb"(?:skipped=[1-9][0-9]*|\.\.\. skipped )", (output / entry["log"]).read_bytes()):
                        raise RuntimeError("unexpected skipped tests; see " + entry["log"])
                    entry["status"] = "passed"
                except (Exception, KeyboardInterrupt):
                    entry["status"] = "failed"
                    if (output / entry["log"]).exists():
                        tail = (output / entry["log"]).read_bytes().splitlines()[-40:]
                        print(b"\n".join(tail).decode("utf-8", errors="replace"), file=sys.stderr)
                    raise
                finally:
                    entry["finished_at"] = int(time.time())
                    if (output / entry["log"]).exists():
                        entry["sha256"] = hashlib.sha256((output / entry["log"]).read_bytes()).hexdigest()
            report["status"] = "passed"
    except (Exception, KeyboardInterrupt) as error:
        report["status"] = "failed"
        report["error"] = str(error)
        print(str(error), file=sys.stderr)
    finally:
        report["finished_at"] = int(time.time())
        (output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    return 0 if report["status"] == "passed" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=("engine", "configuration"), default="engine")
    parser.add_argument("--policy-repository", type=Path)
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    for name in ("source", "base", "policy", "main"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--reviewed-head", help="original reviewed head when validating an integrated merge or squash commit")
    parser.add_argument("--pr-title")
    parser.add_argument("--tag")
    parser.add_argument("--tag-created", action="store_true")
    parser.add_argument("--profile", choices=("quick", "full"), default="full")
    parser.add_argument("--disposable-host", action="store_true")
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("timeout must be positive")
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        return validate(args)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
