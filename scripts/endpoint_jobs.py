"""The worker's protected job projection and export allowance."""

from contextlib import contextmanager
import fcntl
import hashlib
import os
import re
import stat
import struct
import time
import unicodedata
import zlib

from endpoint_snapshot_store import (
    DIRECTORY_FLAGS, MAX_JOB_BYTES, READ_FLAGS, StoreError, _no_acl, _open_directory,
    _os_error, _read_at, _regular, _require, decode_json, encode_json, jobs_identity, publish_jobs, read_jobs,
)

SLOTS = 5000
FRAME = 4096
STATE_BYTES = 8192
AGE = 7 * 86400
MIN_INTERVAL_NS = 60 * 10**9
SCOPE = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
OWNER = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]{1,100}")
FINGERPRINT = re.compile(r"[0-9a-f]{64}")
RUN_ID = re.compile(r"[1-9][0-9]{0,18}")
RUNNER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
RESULTS = {"success", "failure", "cancelled", "skipped", "timed_out", "neutral",
           "action_required", "stale", "startup_failure"}
COVERAGE_GAPS = {"initial_unknown", "unclean_session", "storage_corruption",
                 "event_loss", "retention_cap", "retention_expired", "retention_nonterminal"}
CURRENT_COVERAGE_GAPS = COVERAGE_GAPS - {"retention_expired", "retention_cap"}
SOURCE_TIMES = {"queued_at", "scale_set_assigned_at", "runner_assigned_at", "finished_at"}
LOCAL_TIMES = {"start_observed_at", "completion_observed_at", "last_observed_at"}
FIELDS = SOURCE_TIMES | LOCAL_TIMES | {
    "job_id", "owner", "repository", "display_name", "display_truncated", "run_id",
    "runner_name", "runner_id", "state", "result", "gap",
}
PRIVATE_NAMES = {"state.json", "state.json.tmp", "records", "attempt.json", "attempt.json.tmp"}


def match(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def uint(value):
    return type(value) is int and 0 <= value <= 2**63 - 1


def valid_gaps(value):
    return (isinstance(value, dict) and value.keys() <= COVERAGE_GAPS
            and all(uint(count) and count > 0 for count in value.values()))


def valid_demand(demand, now):
    return (isinstance(demand, dict) and set(demand) == {"observed_at", "available", "assigned"}
            and all(uint(value) for value in demand.values()) and 0 < demand["observed_at"] <= now)


def text(value, limit):
    return (isinstance(value, str) and len(value.encode("utf-8")) <= limit
            and all(unicodedata.category(char) != "Cc" for char in value))


def run_link(record):
    repo = record.get("repository")
    run = record.get("run_id")
    if (match(OWNER, record.get("owner")) and match(REPOSITORY, repo) and repo not in {".", ".."}
            and match(RUN_ID, run) and uint(int(run))):
        return f"https://github.com/{record['owner']}/{repo}/actions/runs/{run}"
    return None


def record_key(record):
    return ("job", record["job_id"]) if "job_id" in record else ("runner", record["runner_name"])


def validate_record(record, now, *, private=False):
    extra = {"activity_epoch"} if private else {"run_url"}
    _require(isinstance(record, dict) and {"last_observed_at", "state"} <= record.keys() <= FIELDS | extra,
             "invalid_jobs")
    for name, pattern in (("owner", OWNER), ("repository", REPOSITORY), ("runner_name", RUNNER), ("run_id", RUN_ID),
                          ("activity_epoch", UUID)):
        _require(name not in record or match(pattern, record[name]), "invalid_jobs")
    _require(("owner" in record) == ("repository" in record) and record.get("repository") not in {".", ".."}
             and ("run_id" not in record or uint(int(record["run_id"])))
             and ("runner_id" not in record or uint(record["runner_id"]) and record["runner_id"] > 0)
             and ("display_truncated" not in record or record["display_truncated"] is True
                  and bool(record.get("display_name"))), "invalid_jobs")
    for field, limit in (("job_id", 256), ("display_name", 512)):
        _require(field not in record or text(record[field], limit) and bool(record[field]), "invalid_jobs")
    for field in SOURCE_TIMES | LOCAL_TIMES:
        _require(field not in record or uint(record[field]) and 0 < record[field] <= now + 300, "invalid_jobs")
    seen = record["last_observed_at"]
    _require(all(record.get(field, 0) <= seen for field in LOCAL_TIMES), "invalid_jobs")
    state, gap = record["state"], record.get("gap")
    if state == "completed":
        _require("job_id" in record and "completion_observed_at" in record and "activity_epoch" not in record
                 and ((gap is None and record.get("result") in RESULTS)
                      or (gap == "result_unknown" and "result" not in record)), "invalid_jobs")
    elif state == "running":
        _require({"job_id", "start_observed_at"} <= record.keys()
                 and ("activity_epoch" in record) == private and gap is None
                 and not {"completion_observed_at", "result"} & record.keys(), "invalid_jobs")
    elif state == "unknown":
        _require(not {"completion_observed_at", "result", "activity_epoch"} & record.keys(), "invalid_jobs")
        if gap in {"recovered_association_unknown", "runner_exit_without_job"}:
            _require(set(record) == {"runner_name", "last_observed_at", "state", "gap"}, "invalid_jobs")
        else:
            _require(gap in {"runner_exited", "coverage_unknown", "runner_activity_unverified"} and "job_id" in record
                     and "start_observed_at" in record, "invalid_jobs")
    else:
        raise StoreError("invalid_jobs")
    _require(private or record.get("run_url") == run_link(record), "invalid_jobs")
    _require(len(encode_json(record)) - 1 <= FRAME, "invalid_jobs")


@contextmanager
def private_directory(base, observer_gid):
    base_fd = fd = None
    try:
        base_fd = _open_directory(base)
        metadata = os.fstat(base_fd)
        _require(metadata.st_uid == 0 and metadata.st_gid == observer_gid
                 and stat.S_IMODE(metadata.st_mode) == 0o750, "unsafe_path")
        _no_acl(base_fd, "unsafe_path")
        fd = os.open("private", DIRECTORY_FLAGS, dir_fd=base_fd)
        metadata = os.fstat(fd)
        _require(metadata.st_uid == 0 and stat.S_IMODE(metadata.st_mode) == 0o700, "unsafe_path")
        _no_acl(fd, "unsafe_path")
        yield fd
    except OSError as error:
        raise _os_error(error) from None
    finally:
        if fd is not None:
            os.close(fd)
        if base_fd is not None:
            os.close(base_fd)


def check_private_file(fd, name, limit):
    _regular(os.stat(name, dir_fd=fd, follow_symlinks=False), modes=(0o600,), max_bytes=limit)
    handle = os.open(name, READ_FLAGS, dir_fd=fd)
    try:
        _regular(os.fstat(handle), modes=(0o600,), max_bytes=limit)
        _no_acl(handle, "unsafe_file")
    finally:
        os.close(handle)


@contextmanager
def private_lock(base, observer_gid):
    with private_directory(base, observer_gid) as fd:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise StoreError("busy") from None
        with os.scandir(fd) as entries:
            for entry in entries:
                _require(entry.name in PRIVATE_NAMES, "unsafe_file")
                limit = SLOTS * FRAME if entry.name == "records" else STATE_BYTES
                check_private_file(fd, entry.name, limit)
        yield fd


def read_private(fd, name, limit=STATE_BYTES):
    try:
        return _read_at(fd, name, modes=(0o600,), max_bytes=limit)
    except OSError as error:
        raise _os_error(error) from None


def load_state(fd, controller, scale_set, now):
    state = decode_json(read_private(fd, "state.json"), max_bytes=STATE_BYTES)
    fields = {"version", "boot", "controller", "scale_set", "generation", "activity_epoch",
              "session_open", "dirty", "poll_at", "gaps", "slots", "record_count", "record_file"}
    _require(fields <= state.keys() <= fields | {"demand"} and type(state["version"]) is int
             and state["version"] == 1 and state["controller"] == controller and state["scale_set"] == scale_set
             and match(UUID, state["generation"]) and match(UUID, state["activity_epoch"]) and match(UUID, state["boot"])
             and type(state["session_open"]) is bool and type(state["dirty"]) is bool
             and uint(state["poll_at"]) and state["poll_at"] <= now and valid_gaps(state["gaps"])
             and uint(state["slots"]) and state["slots"] <= SLOTS
             and uint(state["record_count"]) and state["record_count"] <= state["slots"]
             and match(FINGERPRINT, state["record_file"])
             and ("demand" not in state or valid_demand(state["demand"], now)), "invalid_jobs")
    return state


def allowance_clock(root):
    try:
        boot = (root / "proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        ticks = time.clock_gettime_ns(time.CLOCK_BOOTTIME)
    except (OSError, UnicodeError, AttributeError):
        return None
    return (boot, ticks) if match(UUID, boot) else None


def allowance_ready(fd, clock):
    try:
        check_private_file(fd, "attempt.json", STATE_BYTES)
        prior = decode_json(read_private(fd, "attempt.json"), max_bytes=STATE_BYTES)
    except FileNotFoundError:
        prior = {}
    except StoreError as error:
        if error.code not in {"not_found", "invalid_json"}:
            raise
        prior = {}
    if clock is None:
        if prior != {"uncertain": True}:
            write_attempt(fd, None, in_flight=False)
        return False
    boot, ticks = clock
    valid = (set(prior) == {"boot", "ticks", "in_flight"} and prior["boot"] == boot
             and prior["in_flight"] is False and uint(prior["ticks"]) and prior["ticks"] <= ticks)
    if not valid:
        write_attempt(fd, clock, in_flight=False)
        return False
    return ticks - prior["ticks"] >= MIN_INTERVAL_NS


def invalidate_allowance(base, observer_gid):
    try:
        with private_directory(base, observer_gid) as fd:
            allowance_ready(fd, None)
    except StoreError:
        pass


def write_attempt(fd, clock, *, in_flight):
    value = {"uncertain": True} if clock is None else {"boot": clock[0], "ticks": clock[1], "in_flight": in_flight}
    try:
        check_private_file(fd, "attempt.json.tmp", STATE_BYTES)
        os.unlink("attempt.json.tmp", dir_fd=fd)
    except FileNotFoundError:
        pass
    handle = os.open("attempt.json.tmp", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=fd)
    with os.fdopen(handle, "wb") as temporary:
        temporary.write(encode_json(value))
        temporary.flush()
        os.fsync(temporary.fileno())
    os.replace("attempt.json.tmp", "attempt.json", src_dir_fd=fd, dst_dir_fd=fd)
    os.fsync(fd)


def record_order(record):
    if record["state"] == "completed":
        return (1, -record.get("finished_at", record["completion_observed_at"]), record_key(record))
    return (0, -record["last_observed_at"], record_key(record))


def projection(state, payload, now):
    records = {}
    seen_keys = set()
    gaps = dict(state["gaps"])
    corrupt = int(len(payload) != state["slots"] * FRAME)
    for offset in range(0, len(payload) - FRAME + 1, FRAME):
        frame = payload[offset:offset + FRAME]
        if not any(frame):
            continue
        length, checksum = struct.unpack_from("<II", frame)
        raw = frame[8:8 + length]
        try:
            _require(0 < length <= FRAME - 8 and zlib.crc32(raw) == checksum, "invalid_jobs")
            record = decode_json(raw, max_bytes=FRAME - 8)
            validate_record(record, now, private=True)
            key = record_key(record)
            _require(key not in seen_keys, "invalid_jobs")
        except (StoreError, UnicodeError, TypeError, ValueError):
            corrupt += 1
            continue
        seen_keys.add(key)
        if record["last_observed_at"] <= now - AGE:
            continue
        epoch = record.pop("activity_epoch", None)
        if record["state"] == "running" and (epoch != state["activity_epoch"] or not state["session_open"]):
            record.update(state="unknown", gap="coverage_unknown")
        link = run_link(record)
        if link is not None:
            record["run_url"] = link
        records[key] = record
    corrupt = max(corrupt, abs(state["record_count"] - len(seen_keys)))
    if corrupt:
        gaps["storage_corruption"] = min(2**63 - 1, gaps.get("storage_corruption", 0) + corrupt)
        for record in records.values():
            if record["state"] == "running":
                record.update(state="unknown", gap="coverage_unknown")
    ordered = sorted(records.values(), key=record_order)
    exported = []
    size = 4096
    for record in ordered:
        size += len(encode_json(record))
        if size > MAX_JOB_BYTES:
            break
        exported.append(record)
    times = [record["last_observed_at"] for record in ordered]
    return {
        "schema_version": 1, "role": "worker", "controller": state["controller"],
        "scale_set": state["scale_set"], "generation": state["generation"], "published_at": now,
        "observation_source": "scale_set_listener", "gaps": gaps,
        "retained": len(ordered), "exported": len(exported), "omitted": len(ordered) - len(exported),
        "omitted_nonterminal": sum(record["state"] != "completed" for record in ordered[len(exported):]),
        "covered_from": min(times, default=None), "covered_through": max(times, default=None),
        "records": exported,
    }


def validate_projection(value, controller):
    fields = {"schema_version", "role", "controller", "scale_set", "generation", "published_at",
              "observation_source", "gaps", "retained", "exported", "omitted", "omitted_nonterminal",
              "covered_from", "covered_through", "records"}
    try:
        now = value["published_at"]
        rows = value["records"]
        _require(set(value) == fields and type(value["schema_version"]) is int and value["schema_version"] == 1
                 and value["role"] == "worker" and value["controller"] == controller
                 and match(SCOPE, value["scale_set"]) and match(UUID, value["generation"])
                 and uint(now) and value["observation_source"] == "scale_set_listener"
                 and valid_gaps(value["gaps"]) and isinstance(rows, list)
                 and all(uint(value[field]) for field in ("retained", "exported", "omitted", "omitted_nonterminal"))
                 and value["retained"] == value["exported"] + value["omitted"] <= SLOTS
                 and len(rows) == value["exported"] and value["omitted_nonterminal"] <= value["omitted"], "invalid_jobs")
        lo, hi = value["covered_from"], value["covered_through"]
        _require((uint(lo) and uint(hi) and now - AGE < lo <= hi <= now + 300) if value["retained"]
                 else lo is None and hi is None, "invalid_jobs")
        for record in rows:
            validate_record(record, now)
            _require(lo <= record["last_observed_at"] <= hi, "invalid_jobs")
        _require(len({record_key(record) for record in rows}) == len(rows)
                 and rows == sorted(rows, key=record_order)
                 and (not value["omitted_nonterminal"] or not any(row["state"] == "completed" for row in rows)),
                 "invalid_jobs")
    except (TypeError, ValueError, KeyError, UnicodeError) as error:
        if isinstance(error, StoreError):
            raise
        raise StoreError("invalid_jobs") from None


def valid_summary(value, now):
    fields = {"scale_set", "pending", "source_available", "last_poll_at", "generation", "export_fingerprint", "gaps"}
    return (isinstance(value, dict) and fields <= value.keys() <= fields | {"demand"}
            and match(SCOPE, value["scale_set"]) and type(value["pending"]) is bool
            and type(value["source_available"]) is bool and uint(value["last_poll_at"])
            and value["last_poll_at"] <= now and valid_gaps(value["gaps"])
            and (match(UUID, value["generation"]) and match(FINGERPRINT, value["export_fingerprint"])
                 or value["pending"] and value["generation"] is value["export_fingerprint"] is None)
            and ("demand" not in value or valid_demand(value["demand"], now)))


def fingerprint(identity):
    return hashlib.sha256(repr(identity).encode("ascii")).hexdigest()


def record_file_digest(metadata):
    identity = f"{metadata.st_dev}:{metadata.st_ino}:{metadata.st_size}:{metadata.st_ctime_ns}"
    return hashlib.sha256(identity.encode("ascii")).hexdigest()


def records_health(private_fd, state):
    try:
        fd = os.open("records", READ_FLAGS, dir_fd=private_fd)
    except FileNotFoundError:
        return False, False
    try:
        metadata = os.fstat(fd)
        _regular(metadata, modes=(0o600,), max_bytes=SLOTS * FRAME)
        _no_acl(fd, "unsafe_file")
        intact = (metadata.st_size == state["slots"] * FRAME
                  and record_file_digest(metadata) == state["record_file"])
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return intact, True
        return intact, False
    finally:
        os.close(fd)


def job_summary(state, incumbent, now, clock, intact, writer):
    generation = incumbent.get("generation")
    summary = {
        "scale_set": state["scale_set"], "pending": not intact or state["dirty"] or state["generation"] != generation,
        "generation": generation, "export_fingerprint": incumbent.get("export_fingerprint"),
        "gaps": dict(state["gaps"]), "last_poll_at": state["poll_at"],
        "source_available": (intact and writer and clock is not None and clock[0] == state["boot"]
                             and state["session_open"] and not state["dirty"] and 0 < state["poll_at"] <= now),
    }
    if not intact:
        summary["gaps"]["storage_corruption"] = max(1, summary["gaps"].get("storage_corruption", 0))
    if "demand" in state:
        summary["demand"] = state["demand"]
    return summary


def incumbent_summary(base, observer_gid, directory_fd, controller, scale_set, previous):
    try:
        identity = jobs_identity(directory_fd, observer_gid)
        if (previous.get("scale_set") == scale_set and previous.get("pending") is False
                and previous.get("export_fingerprint") == fingerprint(identity)):
            return previous
        identity, current = read_jobs(base, observer_gid, directory_fd=directory_fd)
        validate_projection(current, controller)
        if current["scale_set"] == scale_set:
            return {"generation": current["generation"], "export_fingerprint": fingerprint(identity),
                    "gaps": current["gaps"]}
    except StoreError:
        pass
    return {}


def publish_history(base, observer_gid, directory_fd, controller, scale_set, now, clock, previous, save_summary):
    incumbent = incumbent_summary(base, observer_gid, directory_fd, controller, scale_set, previous)
    with private_directory(base, observer_gid) as private_fd:
        sampled = clock()
        allowed = allowance_ready(private_fd, sampled)
    with private_lock(base, observer_gid) as private_fd:
        state = load_state(private_fd, controller, scale_set, now)
        intact, writer = records_health(private_fd, state)
        summary = job_summary(state, incumbent, now, sampled, intact, writer)
        if not summary["pending"]:
            summary["gaps"] = incumbent["gaps"]
            save_summary(summary)
            return
        if state["dirty"] or not intact or not allowed:
            save_summary(summary)
            return
        payload = read_private(private_fd, "records", SLOTS * FRAME)
    save_summary(summary)
    value = projection(state, payload, now)
    validate_projection(value, controller)
    with private_directory(base, observer_gid) as private_fd:
        reservation = clock()
        if not allowance_ready(private_fd, reservation):
            return
        write_attempt(private_fd, reservation, in_flight=True)
    try:
        publish_jobs(base, value, observer_gid, directory_fd=directory_fd)
    finally:
        with private_directory(base, observer_gid) as private_fd:
            write_attempt(private_fd, clock(), in_flight=False)
    identity, written = read_jobs(base, observer_gid, directory_fd=directory_fd)
    validate_projection(written, controller)
    _require(written == value, "invalid_jobs")
    with private_lock(base, observer_gid) as private_fd:
        latest = load_state(private_fd, controller, scale_set, now)
        intact, writer = records_health(private_fd, latest)
        summary = job_summary(latest, {"generation": written["generation"], "export_fingerprint": fingerprint(identity)},
                              now, clock(), intact, writer)
        if not summary["pending"]:
            summary["gaps"] = written["gaps"]
        save_summary(summary)
