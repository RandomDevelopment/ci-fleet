#!/usr/bin/env python3
"""Execute validation stages through the system manager on a disposable worker."""
from __future__ import annotations

import argparse
import grp
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid

USER = "ci-fleet-validation"
INSTALL = Path("/usr/local/lib/ci-fleet-validation")
WORK = Path("/var/lib/ci-validation-work")
HOST_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}
# ExecStopPost runs after systemd has terminated the stage's remaining processes.
# Its receipt lives in a root-only directory, outside candidate-writable paths.
FINISH = '''import json, os
from pathlib import Path
result = {key: os.environ.get(key) for key in ("SERVICE_RESULT", "EXIT_CODE", "EXIT_STATUS")}
with Path(__file__).with_name("result.json").open("x") as output:
    json.dump(result, output)
'''


def require_revision(revision: str) -> None:
    """Bind consumers to the reviewed executor installed by worker preparation."""
    program = INSTALL / "executor.py"
    manifest = INSTALL / "revision.json"
    for path in (program, manifest, INSTALL, *INSTALL.parents):
        mode = path.lstat()
        if path.is_symlink() or mode.st_uid != 0 or mode.st_mode & 0o022:
            raise RuntimeError("unsafe installed validation executor")
    identity = json.loads(manifest.read_text())
    if (Path(__file__) != program or not re.fullmatch(r"[0-9a-f]{40}", revision)
            or identity != {"revision": revision, "sha256": hashlib.sha256(program.read_bytes()).hexdigest()}):
        raise RuntimeError("prepare the worker with the exact reviewed executor revision " + revision)


def require_worker() -> None:
    if os.geteuid() != 0:
        raise RuntimeError("the trusted validation launcher requires root on a disposable execution worker")
    if not Path("/run/systemd/system").is_dir() or not Path("/sys/fs/cgroup/cgroup.controllers").is_file():
        raise RuntimeError("validation requires the system systemd manager and cgroup v2")
    for tool in ("systemd-run", "systemctl"):
        if shutil.which(tool, path=HOST_ENV["PATH"]) is None:
            raise RuntimeError(tool + " is required")
    version = subprocess.check_output(["systemd-run", "--version"], env=HOST_ENV, text=True)
    if int(version.split()[1]) < 255:
        raise RuntimeError("validation requires systemd 255 or newer")


def job_account():
    """Refuse inherited host privileges on the dedicated validation account."""
    try:
        account = pwd.getpwnam(USER)
        group = grp.getgrgid(account.pw_gid)
    except KeyError as error:
        raise RuntimeError("prepare the disposable worker before running configuration tests") from error
    if (account.pw_uid == 0 or account.pw_gid == 0 or account.pw_shell != "/usr/sbin/nologin"
            or group.gr_name != USER or set(group.gr_mem) - {USER}
            or set(os.getgrouplist(USER, account.pw_gid)) != {account.pw_gid}):
        raise RuntimeError("validation job account requires its private group and no supplementary groups")
    return account


def prepare_stage(writable: list[Path], readonly: list[Path]) -> None:
    """Prepare job-owned copies; the service mounts trusted inputs read-only."""
    account = job_account()
    for root in (*readonly, *writable):
        for path in (root, *root.rglob("*")):
            os.chown(path, account.pw_uid, account.pw_gid, follow_symlinks=False)


def run(command: list[str], cwd: Path, env: dict, log: Path, timeout: int,
        grace: float = 60, *, user: str = "root") -> None:
    """Wait for the stage result and verified cgroup cleanup before returning."""
    if timeout <= 0 or grace < 0 or user not in ("root", USER):
        raise ValueError("invalid stage deadline, cleanup grace or execution user")
    if not command or any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) for key in env):
        raise ValueError("invalid stage command or environment")
    require_worker()
    if user == USER:
        job_account()
    unit = "ci-validation-" + uuid.uuid4().hex + ".service"
    cgroup = Path("/sys/fs/cgroup/system.slice") / unit
    previous, cancelled = {}, []
    process = None
    started = time.monotonic()

    def cancel(signum, _frame):
        if not cancelled:
            cancelled.append(signum)

    def stop():
        return subprocess.run(["systemctl", "--system", "stop", unit], env=HOST_ENV,
                              stdin=subprocess.DEVNULL, capture_output=True, timeout=grace + 15).returncode == 0

    with tempfile.TemporaryDirectory(prefix=unit.removesuffix(".service") + "-", dir="/run") as temporary:
        root = Path(temporary)
        finish = root / "finish.py"
        finish.write_text(FINISH)
        properties = ["Type=exec", "ExitType=cgroup", "Slice=system.slice", "Restart=no", "Delegate=no",
                      "KillMode=control-group", "SendSIGKILL=yes", "RuntimeMaxSec=" + str(timeout) + "s",
                      "TimeoutStopSec=" + str(max(grace, .001)) + "s", "TasksMax=512",
                      "User=" + user, "ExecStopPost=+/usr/bin/python3 -I " + str(finish)]
        if user != "root":
            writable = list(dict.fromkeys([str(cwd), *(env[key] for key in ("HOME", "TMPDIR") if key in env)]))
            if any(not Path(path).is_absolute() for path in writable):
                raise ValueError("job writable paths must be absolute")
            properties += ["NoNewPrivileges=yes", "CapabilityBoundingSet=", "AmbientCapabilities=",
                           "ProtectSystem=strict", "ReadOnlyPaths=/run", "PrivateTmp=yes",
                           "ReadWritePaths=" + " ".join(json.dumps(path, ensure_ascii=False) for path in writable),
                           "ProtectControlGroups=yes", "RestrictSUIDSGID=yes", "RestrictNamespaces=yes",
                           "InaccessiblePaths=-/run/systemd/private -/run/dbus/system_bus_socket -/run/user -/run/docker.sock -/var/run/docker.sock"]
        launch = ["systemd-run", "--system", "--quiet", "--wait", "--pipe", "--expand-environment=no",
                  "--unit=" + unit, "--working-directory=" + str(cwd),
                  *("--property=" + value for value in properties),
                  "/usr/bin/env", "-i", *(key + "=" + value for key, value in env.items()), *command]
        try:
            if threading.current_thread() is threading.main_thread():
                for signum in (signal.SIGINT, signal.SIGTERM):
                    if signal.getsignal(signum) != signal.SIG_IGN:
                        previous[signum] = signal.signal(signum, cancel)
            with log.open("wb") as output:
                process = subprocess.Popen(launch, env=HOST_ENV, stdin=subprocess.DEVNULL,
                                           stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
                stopping = False
                last_probe = started - 1
                while process.poll() is None:
                    if cancelled and not stopping:
                        stopping = stop()
                    now = time.monotonic()
                    if not stopping and now - last_probe >= .5:
                        last_probe = now
                        state = subprocess.run(["systemctl", "--system", "show", unit, "--property=MainPID",
                                                "--property=ExecMainCode", "--property=ActiveState"], env=HOST_ENV,
                                               capture_output=True, text=True, timeout=5)
                        fields = dict(line.split("=", 1) for line in state.stdout.splitlines() if "=" in line)
                        # PID 1 owns the cgroup even after its original main exits.
                        # Request cleanup then, without killing a child's cleanup helper early.
                        if (fields.get("ActiveState") == "active" and fields.get("MainPID") == "0"
                                and fields.get("ExecMainCode") not in (None, "0")):
                            stopping = stop()
                    if now - started > timeout + grace + 30:
                        raise RuntimeError("system manager did not complete stage cleanup; discard the worker")
                    try:
                        process.wait(timeout=.1)
                    except subprocess.TimeoutExpired:
                        pass
        except BaseException:
            stop()
            raise
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()
        try:
            result = json.loads((root / "result.json").read_text())
            if not isinstance(result, dict):
                raise RuntimeError("invalid system manager receipt")
            events = (cgroup / "cgroup.events").read_text() if cgroup.exists() else "populated 0\n"
            if "populated 0" not in events.splitlines():
                raise RuntimeError("stage cgroup is not empty; discard the worker")
            if cancelled:
                raise RuntimeError("check cancelled by signal " + str(cancelled[0]) + "; see " + str(log))
            if result.get("EXIT_CODE") == "exited" and result.get("EXIT_STATUS") not in (None, "0"):
                raise RuntimeError("check exited " + str(result["EXIT_STATUS"]) + "; see " + str(log))
            if result["SERVICE_RESULT"] == "timeout":
                if time.monotonic() - started >= timeout:
                    raise subprocess.TimeoutExpired(command, timeout)
                raise RuntimeError("check required forced termination; see " + str(log))
            if (process.returncode != 0 or result != {"SERVICE_RESULT": "success", "EXIT_CODE": "exited", "EXIT_STATUS": "0"}):
                raise RuntimeError("check exited unsuccessfully: " + str(result) + "; see " + str(log))
        except (OSError, ValueError, KeyError) as error:
            raise RuntimeError("missing or invalid system manager receipt; see " + str(log)) from error
        finally:
            subprocess.run(["systemctl", "--system", "reset-failed", unit], env=HOST_ENV,
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=10)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true", required=True)
    parser.add_argument("--disposable-host", action="store_true", required=True)
    parser.parse_args()
    require_worker()
    roles = ("ci-fleet", "ci-fleet-tester", "ci-fleet-deployer", "ci-fleet-status", "ci-fleet-signing")
    markers = [Path(parent, role) for parent in ("/etc", "/var/lib", "/opt") for role in roles]
    markers += [Path("/srv/ci-fleet-signing"), Path("/var/lib/ci-fleet-status-installer"),
                Path("/var/lib/ci-fleet-deployer-container-credentials")]
    for parent in ("/etc/systemd/system", "/run/systemd/system", "/usr/lib/systemd/system"):
        for pattern in ("ci-fleet-*", "actions.runner*"):
            markers.extend(Path(parent).rglob(pattern))
    if any(path.exists() or path.is_symlink() for path in markers):
        raise RuntimeError("worker preparation refuses an installed fleet, application role or signing authority")
    try:
        pwd.getpwnam(USER)
    except KeyError:
        subprocess.run(["useradd", "--system", "--user-group", "--no-create-home", "--shell", "/usr/sbin/nologin", USER],
                       env=HOST_ENV, check=True)
    job_account()
    source = Path(__file__).resolve()
    repository = source.parents[1]
    git_env = {**HOST_ENV, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
               "GIT_NO_REPLACE_OBJECTS": "1", "GIT_GRAFT_FILE": "/dev/null"}
    git = ["git", "-c", "safe.directory=" + str(repository), "-C", str(repository)]
    revision = subprocess.check_output([*git, "rev-parse", "HEAD"], env=git_env, text=True).strip()
    program = subprocess.check_output([*git, "show", revision + ":scripts/" + source.name], env=git_env)
    if not re.fullmatch(r"[0-9a-f]{40}", revision) or program != source.read_bytes():
        raise RuntimeError("prepare from a clean, committed, independently reviewed engine checkout")
    WORK.mkdir(mode=0o711, parents=True, exist_ok=True)
    if WORK.is_symlink() or WORK.stat().st_uid != 0 or WORK.stat().st_mode & 0o022:
        raise RuntimeError("unsafe validation workspace parent")
    WORK.chmod(0o711)
    INSTALL.mkdir(mode=0o755, parents=True, exist_ok=True)
    for path in (INSTALL, *INSTALL.parents):
        mode = path.lstat()
        if path.is_symlink() or mode.st_uid != 0 or mode.st_mode & 0o022:
            raise RuntimeError("unsafe executor installation directory")
    identity = json.dumps({"revision": revision, "sha256": hashlib.sha256(program).hexdigest()}).encode()
    for name, data in (("executor.py", program), ("revision.json", identity)):
        with tempfile.NamedTemporaryFile(dir=INSTALL, delete=False) as output:
            temporary = Path(output.name)
            try:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
                temporary.chmod(0o644)
                temporary.replace(INSTALL / name)
            finally:
                temporary.unlink(missing_ok=True)
    print("Validation worker prepared at executor " + revision + ". No service or fleet credentials installed.")


if __name__ == "__main__":
    main()
