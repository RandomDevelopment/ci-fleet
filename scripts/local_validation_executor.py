#!/usr/bin/env python3
"""Execute validation stages through the system manager on a disposable worker."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
import uuid

USER = "ci-fleet-validation"
HOST_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}
# ExecStopPost runs after systemd has terminated the stage's remaining processes.
# Its receipt lives in a root-only directory, outside candidate-writable paths.
FINISH = '''import json, os
from pathlib import Path
result = {key: os.environ.get(key) for key in ("SERVICE_RESULT", "EXIT_CODE", "EXIT_STATUS")}
with Path(__file__).with_name("result.json").open("x") as output:
    json.dump(result, output)
'''


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


def prepare_stage(writable: list[Path], readonly: list[Path]) -> None:
    """Give the job UID its scratch copies, keeping trusted inputs root-owned."""
    try:
        account = pwd.getpwnam(USER)
    except KeyError as error:
        raise RuntimeError("prepare the disposable worker before running configuration tests") from error
    if account.pw_uid == 0:
        raise RuntimeError("validation job account must be unprivileged")
    for roots, mutable in ((readonly, False), (writable, True)):
        for root in roots:
            for path in (root, *root.rglob("*")):
                mode = path.lstat().st_mode
                if mutable:
                    os.chown(path, account.pw_uid, account.pw_gid, follow_symlinks=False)
                elif not stat.S_ISLNK(mode):
                    os.chown(path, 0, 0)
                    access = 0o005 if stat.S_ISDIR(mode) or mode & 0o111 else 0o004
                    path.chmod((stat.S_IMODE(mode) & ~0o022) | access)


def run(command: list[str], cwd: Path, env: dict, log: Path, timeout: int,
        grace: float = 60, *, user: str = "root") -> None:
    """Wait for the stage result and verified cgroup cleanup before returning."""
    if timeout <= 0 or grace < 0 or user not in ("root", USER):
        raise ValueError("invalid stage deadline, cleanup grace or execution user")
    if not command or any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) for key in env):
        raise ValueError("invalid stage command or environment")
    require_worker()
    if user == USER and pwd.getpwnam(USER).pw_uid == 0:
        raise RuntimeError("validation job account must be unprivileged")
    unit = "ci-validation-" + uuid.uuid4().hex + ".service"
    cgroup = Path("/sys/fs/cgroup/system.slice") / unit
    previous, cancelled = {}, []
    process = None
    started = time.monotonic()

    def cancel(signum, _frame):
        if not cancelled:
            cancelled.append(signum)

    def stop():
        subprocess.run(["systemctl", "--system", "stop", unit], env=HOST_ENV,
                       stdin=subprocess.DEVNULL, capture_output=True, timeout=grace + 15)

    with tempfile.TemporaryDirectory(prefix=unit.removesuffix(".service") + "-", dir="/run") as temporary:
        root = Path(temporary)
        finish = root / "finish.py"
        finish.write_text(FINISH)
        properties = ["Type=exec", "ExitType=main", "Slice=system.slice", "Restart=no", "Delegate=no",
                      "KillMode=control-group", "SendSIGKILL=yes", "RuntimeMaxSec=" + str(timeout) + "s",
                      "TimeoutStopSec=" + str(max(grace, .001)) + "s", "TasksMax=512",
                      "User=" + user, "ExecStopPost=+/usr/bin/python3 -I " + str(finish)]
        if user != "root":
            properties += ["NoNewPrivileges=yes", "CapabilityBoundingSet=", "AmbientCapabilities=",
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
                while process.poll() is None:
                    if cancelled and not stopping:
                        stopping = True
                        stop()
                    if time.monotonic() - started > timeout + grace + 30:
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
            events = (cgroup / "cgroup.events").read_text() if cgroup.exists() else "populated 0\n"
            if "populated 0" not in events.splitlines():
                raise RuntimeError("stage cgroup is not empty; discard the worker")
            if cancelled:
                raise RuntimeError("check cancelled by signal " + str(cancelled[0]) + "; see " + str(log))
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
        account = pwd.getpwnam(USER)
    except KeyError:
        subprocess.run(["useradd", "--system", "--user-group", "--no-create-home", "--shell", "/usr/sbin/nologin", USER],
                       env=HOST_ENV, check=True)
        account = pwd.getpwnam(USER)
    if account.pw_uid == 0 or account.pw_shell != "/usr/sbin/nologin":
        raise RuntimeError("unsafe existing validation job account")
    print("Validation worker ready. This adds no persistent service or fleet credentials.")


if __name__ == "__main__":
    main()
