#!/usr/bin/env python3
"""Allowlisted worker observations. This module does not install or start a service."""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import grp
import hmac
import json
import math
import re
import time
from pathlib import Path
from typing import Any, Mapping

from endpoint_snapshot_sources import collect_documents
from endpoint_snapshot_store import StoreError, publication_lock, publish_endpoint, read_endpoint

MAX_BYTES = 65_536
IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
CONTROLLER_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
COMMIT = re.compile(r"[0-9a-f]{40}")
VERSION = re.compile(r"[A-Za-z0-9_.+-]{1,64}")
BEARER = re.compile(r"[A-Za-z0-9_-]{43}")
AVAILABILITY = {"available", "not_configured", "unsupported", "unavailable", "unknown"}
ERRORS = {"not_found", "source_unsafe", "source_too_large", "source_invalid", "source_unavailable",
          "source_timeout", "unsupported", "identity_mismatch", "clock_uncertain", "revision_unverified"}
HEALTH_IDS = {
    "disk_root", "disk_docker", "inode_root", "inode_docker", "memory", "load", "swap", "oom",
    "docker", "docker_network_inspection", "docker_network_headroom", "docker_network_legacy",
    "docker_default_bridge", "controller", "controller_status", "docker_socket_proxy", "restarts",
    "capacity", "capacity_service", "managed_unhealthy", "managed_inactive", "stale_container", "stale_image", "stale_network", "stale_volume",
    "stale_build_cache_byte", "stale_build_cache", "cleanup", "drift", "timer_health", "timer_cleanup", "timer_drift",
    "timer_updates", "timer_reconcile", "timer_capacity", "reconcile", "updates", "updates_service", "reboot", "clock", "backup", "reconciliation",
    "status_delivery",
}
STATES = {"active", "drained", "disabled"}
RECONCILIATION = {"bootstrap", "converged", "drift", "failed", "invalid", "pending", "reconciling", "rolled_back"}
STALE_AFTER = {"controller": 120, "health": 900, "reconciliation": 900}
UNSUPPORTED_SOURCES = {"enrollment_identity", "enrollment_authentication", "enrollment_scale_set",
                       "enrollment_listener", "enrollment_reporting", "enrollment_owner_confirmation",
                       "runtime_revision", "manager_revision", "staged_manager_revision",
                       "desired_engine_revision", "image_verification", "capacity", "jobs", "transaction"}
SOURCE_NAMES = {"installation", "configuration", "controller", "health", "reconciliation"} | UNSUPPORTED_SOURCES


class SnapshotError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def integer(value: Any) -> bool:
    return type(value) is int and 0 <= value <= 2**63 - 1


def token(value: Any) -> bool:
    return isinstance(value, str) and IDENTITY.fullmatch(value) is not None


def commit(value: Any) -> bool:
    return isinstance(value, str) and COMMIT.fullmatch(value) is not None


def choice(*values: str):
    return lambda value: isinstance(value, str) and value in values


def labels(value: Any) -> bool:
    return (isinstance(value, list) and len(value) >= 1
            and all(isinstance(item, str) and CONTROLLER_ID.fullmatch(item) for item in value)
            and len(set(value)) == len(value))


def checks(value: Any) -> bool:
    return (isinstance(value, list) and len(value) <= len(HEALTH_IDS)
            and all(isinstance(item, dict) and set(item) == {"id", "status"}
                    and item["id"] in HEALTH_IDS and item["status"] in {"ok", "warning", "critical"} for item in value)
            and len({item["id"] for item in value}) == len(value))


DATA_FIELDS = {
    "installation": {
        "record_state": choice("installed", "absent"), "lifecycle": choice("experimental", "stable", "retiring"),
        "policy_state": choice(*STATES), "installed_at": integer, "applied_commit": commit,
        "engine_ref": commit, "architecture": choice("amd64", "arm64"), "pool": token,
        "reviewed_maximum": integer, "capacity_budget": integer, "scale_set_name": token,
        "reporting_configured": lambda v: type(v) is bool, "reporting_required": lambda v: type(v) is bool,
        "network_policy_configured": lambda v: type(v) is bool,
    },
    "configuration": {
        "applied_commit": commit, "engine_ref": commit, "routing_labels": labels,
        "runner_cpu_cores": lambda v: type(v) in {int, float} and math.isfinite(v) and v > 0,
        "runner_memory_mib": lambda v: integer(v) and v > 0,
        "minimum_runners": lambda v: type(v) is int and v == 0, "maximum_runners": integer,
        "storage_retention_configured": lambda v: type(v) is bool,
    },
    "controller": {"software_version": lambda v: isinstance(v, str) and len(v) <= 64 and VERSION.fullmatch(v) is not None,
                   "current": integer, "busy": integer, "maximum": integer,
                   "matches_reviewed_maximum": lambda v: type(v) is bool},
    "health": {"status": choice("healthy", "warning", "unhealthy", "maintenance"), "checks": checks},
    "reconciliation": {"state": choice(*RECONCILIATION), "desired_commit": commit, "applied_commit": commit,
                       "last_success_at": integer},
    **{name: {} for name in UNSUPPORTED_SOURCES},
}


def timestamp(value: Any, now: int) -> int:
    if not integer(value):
        raise SnapshotError("source_invalid")
    if value > now:
        raise SnapshotError("clock_uncertain")
    return value


def installed_time(value: Any, now: int) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", value):
        raise SnapshotError("source_invalid")
    try:
        observed = int(dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc).timestamp())
    except (ValueError, OverflowError) as error:
        raise SnapshotError("source_invalid") from error
    return timestamp(observed, now)


def data(name: str, value: dict) -> dict:
    rules = DATA_FIELDS[name]
    try:
        valid = not (set(value) - rules.keys()) and all(rules[key](item) for key, item in value.items())
    except (TypeError, ValueError, KeyError, OverflowError):
        valid = False
    if not valid:
        raise SnapshotError("source_invalid")
    return value


def validate_data(name: str, value: dict) -> None:
    data(name, value)
    required = {
        "installation": {"record_state"},
        "configuration": set(DATA_FIELDS["configuration"]),
        "controller": {"software_version", "current", "busy", "maximum"},
        "health": {"status", "checks"},
        "reconciliation": {"state"},
    }.get(name, set())
    if name == "installation":
        if value.get("record_state") == "absent":
            if set(value) != {"record_state"}:
                raise SnapshotError("invalid_snapshot")
        else:
            required |= {"installed_at", "policy_state", "applied_commit", "engine_ref", "lifecycle", "pool", "reviewed_maximum"}
    if not required <= value.keys():
        raise SnapshotError("invalid_snapshot")
    if name == "controller" and not value["busy"] <= value["current"] <= value["maximum"]:
        raise SnapshotError("invalid_snapshot")
    if name == "configuration" and value["minimum_runners"] > value["maximum_runners"]:
        raise SnapshotError("invalid_snapshot")


def evidence(value: dict | None = None, *, observed_at: int | None = None,
             availability: str = "available", error: str | None = None) -> dict:
    return {"source_version": 1 if availability == "available" else None, "availability": availability,
            "observed_at": observed_at, "last_success_at": observed_at, "error": error, "data": value or {}}


def failure(error: str, previous: dict | None = None) -> dict:
    error = error if error in ERRORS else "source_unavailable"
    availability = "unsupported" if error == "unsupported" else "unknown" if error in {"clock_uncertain", "revision_unverified"} else "unavailable"
    result = evidence(availability=availability, error=error)
    if previous and previous["last_success_at"] is not None and error not in {"identity_mismatch", "clock_uncertain", "revision_unverified"}:
        result["last_success_at"] = previous["last_success_at"]
        result["source_version"] = previous["source_version"]
        result["data"] = copy.deepcopy(previous["data"])
    return result


def project_installation(value: dict, controller: str, now: int) -> dict:
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise SnapshotError("unsupported")
    if value.get("controller") != controller:
        raise SnapshotError("identity_mismatch")
    observed = installed_time(value.get("installed_at"), now)
    result = {"record_state": "installed", "installed_at": observed}
    mapping = {"controller_state": "policy_state", "config_ref": "applied_commit", "engine_ref": "engine_ref",
               "architecture": "architecture", "pool": "pool", "lifecycle": "lifecycle",
               "configured_max_runners": "reviewed_maximum", "capacity_budget": "capacity_budget",
               "scale_set_name": "scale_set_name", "status_reporting_configured": "reporting_configured",
               "status_reporting_required": "reporting_required", "docker_network_policy_configured": "network_policy_configured"}
    for source, target in mapping.items():
        if source in value:
            result[target] = value[source]
    required = {"policy_state", "applied_commit", "engine_ref", "lifecycle", "pool", "reviewed_maximum"}
    if not required <= result.keys():
        raise SnapshotError("source_invalid")
    return evidence(data("installation", result), observed_at=observed)


def project_configuration(value: dict, installation: dict, controller: str) -> dict:
    policy = installation["data"]
    if installation["availability"] != "available" or policy.get("record_state") != "installed":
        raise SnapshotError("revision_unverified")
    if (value.get("CI_FLEET_INSTANCE") != controller or value.get("CI_FLEET_CONFIG_REF") != policy["applied_commit"]
            or value.get("CI_FLEET_ENGINE_REF") != policy["engine_ref"]):
        raise SnapshotError("identity_mismatch")
    if value.get("CI_FLEET_DESIRED_STATE_SCHEMA") != "3":
        raise SnapshotError("unsupported")
    if "CI_FLEET_DOCKER_STORAGE_RETENTION" in value and value["CI_FLEET_DOCKER_STORAGE_RETENTION"] != "exclusive-fleet-daemon-v1":
        raise SnapshotError("source_invalid")
    try:
        result = {"applied_commit": policy["applied_commit"], "engine_ref": policy["engine_ref"],
                  "routing_labels": value["CI_FLEET_LABELS"].split(","),
                  "runner_cpu_cores": float(value["CI_FLEET_RUNNER_CPUS"]),
                  "runner_memory_mib": int(value["CI_FLEET_RUNNER_MEMORY_MIB"]),
                  "minimum_runners": int(value["CI_FLEET_MIN_RUNNERS"]),
                  "maximum_runners": int(value["CI_FLEET_MAX_RUNNERS"]),
                  "storage_retention_configured": value.get("CI_FLEET_DOCKER_STORAGE_RETENTION") == "exclusive-fleet-daemon-v1"}
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise SnapshotError("source_invalid") from error
    if result["maximum_runners"] != (policy["reviewed_maximum"] if policy["policy_state"] == "active" else 0):
        raise SnapshotError("source_invalid")
    return evidence(data("configuration", result), observed_at=installation["observed_at"])


def project_controller(value: dict, controller: str, now: int, installation: dict) -> dict:
    if value.get("controller") != controller:
        raise SnapshotError("identity_mismatch")
    result = {name: value[name] for name in ("software_version", "current", "busy", "maximum") if name in value}
    if set(result) != {"software_version", "current", "busy", "maximum"}:
        raise SnapshotError("source_invalid")
    data("controller", result)
    if not result["busy"] <= result["current"] <= result["maximum"]:
        raise SnapshotError("source_invalid")
    if installation["availability"] == "available" and "reviewed_maximum" in installation["data"]:
        result["matches_reviewed_maximum"] = result["maximum"] == installation["data"]["reviewed_maximum"]
    return evidence(result, observed_at=timestamp(value.get("generated_at"), now))


def project_health(value: dict, controller: str, now: int) -> dict:
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise SnapshotError("unsupported")
    if value.get("controller") != controller:
        raise SnapshotError("identity_mismatch")
    if not isinstance(value.get("checks"), list):
        raise SnapshotError("source_invalid")
    approved = []
    for check in value["checks"]:
        if not isinstance(check, dict) or not isinstance(check.get("id"), str):
            raise SnapshotError("source_invalid")
        if check["id"] in HEALTH_IDS:
            identifier = check["id"] + "_service" if check["id"] in {"capacity", "updates"} and "state" in check else check["id"]
            approved.append({"id": identifier, "status": check.get("status")})
    result = data("health", {"status": value.get("status"), "checks": approved})
    return evidence(result, observed_at=timestamp(value.get("timestamp"), now))


def project_reconciliation(value: dict, now: int, installation: dict) -> dict:
    if installation["availability"] != "available" or installation["data"].get("record_state") != "installed":
        raise SnapshotError("revision_unverified")
    observed = timestamp(value.get("checked_at"), now)
    if observed < installation["data"]["installed_at"]:
        raise SnapshotError("revision_unverified")
    result = {"state": value.get("status")}
    for name in ("desired_commit", "applied_commit"):
        if value.get(name):
            result[name] = value[name]
    if value.get("last_success_at") is not None:
        result["last_success_at"] = timestamp(value["last_success_at"], observed)
    return evidence(data("reconciliation", result), observed_at=observed)


def clock_state(sources: Mapping[str, dict], now: int) -> str:
    if any(source["error"] == "clock_uncertain" for source in sources.values()):
        return "uncertain"
    health = sources["health"]
    observed = health["last_success_at"]
    if observed is not None and now - observed <= STALE_AFTER["health"]:
        for check in health["data"].get("checks", []):
            if check["id"] == "clock":
                if check["status"] != "ok":
                    return "uncertain"
                return "synchronized" if health["availability"] == "available" else "unknown"
    return "unknown"


def build_snapshot(controller: str, documents: Mapping[str, dict | None], errors: Mapping[str, str], *,
                   now: int, previous: dict | None = None) -> dict:
    if not isinstance(controller, str) or not CONTROLLER_ID.fullmatch(controller) or not integer(now):
        raise SnapshotError("invalid_argument")
    if previous is not None:
        validate_snapshot(previous, controller=controller)
        if now < previous["published_at"]:
            raise SnapshotError("clock_uncertain")
    old = previous["sources"] if previous else {}
    sources = {name: failure("unsupported") for name in SOURCE_NAMES}

    def observe(name: str, source: str, projector) -> None:
        try:
            if errors.get(source):
                raise SnapshotError(errors[source])
            value = documents.get(source)
            if not isinstance(value, dict):
                raise SnapshotError("source_unavailable")
            sources[name] = projector(value)
        except SnapshotError as error:
            retained = old.get(name)
            if name in {"configuration", "reconciliation"} and retained and retained["data"]:
                installation = sources["installation"]
                prior_installation = old["installation"]
                if (installation["availability"] != "available"
                        or installation["data"].get("record_state") != "installed"
                        or prior_installation["availability"] != "available"
                        or prior_installation["data"] != installation["data"]
                        or retained["last_success_at"] < installation["data"]["installed_at"]):
                    sources[name] = failure("revision_unverified")
                    return
            sources[name] = failure(error.code, retained)
            if name == "controller":
                sources[name]["data"].pop("matches_reviewed_maximum", None)

    if errors.get("installation") == "not_found":
        sources["installation"] = evidence({"record_state": "absent"}, observed_at=now)
    else:
        observe("installation", "installation", lambda v: project_installation(v, controller, now))
    observe("configuration", "rendered", lambda v: project_configuration(v, sources["installation"], controller))
    observe("controller", "controller", lambda v: project_controller(v, controller, now, sources["installation"]))
    observe("health", "health", lambda v: project_health(v, controller, now))
    observe("reconciliation", "reconciliation", lambda v: project_reconciliation(v, now, sources["installation"]))
    value = {"schema_version": 1, "role": "worker", "controller": controller,
             "published_at": now, "clock_state": clock_state(sources, now), "sources": sources}
    validate_snapshot(value, controller=controller)
    return value


def validate_snapshot(value: Any, *, controller: str) -> None:
    try:
        _validate_snapshot(value, controller=controller)
    except SnapshotError:
        raise
    except (TypeError, ValueError, KeyError, OverflowError, RecursionError) as error:
        raise SnapshotError("invalid_snapshot") from error


def _validate_snapshot(value: Any, *, controller: str) -> None:
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise SnapshotError("unsupported_schema")
    if set(value) != {"schema_version", "role", "controller", "published_at", "clock_state", "sources"}:
        raise SnapshotError("invalid_snapshot")
    if value["role"] != "worker" or not isinstance(controller, str) or not CONTROLLER_ID.fullmatch(controller) or value["controller"] != controller:
        raise SnapshotError("identity_mismatch")
    if not integer(value["published_at"]) or value["clock_state"] not in {"synchronized", "uncertain", "unknown"}:
        raise SnapshotError("invalid_snapshot")
    if not isinstance(value["sources"], dict) or set(value["sources"]) != SOURCE_NAMES:
        raise SnapshotError("invalid_snapshot")
    for name, source in value["sources"].items():
        if not isinstance(source, dict) or set(source) != {"source_version", "availability", "observed_at", "last_success_at", "error", "data"}:
            raise SnapshotError("invalid_snapshot")
        if source["source_version"] is not None and (type(source["source_version"]) is not int or source["source_version"] != 1):
            raise SnapshotError("unsupported_schema")
        if source["availability"] not in AVAILABILITY or (source["error"] is not None and source["error"] not in ERRORS):
            raise SnapshotError("invalid_snapshot")
        for field in ("observed_at", "last_success_at"):
            if source[field] is not None:
                timestamp(source[field], value["published_at"])
        if not isinstance(source["data"], dict):
            raise SnapshotError("invalid_snapshot")
        data(name, source["data"])
        if source["data"]:
            validate_data(name, source["data"])
        if source["availability"] == "available":
            if source["source_version"] != 1 or source["observed_at"] is None or source["last_success_at"] != source["observed_at"] or source["error"] is not None:
                raise SnapshotError("invalid_snapshot")
            if name in UNSUPPORTED_SOURCES:
                raise SnapshotError("invalid_snapshot")
            validate_data(name, source["data"])
        elif source["observed_at"] is not None or (source["data"] and source["last_success_at"] is None):
            raise SnapshotError("invalid_snapshot")
        if name == "installation" and "installed_at" in source["data"]:
            timestamp(source["data"]["installed_at"], source["last_success_at"])
        if name == "reconciliation" and "last_success_at" in source["data"]:
            timestamp(source["data"]["last_success_at"], source["last_success_at"])
    policy = value["sources"]["installation"]
    configuration = value["sources"]["configuration"]
    reconciliation = value["sources"]["reconciliation"]
    runtime = value["sources"]["controller"]
    if value["clock_state"] != clock_state(value["sources"], value["published_at"]):
        raise SnapshotError("invalid_snapshot")
    if configuration["data"]:
        configured = configuration["data"]
        installed = policy["data"]
        if (policy["availability"] != "available" or installed.get("record_state") != "installed"
                or configured["applied_commit"] != installed["applied_commit"]
                or configured["engine_ref"] != installed["engine_ref"]
                or configuration["last_success_at"] != installed["installed_at"]
                or configured["maximum_runners"] != (installed["reviewed_maximum"] if installed["policy_state"] == "active" else 0)):
            raise SnapshotError("invalid_snapshot")
    if "matches_reviewed_maximum" in runtime["data"]:
        if policy["availability"] != "available" or "reviewed_maximum" not in policy["data"]:
            raise SnapshotError("invalid_snapshot")
        matches = runtime["data"]["maximum"] == policy["data"]["reviewed_maximum"]
        if runtime["data"]["matches_reviewed_maximum"] != matches:
            raise SnapshotError("invalid_snapshot")
    if reconciliation["data"]:
        if (policy["availability"] != "available" or policy["data"].get("record_state") != "installed"
                or reconciliation["last_success_at"] < policy["data"]["installed_at"]):
            raise SnapshotError("invalid_snapshot")
    try:
        encoded = (json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n").encode()
    except (ValueError, TypeError, UnicodeError) as error:
        raise SnapshotError("invalid_snapshot") from error
    if len(encoded) > MAX_BYTES:
        raise SnapshotError("snapshot_too_large")


class SnapshotReader:
    def __init__(self, base: Path, *, observer_gid: int, controller: str, backend_bearer: str):
        if not isinstance(controller, str) or not CONTROLLER_ID.fullmatch(controller) or not isinstance(backend_bearer, str) or not BEARER.fullmatch(backend_bearer):
            raise SnapshotError("invalid_reader_configuration")
        self.base, self.observer_gid, self.controller, self._bearer = base, observer_gid, controller, backend_bearer

    def read(self, headers: Mapping[str, str], *, now: int | None = None) -> dict:
        if not isinstance(headers, Mapping):
            raise SnapshotError("authentication_failed")
        authorization = [value for key, value in headers.items() if isinstance(key, str) and key.lower() == "authorization"]
        if (len(authorization) != 1 or not isinstance(authorization[0], str) or len(authorization[0]) != 50 or not authorization[0].isascii()
                or not hmac.compare_digest(authorization[0], "Bearer " + self._bearer)):
            raise SnapshotError("authentication_failed")
        value = read_endpoint(self.base, self.observer_gid)
        validate_snapshot(value, controller=self.controller)
        now = int(time.time()) if now is None else now
        if not integer(now):
            raise SnapshotError("invalid_argument")
        uncertain = value["clock_state"] == "uncertain" or now < value["published_at"]
        if uncertain:
            value["clock_state"] = "uncertain"
        for name, source in value["sources"].items():
            observed = source["observed_at"]
            source["freshness"] = ("unknown" if uncertain or source["availability"] != "available" else
                                   "recorded" if name not in STALE_AFTER else
                                   "stale" if now - observed > STALE_AFTER[name] else "current")
        value["publisher_freshness"] = "unknown" if uncertain else "stale" if now - value["published_at"] > 120 else "current"
        return value


def publish(controller: str, *, observer_gid: int, root: Path = Path("/"), now: int | None = None) -> dict:
    base = root / "var/lib/ci-fleet-observer"
    with publication_lock(base, observer_gid) as directory_fd:
        previous = None
        try:
            previous = read_endpoint(base, observer_gid, _directory_fd=directory_fd)
            validate_snapshot(previous, controller=controller)
        except StoreError as error:
            if error.code not in {"not_found", "invalid_json"}:
                raise
            previous = None
        except SnapshotError as error:
            if error.code not in {"invalid_snapshot", "source_invalid", "snapshot_too_large"}:
                raise
            previous = None
        documents, errors = collect_documents(controller, root=root)
        value = build_snapshot(controller, documents, errors, now=int(time.time()) if now is None else now, previous=previous)
        publish_endpoint(base, value, observer_gid, _directory_fd=directory_fd)
        return value


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish an allowlisted worker snapshot into an already prepared export tree.")
    parser.add_argument("--controller", required=True)
    parser.add_argument("--observer-group", required=True)
    args = parser.parse_args()
    try:
        publish(args.controller, observer_gid=grp.getgrnam(args.observer_group).gr_gid)
    except (StoreError, SnapshotError, KeyError):
        print("endpoint_snapshot_unavailable")
        return 1
    print("endpoint_snapshot_published")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
