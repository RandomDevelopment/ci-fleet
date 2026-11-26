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

from endpoint_snapshot_sources import CAPACITY_METRICS, CAPACITY_PERIOD, SourceError, capacity_aggregate, collect_documents
from endpoint_snapshot_store import StoreError, publication_lock, publish_endpoint, read_endpoint

MAX_BYTES = 65_536
CONTROLLER_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
COMMIT = re.compile(r"[0-9a-f]{40}")
VERSION = re.compile(r"[A-Za-z0-9_.+-]{1,64}")
BEARER = re.compile(r"[A-Za-z0-9_-]{43}")
AVAILABILITY = {"available", "not_configured", "unsupported", "unavailable", "unknown"}
ERRORS = {"not_found", "source_unsafe", "source_too_large", "source_invalid", "source_unavailable",
          "source_timeout", "unsupported", "identity_mismatch", "clock_uncertain", "revision_unverified",
          "observation_unknown"}
HEALTH_IDS = {
    "disk_root", "disk_docker", "inode_root", "inode_docker", "memory", "load", "swap", "oom",
    "docker", "docker_network_inspection", "docker_network_headroom", "docker_network_legacy",
    "docker_default_bridge", "controller", "controller_status", "docker_socket_proxy", "restarts",
    "capacity", "capacity_service", "managed_unhealthy", "managed_inactive", "stale_container", "stale_image", "stale_network", "stale_volume",
    "stale_build_cache_byte", "stale_build_cache", "cleanup", "drift", "timer_health", "timer_cleanup", "timer_drift",
    "timer_updates", "timer_reconcile", "timer_capacity", "reconcile", "updates", "updates_service", "reboot", "clock", "backup", "reconciliation",
    "status_delivery",
}
BASELINE_HEALTH_IDS = {
    "disk_root", "disk_docker", "inode_root", "inode_docker", "memory", "load", "swap", "oom", "docker",
    "controller", "controller_status", "restarts", "capacity", "managed_unhealthy", "managed_inactive",
    "stale_container", "stale_image", "stale_network", "stale_volume", "stale_build_cache",
    "cleanup", "drift", "timer_health", "timer_cleanup", "timer_drift", "updates", "reboot", "clock", "backup",
}
STATES = {"active", "drained", "disabled"}
RECONCILIATION = {"bootstrap", "converged", "drift", "failed", "invalid", "pending", "reconciling", "rolled_back"}
STALE_AFTER = {"controller": 120, "enrollment_identity": 120, "enrollment_listener": 120,
               "runtime_revision": 120, "manager_revision": 120, "staged_manager_revision": 900,
               "image_verification": 120, "capacity": 120, "health": 900, "reconciliation": 900,
               "enrollment_reporting": 900, "desired_engine_revision": 900, "transaction": 900}
UNSUPPORTED_SOURCES = {"enrollment_owner_confirmation", "jobs"}
CONTROLLER_ENROLLMENT = {
    "enrollment_authentication": ("session_opened_at", "authenticated"),
    "enrollment_scale_set": ("matched_at", "matched"),
    "controller_session": ("session_opened_at", "opened"),
    "enrollment_listener": ("polled_at", "poll_succeeded"),
}
TRANSACTION_REASONS = {"docker_disk_capacity", "installer_precondition"}
FOUNDATION_SOURCES = {"installation", "configuration", "controller", "health", "reconciliation"}


class SnapshotError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def integer(value: Any) -> bool:
    return type(value) is int and 0 <= value <= 2**63 - 1


def rendered_integer(value: Any) -> int:
    if not isinstance(value, str) or re.fullmatch(r"0|[1-9][0-9]*", value) is None:
        raise SnapshotError("source_invalid")
    result = int(value)
    if not integer(result):
        raise SnapshotError("source_invalid")
    return result


def token(value: Any) -> bool:
    return isinstance(value, str) and CONTROLLER_ID.fullmatch(value) is not None


def commit(value: Any) -> bool:
    return isinstance(value, str) and COMMIT.fullmatch(value) is not None and value != "0" * 40


def choice(*values: str):
    return lambda value: isinstance(value, str) and value in values


def labels(value: Any) -> bool:
    return (isinstance(value, list) and len(value) >= 1
            and all(token(item) and item != "self-hosted" for item in value)
            and len(set(value)) == len(value))


def checks(value: Any) -> bool:
    return (isinstance(value, list) and len(value) <= len(HEALTH_IDS)
            and all(isinstance(item, dict) and set(item) == {"id", "status"}
                    and item["id"] in HEALTH_IDS and item["status"] in {"ok", "warning", "critical"} for item in value)
            and len({item["id"] for item in value}) == len(value))


def capacity_metrics(value: Any) -> bool:
    required = {"host_memory_used_bytes", "host_swap_used_bytes",
                "runner_cpu_percent", "runner_memory_used_bytes", "runner_memory_limit_bytes"}
    if not isinstance(value, dict) or not required <= value.keys() <= CAPACITY_METRICS:
        return False
    for name, percentiles in value.items():
        if not isinstance(percentiles, dict) or set(percentiles) != {"p50", "p95"}:
            return False
        for number in percentiles.values():
            if name.endswith("_percent"):
                if type(number) not in (int, float) or not math.isfinite(number) or number < 0:
                    return False
                if name == "host_cpu_percent" and number > 100:
                    return False
            elif not integer(number):
                return False
        if percentiles["p50"] > percentiles["p95"]:
            return False
    return True


DATA_FIELDS = {
    "installation": {
        "record_state": choice("installed", "absent"), "lifecycle": choice("experimental", "stable", "retiring"),
        "policy_state": choice(*STATES), "installed_at": integer, "applied_commit": commit,
        "engine_ref": commit, "architecture": choice("amd64", "arm64"), "pool": token,
        "reviewed_maximum": lambda v: integer(v) and v > 0, "capacity_budget": lambda v: integer(v) and v > 0, "scale_set_name": token,
        "reporting_configured": lambda v: type(v) is bool, "reporting_required": lambda v: type(v) is bool,
        "network_policy_configured": lambda v: type(v) is bool,
    },
    "configuration": {
        "applied_commit": commit, "engine_ref": commit, "routing_labels": labels,
        "runner_cpu_cores": lambda v: integer(v) and v >= 1,
        "runner_memory_mib": lambda v: integer(v) and v >= 512,
        "minimum_runners": lambda v: type(v) is int and v == 0, "maximum_runners": integer,
        "storage_retention_configured": lambda v: type(v) is bool,
    },
    "controller": {"software_version": lambda v: isinstance(v, str) and len(v) <= 64 and VERSION.fullmatch(v) is not None,
                   "current": integer, "busy": integer, "maximum": integer,
                   "matches_reviewed_maximum": lambda v: type(v) is bool},
    "health": {"status": choice("healthy", "warning", "unhealthy", "maintenance"), "checks": checks,
               "desired_state": choice(*STATES)},
    "reconciliation": {"state": choice(*RECONCILIATION), "desired_commit": commit, "applied_commit": commit,
                       "last_success_at": integer},
    "boot": {"boot_at": lambda v: integer(v) and v > 0},
    **{name: {"state": choice(state), "config_commit": commit} for name, (_, state) in CONTROLLER_ENROLLMENT.items()},
    "enrollment_identity": {"state": choice("present", "absent")},
    "enrollment_reporting": {"state": choice("delivered", "failed"), "config_commit": commit},
    "runtime_revision": {"revision": commit},
    "manager_revision": {"revision": commit},
    "staged_manager_revision": {"revision": commit},
    "desired_engine_revision": {"revision": commit, "config_commit": commit},
    "image_verification": {"state": choice("bindings_verified"), "revision": commit,
                           "architecture": choice("amd64", "arm64")},
    "capacity": {"period_seconds": lambda v: type(v) is int and v == CAPACITY_PERIOD,
                 "samples": lambda v: integer(v) and 0 < v <= 26000,
                 "runner_observations": lambda v: integer(v) and v > 0, "metrics": capacity_metrics},
    "transaction": {"outcome": choice("rejected", "rollback_verified", "applied", "rollback_unverified"),
                    "phase": choice("pre_transaction"),
                    "reason": choice(*TRANSACTION_REASONS)},
    **{name: {} for name in UNSUPPORTED_SOURCES},
}
SOURCE_NAMES = set(DATA_FIELDS)
LEGACY_SOURCE_NAMES = SOURCE_NAMES - {"boot", "controller_session"}


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
        "health": {"status", "checks", "desired_state"},
        "reconciliation": {"state"},
        "transaction": {"outcome"},
    }.get(name, set(DATA_FIELDS[name]))
    if name == "installation":
        if value.get("record_state") == "absent":
            if set(value) != {"record_state"}:
                raise SnapshotError("invalid_snapshot")
        else:
            required = set(DATA_FIELDS["installation"])
    if not required <= value.keys():
        raise SnapshotError("invalid_snapshot")
    if (name == "installation" and value["record_state"] == "installed"
            and value["policy_state"] != "disabled" and value["reviewed_maximum"] > value["capacity_budget"]):
        raise SnapshotError("invalid_snapshot")
    if (name == "installation" and value["record_state"] == "installed"
            and value["reporting_required"] and not value["reporting_configured"]):
        raise SnapshotError("invalid_snapshot")
    if name == "health":
        if not BASELINE_HEALTH_IDS <= {check["id"] for check in value["checks"]}:
            raise SnapshotError("invalid_snapshot")
        rank = {"healthy": 0, "maintenance": 0, "warning": 1, "unhealthy": 2}[value["status"]]
        if (any({"ok": 0, "warning": 1, "critical": 2}[check["status"]] > rank for check in value["checks"])
                or value["status"] == "maintenance" and value["desired_state"] == "active"
                or value["status"] == "healthy" and value["desired_state"] != "active"):
            raise SnapshotError("invalid_snapshot")
    if name == "controller" and not value["busy"] <= value["current"] <= value["maximum"]:
        raise SnapshotError("invalid_snapshot")
    if name == "configuration" and value["minimum_runners"] > value["maximum_runners"]:
        raise SnapshotError("invalid_snapshot")
    if name == "capacity" and value["runner_observations"] < value["samples"]:
        raise SnapshotError("invalid_snapshot")
    if name == "transaction":
        expected = set(DATA_FIELDS[name]) if value["outcome"] == "rejected" else {"outcome"}
        if set(value) != expected:
            raise SnapshotError("invalid_snapshot")


def evidence(value: dict | None = None, *, observed_at: int | None = None,
             availability: str = "available", error: str | None = None) -> dict:
    return {"source_version": 1 if availability == "available" else None, "availability": availability,
            "observed_at": observed_at, "last_success_at": observed_at, "error": error, "data": value or {}}


def failure(error: str, previous: dict | None = None) -> dict:
    error = error if error in ERRORS else "source_unavailable"
    availability = "unsupported" if error == "unsupported" else "unknown" if error in {"clock_uncertain", "revision_unverified", "observation_unknown"} else "unavailable"
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
    required = set(DATA_FIELDS["installation"])
    if not required <= result.keys():
        raise SnapshotError("source_invalid")
    data("installation", result)
    if controller not in result["scale_set_name"]:
        raise SnapshotError("identity_mismatch")
    if result["policy_state"] != "disabled" and result["reviewed_maximum"] > result["capacity_budget"]:
        raise SnapshotError("source_invalid")
    return evidence(result, observed_at=observed)


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
        if (value["CI_FLEET_CONTROLLER_STATE"] != policy["policy_state"]
                or value["CI_FLEET_SCALE_SET_NAME"] != policy["scale_set_name"]
                or rendered_integer(value["CI_FLEET_CONFIGURED_MAX_RUNNERS"]) != policy["reviewed_maximum"]
                or rendered_integer(value["CI_FLEET_CAPACITY_BUDGET"]) != policy["capacity_budget"]):
            raise SnapshotError("identity_mismatch")
        result = {"applied_commit": policy["applied_commit"], "engine_ref": policy["engine_ref"],
                  "routing_labels": value["CI_FLEET_LABELS"].split(","),
                  "runner_cpu_cores": rendered_integer(value["CI_FLEET_RUNNER_CPUS"]),
                  "runner_memory_mib": rendered_integer(value["CI_FLEET_RUNNER_MEMORY_MIB"]),
                  "minimum_runners": rendered_integer(value["CI_FLEET_MIN_RUNNERS"]),
                  "maximum_runners": rendered_integer(value["CI_FLEET_MAX_RUNNERS"]),
                  "storage_retention_configured": value.get("CI_FLEET_DOCKER_STORAGE_RETENTION") == "exclusive-fleet-daemon-v1"}
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise SnapshotError("source_invalid") from error
    if result["maximum_runners"] != (policy["reviewed_maximum"] if policy["policy_state"] == "active" else 0):
        raise SnapshotError("source_invalid")
    if policy["scale_set_name"] in result["routing_labels"]:
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


def project_health(value: dict, controller: str, now: int, installation: dict) -> dict:
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
    result = data("health", {"status": value.get("status"), "checks": approved, "desired_state": value.get("desired_state")})
    observed = timestamp(value.get("timestamp"), now)
    if installation["availability"] == "available" and installation["data"].get("record_state") == "installed":
        if result["desired_state"] != installation["data"]["policy_state"] or observed < installation["data"]["installed_at"]:
            raise SnapshotError("revision_unverified")
    return evidence(result, observed_at=observed)


def project_reconciliation(value: dict, now: int, installation: dict, controller: str) -> dict:
    if installation["availability"] != "available" or installation["data"].get("record_state") != "installed":
        raise SnapshotError("revision_unverified")
    if "controller" in value and value["controller"] != controller:
        raise SnapshotError("identity_mismatch")
    observed = timestamp(value.get("checked_at"), now)
    if observed < installation["data"]["installed_at"]:
        raise SnapshotError("revision_unverified")
    result = {"state": value.get("status")}
    for name in ("desired_commit", "applied_commit"):
        if value.get(name):
            result[name] = value[name]
    if value.get("last_success_at") is not None:
        result["last_success_at"] = timestamp(value["last_success_at"], observed)
    if result["state"] == "converged" and any(result.get(name) != installation["data"]["applied_commit"]
                                               for name in ("desired_commit", "applied_commit")):
        raise SnapshotError("revision_unverified")
    return evidence(data("reconciliation", result), observed_at=observed)


def require_observed(source: dict) -> dict:
    if source["availability"] != "available":
        raise SnapshotError(source["error"] or "observation_unknown")
    return source["data"]


def project_controller_enrollment(name: str, value: dict, sources: dict) -> dict:
    running = require_observed(sources["controller"])
    observation = value.get("enrollment")
    if observation is None:
        raise SnapshotError("unsupported")
    boot = require_observed(sources["boot"])
    policy = require_observed(sources["installation"])
    configuration = require_observed(sources["configuration"])
    if not isinstance(observation, dict):
        raise SnapshotError("source_invalid")
    if "config_commit" not in observation:
        raise SnapshotError("unsupported")
    if observation["config_commit"] != policy.get("applied_commit"):
        raise SnapshotError("revision_unverified")
    if observation.get("scale_set_name") != policy.get("scale_set_name"):
        raise SnapshotError("identity_mismatch")
    if (running["software_version"] != policy.get("engine_ref")
            or not labels(observation.get("routing_labels"))
            or set(observation["routing_labels"]) != set(configuration["routing_labels"])):
        raise SnapshotError("revision_unverified")
    reported = sources["controller"]["observed_at"]
    matched = timestamp(observation.get("matched_at"), reported)
    opened = timestamp(observation.get("session_opened_at"), reported)
    if not boot["boot_at"] <= matched <= opened:
        raise SnapshotError("revision_unverified")
    field, state = CONTROLLER_ENROLLMENT[name]
    if field == "polled_at":
        if field not in observation:
            raise SnapshotError("observation_unknown")
        if timestamp(observation[field], reported) < opened:
            raise SnapshotError("clock_uncertain")
    return evidence({"state": state, "config_commit": observation["config_commit"]}, observed_at=observation[field])


def project_desired_engine(value: dict, sources: dict, controller: str) -> dict:
    if "desired_engine" not in value:
        raise SnapshotError("unsupported")
    require_observed(sources["reconciliation"])
    desired = value["desired_engine"]
    if not isinstance(desired, dict) or desired.get("lookup") != "default_branch":
        raise SnapshotError("source_invalid")
    if desired.get("controller") != controller:
        raise SnapshotError("identity_mismatch")
    installed = sources["installation"]["data"]
    config_commit, revision = desired.get("config_commit"), desired.get("revision")
    if config_commit == installed["applied_commit"] and revision != installed["engine_ref"]:
        raise SnapshotError("revision_unverified")
    return evidence({"revision": revision, "config_commit": config_commit},
                    observed_at=timestamp(desired.get("observed_at"), sources["reconciliation"]["observed_at"]))


def project_transaction(value: dict, sources: dict, controller: str) -> dict:
    reconciliation = require_observed(sources["reconciliation"])
    policy = require_observed(sources["installation"])
    if (value.get("controller") != controller
            or reconciliation.get("applied_commit") != policy["applied_commit"]
            or not commit(reconciliation.get("desired_commit"))):
        raise SnapshotError("revision_unverified")
    phase, reason = value.get("transaction_phase"), value.get("failure_reason")
    outcome = value.get("transaction_outcome")
    if phase not in (None, "") or reason not in (None, ""):
        if (phase != "pre_transaction" or not isinstance(reason, str) or reason not in TRANSACTION_REASONS
                or reconciliation["state"] != "failed" or outcome is not None):
            raise SnapshotError("source_invalid")
        result = {"outcome": "rejected", "phase": phase, "reason": reason}
    elif reconciliation["state"] == "rolled_back" and outcome in (None, "rollback_verified"):
        result = {"outcome": "rollback_verified"}
    elif (reconciliation["state"] == "failed" and outcome in ("applied", "rollback_unverified")
          or reconciliation["state"] == "converged" and outcome == "applied"):
        if outcome == "applied" and reconciliation["applied_commit"] != reconciliation["desired_commit"]:
            raise SnapshotError("revision_unverified")
        result = {"outcome": outcome}
    else:
        raise SnapshotError("observation_unknown" if outcome is None else "source_invalid")
    return evidence(result, observed_at=sources["reconciliation"]["observed_at"])


def delivery_consistent(state: str, health: dict) -> bool:
    warning = any(check["id"] == "status_delivery" and check["status"] in {"warning", "critical"}
                  for check in health["checks"])
    return (state == "failed") == warning


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
                raise SnapshotError("source_unavailable" if name in FOUNDATION_SOURCES else "unsupported")
            observed = projector(value)
            if observed["availability"] == "available":
                validate_data(name, observed["data"])
            sources[name] = observed
        except (SnapshotError, SourceError) as error:
            retained = old.get(name) if name in FOUNDATION_SOURCES else None
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
            sources[name] = failure("source_invalid" if error.code == "invalid_snapshot" else error.code, retained)
            if name == "controller":
                sources[name]["data"].pop("matches_reviewed_maximum", None)
            if name == "health" and sources[name]["data"]:
                installation = sources["installation"]
                if installation["availability"] == "available" and installation["data"].get("record_state") == "installed":
                    if (sources[name]["data"]["desired_state"] != installation["data"]["policy_state"]
                            or sources[name]["last_success_at"] < installation["data"]["installed_at"]):
                        sources[name] = failure("revision_unverified")

    if errors.get("installation") == "not_found":
        sources["installation"] = evidence({"record_state": "absent"}, observed_at=now)
    else:
        observe("installation", "installation", lambda v: project_installation(v, controller, now))
    observe("configuration", "rendered", lambda v: project_configuration(v, sources["installation"], controller))
    observe("controller", "controller", lambda v: project_controller(v, controller, now, sources["installation"]))
    observe("health", "health", lambda v: project_health(v, controller, now, sources["installation"]))
    observe("reconciliation", "reconciliation", lambda v: project_reconciliation(v, now, sources["installation"], controller))
    observe("boot", "boot", lambda v: evidence({"boot_at": v.get("boot_at")},
                                               observed_at=timestamp(v.get("boot_at"), now)))
    for name in CONTROLLER_ENROLLMENT:
        observe(name, "controller", lambda v: project_controller_enrollment(name, v, sources))
    observe("desired_engine_revision", "reconciliation", lambda v: project_desired_engine(v, sources, controller))
    observe("transaction", "reconciliation", lambda v: project_transaction(v, sources, controller))
    for name in ("runtime_revision", "manager_revision"):
        observe(name, name, lambda v: evidence({"revision": v.get("revision")},
                                               observed_at=timestamp(v.get("observed_at"), now)))

    manager, runtime, desired = (sources[name]["data"].get("revision")
                                 for name in ("manager_revision", "runtime_revision", "desired_engine_revision"))
    if manager and runtime and manager == desired and manager != runtime and runtime == sources["installation"]["data"].get("engine_ref"):
        selected_at = min(sources[name]["observed_at"]
                          for name in ("manager_revision", "runtime_revision", "desired_engine_revision"))
        sources["staged_manager_revision"] = evidence({"revision": manager}, observed_at=selected_at)
    elif sources["manager_revision"]["availability"] != "unsupported":
        sources["staged_manager_revision"] = failure("observation_unknown")

    def with_configuration(result: dict, observed: int) -> dict:
        require_observed(sources["configuration"])
        if observed < sources["installation"]["observed_at"]:
            raise SnapshotError("revision_unverified")
        return evidence(result, observed_at=observed)

    observe("enrollment_identity", "enrollment_identity", lambda v: with_configuration(
        {"state": v.get("state")}, timestamp(v.get("observed_at"), now)))
    def image_verification(v: dict) -> dict:
        installed = sources["installation"]["data"]
        if v.get("revision") != installed.get("engine_ref") or v.get("architecture") != installed.get("architecture"):
            raise SnapshotError("revision_unverified")
        return with_configuration({"state": "bindings_verified", "revision": v.get("revision"),
                                   "architecture": v.get("architecture")}, timestamp(v.get("observed_at"), now))

    observe("image_verification", "image_verification", image_verification)

    def reporting(v: dict) -> dict:
        health = require_observed(sources["health"])
        policy = require_observed(sources["installation"])
        if policy.get("record_state") != "installed":
            raise SnapshotError("revision_unverified")
        if "status_reporting" not in v:
            raise SnapshotError("unsupported")
        report = v["status_reporting"]
        if not isinstance(report, dict):
            raise SnapshotError("source_invalid")
        if report.get("config_commit") != policy["applied_commit"]:
            raise SnapshotError("revision_unverified")
        observed = timestamp(report.get("observed_at"), now)
        if observed < sources["health"]["observed_at"]:
            raise SnapshotError("clock_uncertain")
        state = report.get("state")
        if state == "not_configured":
            if policy["reporting_required"]:
                raise SnapshotError("revision_unverified")
            return evidence(availability="not_configured")
        if not delivery_consistent(state, health):
            raise SnapshotError("source_invalid")
        return evidence({"state": state, "config_commit": policy["applied_commit"]}, observed_at=observed)

    def capacity(v: dict) -> dict:
        require_observed(sources["configuration"])
        pool = sources["installation"]["data"]["pool"]
        configured_pool = documents["rendered"].get("CI_FLEET_POOL", controller)
        if configured_pool not in {pool, controller}:
            raise SnapshotError("revision_unverified")
        if not isinstance(v.get("history"), bytes):
            raise SnapshotError("source_invalid")
        latest, aggregate = capacity_aggregate(v["history"], configured_pool, now)
        return evidence({"period_seconds": CAPACITY_PERIOD, **aggregate}, observed_at=latest)

    observe("enrollment_reporting", "health", reporting)
    observe("capacity", "capacity", capacity)
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
    if not isinstance(value["sources"], dict) or set(value["sources"]) not in (SOURCE_NAMES, LEGACY_SOURCE_NAMES):
        raise SnapshotError("invalid_snapshot")
    legacy = set(value["sources"]) == LEGACY_SOURCE_NAMES
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
        if source["data"] or source["last_success_at"] is not None:
            if source["source_version"] != 1:
                raise SnapshotError("unsupported_schema")
            validate_data(name, source["data"])
        if source["availability"] == "available":
            if source["source_version"] != 1 or source["observed_at"] is None or source["last_success_at"] != source["observed_at"] or source["error"] is not None:
                raise SnapshotError("invalid_snapshot")
            if name in UNSUPPORTED_SOURCES:
                raise SnapshotError("invalid_snapshot")
            validate_data(name, source["data"])
        elif source["availability"] == "not_configured":
            if source != evidence(availability="not_configured"):
                raise SnapshotError("invalid_snapshot")
        elif source != failure(source["error"], source):
            raise SnapshotError("invalid_snapshot")
        if (name in UNSUPPORTED_SOURCES or legacy and name not in FOUNDATION_SOURCES) and source != failure("unsupported"):
            raise SnapshotError("invalid_snapshot")
        if name not in FOUNDATION_SOURCES and source["data"] and source["availability"] != "available":
            raise SnapshotError("invalid_snapshot")
        if name == "installation" and "installed_at" in source["data"]:
            timestamp(source["data"]["installed_at"], source["last_success_at"])
        if name == "reconciliation" and "last_success_at" in source["data"]:
            timestamp(source["data"]["last_success_at"], source["last_success_at"])
    policy = value["sources"]["installation"]
    configuration = value["sources"]["configuration"]
    reconciliation = value["sources"]["reconciliation"]
    health = value["sources"]["health"]
    runtime = value["sources"]["controller"]
    if policy["data"].get("record_state") == "installed" and controller not in policy["data"]["scale_set_name"]:
        raise SnapshotError("invalid_snapshot")
    if value["clock_state"] != clock_state(value["sources"], value["published_at"]):
        raise SnapshotError("invalid_snapshot")
    if configuration["data"]:
        configured = configuration["data"]
        installed = policy["data"]
        if (policy["availability"] != "available" or installed.get("record_state") != "installed"
                or configured["applied_commit"] != installed["applied_commit"]
                or configured["engine_ref"] != installed["engine_ref"]
                or configuration["last_success_at"] != installed["installed_at"]
                or installed["scale_set_name"] in configured["routing_labels"]
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
        if reconciliation["data"]["state"] == "converged" and any(
                reconciliation["data"].get(name) != policy["data"]["applied_commit"] for name in ("desired_commit", "applied_commit")):
            raise SnapshotError("invalid_snapshot")
    if health["data"] and policy["availability"] == "available" and policy["data"].get("record_state") == "installed":
        if (health["data"]["desired_state"] != policy["data"]["policy_state"]
                or health["last_success_at"] < policy["data"]["installed_at"]):
            raise SnapshotError("invalid_snapshot")
    validate_new_sources(value["sources"])
    try:
        encoded = (json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n").encode()
    except (ValueError, TypeError, UnicodeError) as error:
        raise SnapshotError("invalid_snapshot") from error
    if len(encoded) > MAX_BYTES:
        raise SnapshotError("snapshot_too_large")


def validate_new_sources(sources: dict) -> None:
    def require(name: str) -> dict:
        source = sources.get(name)
        if not source or source["availability"] != "available":
            raise SnapshotError("invalid_snapshot")
        return source

    for name, source in sources.items():
        observed, approved = source["observed_at"], source["data"]
        if name == "enrollment_reporting" and source["availability"] == "not_configured":
            require("health")
            if require("installation")["data"].get("reporting_required") is not False:
                raise SnapshotError("invalid_snapshot")
        if name in FOUNDATION_SOURCES or not approved:
            continue
        if name == "boot" and observed != approved["boot_at"]:
            raise SnapshotError("invalid_snapshot")
        if name in CONTROLLER_ENROLLMENT:
            controller = require("controller")
            installed = require("installation")["data"]
            require("configuration")
            if (not require("boot")["observed_at"] <= observed <= controller["observed_at"]
                    or approved["config_commit"] != installed["applied_commit"]
                    or controller["data"]["software_version"] != installed["engine_ref"]):
                raise SnapshotError("invalid_snapshot")
            if name in {"enrollment_authentication", "controller_session", "enrollment_listener"}:
                session = require("controller_session")["observed_at"]
                if (require("enrollment_scale_set")["observed_at"] > session
                        or name == "enrollment_listener" and observed < session
                        or name == "enrollment_authentication" and observed != session):
                    raise SnapshotError("invalid_snapshot")
        if name in {"enrollment_identity", "image_verification", "capacity"}:
            require("configuration")
            installed = require("installation")["data"]
            if name != "capacity" and observed < installed["installed_at"]:
                raise SnapshotError("invalid_snapshot")
            if name == "image_verification" and (approved["revision"], approved["architecture"]) != (
                    installed["engine_ref"], installed["architecture"]):
                raise SnapshotError("invalid_snapshot")
        if name == "enrollment_reporting":
            health = require("health")
            if (approved["config_commit"] != require("installation")["data"].get("applied_commit")
                    or observed < health["observed_at"] or not delivery_consistent(approved["state"], health["data"])):
                raise SnapshotError("invalid_snapshot")
        if name in {"desired_engine_revision", "transaction"}:
            reconcile = require("reconciliation")
            config = reconcile["data"]
            if observed > reconcile["observed_at"] or name == "transaction" and observed != reconcile["observed_at"]:
                raise SnapshotError("invalid_snapshot")
            if name == "desired_engine_revision":
                installed = require("installation")["data"]
                if (approved["config_commit"] == installed["applied_commit"]
                        and approved["revision"] != installed["engine_ref"]):
                    raise SnapshotError("invalid_snapshot")
            elif (config.get("applied_commit") != require("installation")["data"]["applied_commit"]
                  or not commit(config.get("desired_commit"))
                  or approved["outcome"] not in {
                      "failed": {"applied", "rejected", "rollback_unverified"},
                      "rolled_back": {"rollback_verified"},
                      "converged": {"applied"},
                  }.get(config["state"], set())
                  or approved["outcome"] == "applied" and config["applied_commit"] != config["desired_commit"]):
                raise SnapshotError("invalid_snapshot")
        if name == "staged_manager_revision":
            manager, runtime = require("manager_revision"), require("runtime_revision")
            desired = require("desired_engine_revision")
            revision = approved["revision"]
            if (revision != manager["data"]["revision"] or revision == runtime["data"]["revision"]
                    or runtime["data"]["revision"] != require("installation")["data"]["engine_ref"]
                    or revision != desired["data"]["revision"]
                    or observed != min(manager["observed_at"], runtime["observed_at"], desired["observed_at"])):
                raise SnapshotError("invalid_snapshot")


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
        for name in SOURCE_NAMES - value["sources"].keys():
            value["sources"][name] = failure("unsupported")
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
        stage = value["sources"]["staged_manager_revision"]
        if stage["freshness"] == "current" and any(
                value["sources"][name]["freshness"] == "stale" for name in ("manager_revision", "runtime_revision")):
            stage["freshness"] = "stale"
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
