#!/usr/bin/env python3
"""Own and reap one validation stage, including descendants that call setsid."""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

if __name__ != "__main__":
    # Later candidates can write files owned by this UID, so never reload this path.
    _SUPERVISOR_PROGRAM = Path(__file__).read_text()


def _identity(pid: int) -> tuple[int, int, str]:
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return int(fields[1]), int(fields[19]), fields[0]


def _enable_subreaper() -> None:
    if not Path("/proc/self/stat").is_file():
        raise RuntimeError("Linux /proc is required for process cleanup")
    # This process owns only one stage. Never make the caller a subreaper.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), "cannot enable child subreaper")
    descriptor = os.pidfd_open(os.getpid())
    try:
        signal.pidfd_send_signal(descriptor, 0)
    finally:
        os.close(descriptor)


def _descendants(owner: int, handles: dict) -> None:
    snapshot = {}
    for entry in Path("/proc").glob("[0-9]*/stat"):
        pid = int(entry.parent.name)
        try:
            snapshot[pid] = _identity(pid)
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
    descendants = {owner}
    while True:
        found = {pid for pid, (parent, _, _) in snapshot.items() if parent in descendants}
        if found <= descendants:
            break
        descendants.update(found)
    for pid in descendants - {owner}:
        identity = snapshot[pid]
        if pid in handles and handles[pid][0] == identity[1]:
            continue
        try:
            descriptor = os.pidfd_open(pid)
        except ProcessLookupError:
            continue
        try:
            # A pidfd cannot signal a replacement process after PID reuse.
            current = _identity(pid)
            parent = current[0]
            if (current[:2] != identity[:2]
                    or parent not in descendants
                    or (parent != owner and _identity(parent)[1] != snapshot[parent][1])):
                os.close(descriptor)
                continue
        except (FileNotFoundError, ProcessLookupError):
            os.close(descriptor)
            continue
        previous = handles.pop(pid, None)
        if previous:
            os.close(previous[1])
        handles[pid] = (identity[1], descriptor)


def _signal_descendants(handles: dict, sent: set, signum: int, adopted_only: bool = False) -> bool:
    signalled = False
    for pid, (start, descriptor) in list(handles.items()):
        try:
            parent, actual_start, state = _identity(pid)
        except (FileNotFoundError, ProcessLookupError):
            actual_start, state = None, "Z"
        if actual_start != start:
            os.close(descriptor)
            del handles[pid]
            continue
        identity = (pid, start)
        if state == "Z" or identity in sent or (adopted_only and parent != os.getpid()):
            continue
        try:
            signal.pidfd_send_signal(descriptor, signum)
            signalled = True
            sent.add(identity)
        except ProcessLookupError:
            pass
    return signalled


def _supervise(request: dict, output) -> dict:
    result = {"version": 1, "launched": False, "leader": None, "timed_out": False,
              "cancelled": None, "forced": False, "cleanup_complete": False, "error": None}
    cancelled = None

    def cancel(signum, _frame):
        nonlocal cancelled
        if cancelled is None:
            cancelled = signum

    handles = {}
    process = None
    previous = {}
    try:
        _enable_subreaper()
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, cancel)
        process = subprocess.Popen(request["command"], cwd=request["cwd"], env=request["env"],
                                   stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        result["launched"] = True
        deadline = time.monotonic() + request["timeout"]
        cleanup_deadline = None
        initial_cleanup = True
        terminated, killed = set(), set()
        while True:
            empty = False
            while True:
                try:
                    pid, status = os.waitpid(-1, os.WNOHANG)
                except ChildProcessError:
                    empty = True
                    break
                if pid == 0:
                    break
                if pid == process.pid:
                    result["leader"] = process.returncode = os.waitstatus_to_exitcode(status)
            if empty:
                result["cleanup_complete"] = True
                break
            now = time.monotonic()
            if cleanup_deadline is None:
                if cancelled is not None:
                    result["cancelled"] = cancelled
                elif result["leader"] is None and now >= deadline:
                    result["timed_out"] = True
                if result["leader"] is not None or result["timed_out"] or cancelled is not None:
                    cleanup_deadline = now + request["grace"]
            if cleanup_deadline is not None:
                _descendants(os.getpid(), handles)
                if now >= cleanup_deadline:
                    result["forced"] |= _signal_descendants(handles, killed, signal.SIGKILL)
                else:
                    # Let signalled parents run cleanup helpers during their grace.
                    _signal_descendants(handles, terminated, signal.SIGTERM, adopted_only=not initial_cleanup)
                    initial_cleanup = False
            time.sleep(0.01)
    except Exception as error:
        result["error"] = f"{type(error).__name__}: {error}"[:2000]
        if process is None:
            result["cleanup_complete"] = True
        else:
            # Keep ownership until the kernel confirms every child was reaped.
            while True:
                _descendants(os.getpid(), handles)
                result["forced"] |= _signal_descendants(handles, set(), signal.SIGKILL)
                try:
                    pid, status = os.waitpid(-1, os.WNOHANG)
                    if pid == process.pid:
                        result["leader"] = process.returncode = os.waitstatus_to_exitcode(status)
                except ChildProcessError:
                    result["cleanup_complete"] = True
                    break
                time.sleep(0.01)
    finally:
        for _, descriptor in handles.values():
            os.close(descriptor)
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return result


def run(command: list[str], cwd: Path, env: dict, log: Path, timeout: int,
        grace: float = 60) -> None:
    """Run one stage and wait for all its descendants before returning or raising."""
    if timeout <= 0 or grace < 0:
        raise ValueError("timeout must be positive and cleanup grace nonnegative")
    request = {"command": command, "cwd": str(cwd), "env": env, "timeout": timeout, "grace": grace}
    previous = {}
    pending = None
    interruption = None
    descriptor = None
    process = None

    def cancel(signum, frame):
        nonlocal pending
        if pending is None:
            pending = (signum, frame)
        if descriptor is not None:
            try:
                signal.pidfd_send_signal(descriptor, signum)
            except ProcessLookupError:
                pass
        elif process is not None:
            # An unreaped direct child cannot have its PID reused.
            process.send_signal(signum)

    try:
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                handler = signal.getsignal(signum)
                if handler != signal.SIG_IGN:
                    previous[signum] = signal.signal(signum, cancel)
        with log.open("wb") as output:
            process = subprocess.Popen([sys.executable, "-I", "-c", _SUPERVISOR_PROGRAM, "--supervise"],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=output,
                                       cwd="/", env={"PATH": os.defpath, "LANG": "C.UTF-8"},
                                       start_new_session=True)
            try:
                descriptor = os.pidfd_open(process.pid)
                process.stdin.write(json.dumps(request).encode())
                process.stdin.close()
                if pending is not None:
                    cancel(*pending)
                process.wait()
            except BaseException as error:
                interruption = error
                cancel(signal.SIGTERM, None)
                # Repeated cancellation cannot cut short descendant cleanup.
                while process.poll() is None:
                    try:
                        process.wait()
                    except KeyboardInterrupt:
                        cancel(signal.SIGTERM, None)
            finally:
                try:
                    process.stdin.close()
                except BrokenPipeError:
                    pass
            response = process.stdout.read()
            process.stdout.close()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        if descriptor is not None:
            os.close(descriptor)
    if interruption is not None:
        raise interruption
    if pending is not None:
        signum, frame = pending
        handler = previous.get(signum, signal.SIG_DFL)
        if handler == signal.SIG_DFL:
            os.kill(os.getpid(), signum)
        elif callable(handler):
            handler(signum, frame)
        if handler != signal.SIG_IGN:
            raise RuntimeError(f"check cancelled by signal {signum}; see {log}")
    try:
        result = json.loads(response)
        if (process.returncode != 0 or result.get("version") != 1
                or result.get("cleanup_complete") is not True
                or type(result.get("forced")) is not bool
                or type(result.get("timed_out")) is not bool):
            raise ValueError("incomplete supervisor result")
    except (ValueError, TypeError, AttributeError) as error:
        raise RuntimeError(f"invalid stage supervisor result; see {log}") from error
    if result.get("error"):
        raise RuntimeError(f"stage supervisor failed: {result['error']}; see {log}")
    if result["timed_out"]:
        raise subprocess.TimeoutExpired(command, timeout)
    if result.get("cancelled") is not None:
        raise RuntimeError(f"check cancelled; see {log}")
    if result.get("launched") is not True or type(result.get("leader")) is not int:
        raise RuntimeError(f"incomplete stage supervisor result; see {log}")
    if result["leader"]:
        raise RuntimeError(f"check exited {result['leader']}; see {log}")
    if result["forced"]:
        raise RuntimeError("check required forced termination; cleanup is incomplete")


if __name__ == "__main__":
    if sys.argv[1:] != ["--supervise"]:
        raise SystemExit("use the trusted validation orchestrator")
    try:
        configuration = json.loads(sys.stdin.buffer.read())
        # stdout is the private result pipe. Candidate output goes only to stderr.
        outcome = _supervise(configuration, sys.stderr)
        print(json.dumps(outcome), flush=True)
    except Exception as failure:
        print(f"stage supervisor failed: {failure}", file=sys.stderr)
        raise SystemExit(1)
