#!/usr/bin/env python3
"""Verify redaction, provenance, source failures, and authenticated readback."""
import copy
import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import endpoint_snapshot as snapshot
import health
from test_health import healthy_snapshot
import endpoint_snapshot_sources as sources
import endpoint_snapshot_store as store

CONTROLLER = "example-ci-01"
NOW = 1_700_000_100
BEARER = "a" * 43


def documents():
    return {
        "installation": {"schema_version": 1, "controller": CONTROLLER, "controller_state": "active",
                         "installed_at": dt.datetime.fromtimestamp(NOW - 100, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                         "config_ref": "1" * 40, "engine_ref": "2" * 40, "pool": "validation",
                         "lifecycle": "stable", "configured_max_runners": 4, "capacity_budget": 8,
                         "architecture": "amd64", "scale_set_name": "example-ci-01-scale-set",
                         "status_reporting_configured": True, "status_reporting_required": False,
                         "docker_network_policy_configured": False},
        "rendered": {"CI_FLEET_INSTANCE": CONTROLLER, "CI_FLEET_CONFIG_REF": "1" * 40,
                     "CI_FLEET_ENGINE_REF": "2" * 40, "CI_FLEET_DESIRED_STATE_SCHEMA": "3",
                     "CI_FLEET_LABELS": "docker-ci", "CI_FLEET_RUNNER_CPUS": "2",
                     "CI_FLEET_RUNNER_MEMORY_MIB": "4096", "CI_FLEET_MIN_RUNNERS": "0",
                     "CI_FLEET_MAX_RUNNERS": "4", "CI_FLEET_CONTROLLER_STATE": "active",
                     "CI_FLEET_CONFIGURED_MAX_RUNNERS": "4", "CI_FLEET_CAPACITY_BUDGET": "8"},
        "controller": {"controller": CONTROLLER, "software_version": "3" * 40,
                       "current": 2, "busy": 1, "maximum": 4, "generated_at": NOW - 10},
        "health": {"schema_version": 1, "controller": CONTROLLER, "desired_state": "active", "status": "healthy", "timestamp": NOW - 20,
                   "checks": [{"id": "clock", "status": "ok"}, {"id": "docker", "status": "ok"}]},
        "reconciliation": {"status": "converged", "desired_commit": "1" * 40, "applied_commit": "1" * 40,
                           "checked_at": NOW - 30, "last_success_at": NOW - 30},
    }


class SnapshotTests(unittest.TestCase):
    def build(self, docs=None, errors=None, **kwargs):
        return snapshot.build_snapshot(CONTROLLER, docs or documents(), errors or {}, now=kwargs.pop("now", NOW), **kwargs)

    def assert_snapshot_error(self, call, code=None):
        with self.assertRaises(snapshot.SnapshotError) as caught:
            call()
        if code is not None:
            self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), caught.exception.code)

    def test_keeps_applied_policy_running_revision_and_times_separate(self):
        value = self.build()
        installation = value["sources"]["installation"]
        runtime = value["sources"]["controller"]
        self.assertEqual(installation["data"]["engine_ref"], "2" * 40)
        self.assertEqual(runtime["data"]["software_version"], "3" * 40)
        self.assertEqual(installation["observed_at"], NOW - 100)
        self.assertEqual(runtime["observed_at"], NOW - 10)
        self.assertEqual(value["sources"]["reconciliation"]["observed_at"], NOW - 30)
        self.assertEqual(value["sources"]["configuration"]["observed_at"], NOW - 100)
        for name in snapshot.UNSUPPORTED_SOURCES:
            self.assertEqual(value["sources"][name]["availability"], "unsupported")
            self.assertEqual(value["sources"][name]["data"], {})
        snapshot.validate_snapshot(value, controller=CONTROLLER)

    def test_private_values_and_unapproved_health_details_never_escape(self):
        docs = documents()
        private = "PRIVATE-DETAIL-NEVER-EXPORT"
        docs["installation"].update(location=private, config_repository=private, credential_file=private)
        docs["rendered"].update(CI_FLEET_GITHUB_URL=private, CI_FLEET_GITHUB_APP_PRIVATE_KEY_FILE=private,
                                HTTPS_PROXY=private, CI_FLEET_CONTROLLER_IMAGE_DIGEST=private)
        docs["health"]["checks"][0].update(message=private, password=private, value=0)
        docs["health"]["checks"].append({"id": private, "status": "ok", "message": private})
        docs["reconciliation"]["message"] = private
        docs["controller"]["private_error"] = private
        value = self.build(docs)
        self.assertNotIn(private, json.dumps(value))
        self.assertEqual(value["sources"]["health"]["data"]["checks"][0], {"id": "clock", "status": "ok"})
        self.assertNotIn("metrics", value["sources"]["health"]["data"])

    def test_publisher_preserves_incumbent_during_clock_rollback(self):
        previous = self.build()
        with patch.object(snapshot, 'publication_lock'), \
             patch.object(snapshot, 'read_endpoint', return_value=previous), \
             patch.object(snapshot, 'collect_documents', return_value=(documents(), {})), \
             patch.object(snapshot, 'publish_endpoint') as publish:
            self.assert_snapshot_error(lambda: snapshot.publish(CONTROLLER, observer_gid=1, now=NOW - 5),
                                       'clock_uncertain')
            publish.assert_not_called()
        value = self.build(previous=previous, now=NOW + 1)
        self.assertEqual(value['sources']['controller']['observed_at'], NOW - 10)

    def test_authoritative_lifecycle_values_are_available(self):
        for lifecycle in ('experimental', 'stable', 'retiring'):
            with self.subTest(lifecycle=lifecycle):
                docs = documents(); docs['installation']['lifecycle'] = lifecycle
                value = self.build(docs)
                self.assertEqual(value['sources']['installation']['data']['lifecycle'], lifecycle)
                self.assertEqual(value['sources']['configuration']['availability'], 'available')

    def test_controller_version_matches_the_existing_status_contract(self):
        for version in ("dev", "unknown", "v1.2.3-rc.1+build", "3" * 40):
            with self.subTest(version=version):
                docs = documents()
                docs["controller"]["software_version"] = version
                source = self.build(docs)["sources"]["controller"]
                self.assertEqual(source["availability"], "available")
                self.assertEqual(source["data"]["software_version"], version)
                self.assertEqual(source["data"]["current"], 2)

    def test_capacity_and_dynamic_service_diagnostics_are_distinct_and_redacted(self):
        docs = documents()
        docs['health']['checks'] += [
            {'id': 'capacity', 'status': 'ok', 'configured': {'max': 4}, 'effective': {'max': 4}},
            {'id': 'capacity', 'status': 'critical', 'state': 'failed'},
            *({'id': name, 'status': 'warning', 'message': 'PRIVATE-DETAIL'}
              for name in ('stale_container', 'timer_capacity', 'reconcile')),
        ]
        docs['health']['status'] = 'unhealthy'
        value = self.build(docs)
        health = value['sources']['health']
        self.assertEqual(health['availability'], 'available')
        self.assertEqual({check['id'] for check in health['data']['checks']},
                         {'clock', 'docker', 'capacity', 'capacity_service', 'stale_container', 'timer_capacity', 'reconcile'})
        self.assertNotIn('PRIVATE-DETAIL', json.dumps(value))

    def test_current_health_producer_keeps_every_emitted_diagnostic(self):
        observed = healthy_snapshot()
        observed['services'].update(capacity='failed', updates='failed', reconcile='failed')
        observed['timers'].update(capacity='failed', reconcile='failed')
        observed['stale'] = {name: 1 for name in ('containers', 'images', 'networks', 'volumes', 'build_cache')}
        report = health.evaluate(observed, health.Thresholds())
        report['timestamp'] = NOW - 20
        docs = documents(); docs['health'] = report
        value = self.build(docs)
        projected = value['sources']['health']
        self.assertEqual(projected['availability'], 'available')
        expected = {item['id'] + '_service' if item['id'] in {'capacity', 'updates'} and 'state' in item else item['id']
                    for item in report['checks']}
        self.assertEqual({item['id'] for item in projected['data']['checks']}, expected)
        self.assertTrue({'capacity_service', 'updates_service', 'stale_build_cache', 'timer_capacity', 'reconcile'} <= expected)

    def test_source_loss_retains_original_last_success_without_zero_fallback(self):
        previous = self.build()
        value = self.build(errors={"controller": "source_timeout"}, previous=previous, now=NOW + 30)
        runtime = value["sources"]["controller"]
        self.assertEqual(runtime["availability"], "unavailable")
        self.assertIsNone(runtime["observed_at"])
        self.assertEqual(runtime["last_success_at"], NOW - 10)
        self.assertEqual(runtime["data"]["current"], 2)
        self.assertEqual(value["sources"]["health"]["availability"], "available")
        initial = self.build(errors={"controller": "source_unavailable"})
        self.assertNotIn("current", initial["sources"]["controller"]["data"])

    def test_wrong_identity_does_not_retain_another_controllers_data(self):
        docs = documents()
        docs["controller"]["controller"] = "different-ci-01"
        value = self.build(docs, previous=self.build())
        self.assertEqual(value["sources"]["controller"]["error"], "identity_mismatch")
        self.assertEqual(value["sources"]["controller"]["data"], {})

    def test_safe_missing_installation_is_absent_but_unsafe_is_unknown(self):
        absent = self.build(errors={"installation": "not_found"})
        self.assertEqual(absent["sources"]["installation"]["data"], {"record_state": "absent"})
        unsafe = self.build(errors={"installation": "source_unsafe"})
        self.assertEqual(unsafe["sources"]["installation"]["availability"], "unavailable")
        self.assertNotIn("record_state", unsafe["sources"]["installation"]["data"])

    def test_unversioned_legacy_installation_does_not_invent_supported_fields(self):
        docs = documents()
        del docs["installation"]["schema_version"]
        value = self.build(docs)
        self.assertEqual(value["sources"]["installation"]["availability"], "unsupported")
        self.assertEqual(value["sources"]["configuration"]["availability"], "unknown")
        self.assertEqual(value["sources"]["health"]["availability"], "available")

    def test_rendered_policy_requires_installed_identity_and_revision_binding(self):
        for field, bad in (("CI_FLEET_INSTANCE", "different-ci-01"), ("CI_FLEET_CONFIG_REF", "4" * 40),
                           ("CI_FLEET_ENGINE_REF", "5" * 40)):
            docs = documents()
            docs["rendered"][field] = bad
            value = self.build(docs)
            self.assertEqual(value["sources"]["configuration"]["error"], "identity_mismatch")
            self.assertEqual(value["sources"]["configuration"]["data"], {})

    def test_routing_labels_follow_schema_v3_without_a_private_count_limit(self):
        docs = documents()
        expected = [f"capability-{index}" for index in range(40)]
        docs["rendered"]["CI_FLEET_LABELS"] = ",".join(expected)
        source = self.build(docs)["sources"]["configuration"]
        self.assertEqual(source["availability"], "available")
        self.assertEqual(source["data"]["routing_labels"], expected)

    def test_configuration_capacity_matches_authoritative_policy_state(self):
        for state in ("active", "drained", "disabled"):
            for minimum, maximum in ((0, 0), (0, 3), (0, 4), (1, 4)):
                with self.subTest(state=state, minimum=minimum, maximum=maximum):
                    docs = documents()
                    docs["installation"]["controller_state"] = state
                    docs["rendered"]["CI_FLEET_CONTROLLER_STATE"] = state
                    docs["rendered"]["CI_FLEET_MIN_RUNNERS"] = str(minimum)
                    docs["rendered"]["CI_FLEET_MAX_RUNNERS"] = str(maximum)
                    value = self.build(docs)
                    valid = minimum == 0 and maximum == (4 if state == "active" else 0)
                    self.assertEqual(value["sources"]["configuration"]["availability"] == "available", valid)

    def test_complete_installation_and_rendered_capacity_are_required(self):
        for field in ("architecture", "scale_set_name", "status_reporting_configured", "status_reporting_required",
                      "docker_network_policy_configured"):
            with self.subTest(field=field):
                docs = documents()
                del docs["installation"][field]
                self.assertNotEqual(self.build(docs)["sources"]["installation"]["availability"], "available")
        for field, changed in (("CI_FLEET_CONTROLLER_STATE", "drained"),
                               ("CI_FLEET_CONFIGURED_MAX_RUNNERS", "5"), ("CI_FLEET_CAPACITY_BUDGET", "9")):
            for missing in (False, True):
                with self.subTest(field=field, missing=missing):
                    docs = documents()
                    if missing:
                        del docs["rendered"][field]
                    else:
                        docs["rendered"][field] = changed
                    self.assertNotEqual(self.build(docs)["sources"]["configuration"]["availability"], "available")

    def test_engine_and_routing_identity_reject_impossible_schema_v3_values(self):
        docs = documents()
        docs["installation"]["engine_ref"] = docs["rendered"]["CI_FLEET_ENGINE_REF"] = "0" * 40
        self.assertNotEqual(self.build(docs)["sources"]["installation"]["availability"], "available")
        for label in ("self-hosted", "example-ci-01-scale-set", "UPPERCASE", "x" * 64):
            docs = documents()
            docs["rendered"]["CI_FLEET_LABELS"] = label
            self.assertNotEqual(self.build(docs)["sources"]["configuration"]["availability"], "available")
        for field, changed in (("pool", "UPPERCASE"), ("scale_set_name", "unbound-scale-set")):
            docs = documents()
            docs["installation"][field] = changed
            self.assertNotEqual(self.build(docs)["sources"]["installation"]["availability"], "available")

    def test_health_summary_cannot_understate_approved_check_severity(self):
        for summary, severity in (("healthy", "warning"), ("healthy", "critical"), ("warning", "critical"),
                                  ("maintenance", "critical")):
            docs = documents()
            docs["health"]["status"] = summary
            docs["health"]["checks"][1]["status"] = severity
            self.assertNotEqual(self.build(docs)["sources"]["health"]["availability"], "available")

    def test_installation_requires_possible_schema_v3_capacity(self):
        for state, maximum, budget in (("active", 0, 8), ("active", 4, 0), ("active", 9, 8),
                                       ("drained", 9, 8), ("active", 4, None), ("disabled", 9, 8)):
            with self.subTest(state=state, maximum=maximum, budget=budget):
                docs = documents()
                docs["installation"].update(controller_state=state, configured_max_runners=maximum)
                if budget is None:
                    del docs["installation"]["capacity_budget"]
                else:
                    docs["installation"]["capacity_budget"] = budget
                source = self.build(docs)["sources"]["installation"]
                self.assertEqual(source["availability"] == "available", state == "disabled")

    def test_runner_resources_follow_schema_v3_types_and_minimums(self):
        for field, invalid in (("CI_FLEET_RUNNER_CPUS", "0.5"), ("CI_FLEET_RUNNER_CPUS", "1.5"),
                               ("CI_FLEET_RUNNER_MEMORY_MIB", "1"), ("CI_FLEET_RUNNER_MEMORY_MIB", "511")):
            with self.subTest(field=field, invalid=invalid):
                docs = documents()
                docs["rendered"][field] = invalid
                self.assertNotEqual(self.build(docs)["sources"]["configuration"]["availability"], "available")

    def test_health_and_convergence_bind_to_authoritative_installation(self):
        for desired in ("drained", "disabled", None):
            docs = documents()
            docs["health"]["desired_state"] = desired
            source = self.build(docs)["sources"]["health"]
            self.assertNotEqual(source["availability"], "available")
            self.assertEqual(source["data"], {})
        for field, changed in (("desired_commit", None), ("applied_commit", None),
                               ("desired_commit", "4" * 40), ("applied_commit", "4" * 40)):
            docs = documents()
            docs["reconciliation"][field] = changed
            source = self.build(docs)["sources"]["reconciliation"]
            self.assertNotEqual(source["availability"], "available")
            self.assertEqual(source["data"], {})

    def test_failed_health_read_discards_old_policy_evidence(self):
        docs = documents()
        docs["installation"]["controller_state"] = "drained"
        docs["rendered"]["CI_FLEET_MAX_RUNNERS"] = "0"
        value = self.build(docs, errors={"health": "source_timeout"}, previous=self.build())
        self.assertEqual(value["sources"]["health"]["error"], "revision_unverified")
        self.assertEqual(value["sources"]["health"]["data"], {})

    def test_retained_controller_counts_do_not_keep_a_stale_policy_comparison(self):
        previous = self.build()
        docs = documents()
        docs["installation"]["configured_max_runners"] = 5
        docs["rendered"]["CI_FLEET_MAX_RUNNERS"] = "5"
        value = self.build(docs, errors={"controller": "source_timeout"}, previous=previous)
        runtime = value["sources"]["controller"]
        self.assertEqual(runtime["last_success_at"], NOW - 10)
        self.assertEqual(runtime["data"]["maximum"], 4)
        self.assertNotIn("matches_reviewed_maximum", runtime["data"])

    def test_invalid_counts_times_and_policy_values_fail_independently(self):
        for field, bad in (("current", False), ("busy", 3), ("maximum", -1), ("generated_at", NOW + 1),
                           ("software_version", "arbitrary private value")):
            docs = documents()
            docs["controller"][field] = bad
            value = self.build(docs)
            self.assertNotEqual(value["sources"]["controller"]["availability"], "available")
            self.assertEqual(value["sources"]["controller"]["data"], {})
            self.assertEqual(value["sources"]["health"]["availability"], "available")
        for field, bad in (("CI_FLEET_RUNNER_CPUS", "NaN"), ("CI_FLEET_RUNNER_MEMORY_MIB", "0"),
                           ("CI_FLEET_MAX_RUNNERS", "99"), ("CI_FLEET_LABELS", "a,a")):
            docs = documents()
            docs["rendered"][field] = bad
            self.assertNotEqual(self.build(docs)["sources"]["configuration"]["availability"], "available")

    def test_runtime_policy_mismatch_is_observed_without_claiming_readiness(self):
        docs = documents()
        docs["controller"]["maximum"] = 5
        value = self.build(docs)
        runtime = value["sources"]["controller"]
        self.assertEqual(runtime["availability"], "available")
        self.assertFalse(runtime["data"]["matches_reviewed_maximum"])
        self.assertEqual(value["sources"]["enrollment_listener"]["availability"], "unsupported")

    def test_old_reconciliation_cannot_bind_to_a_new_installation(self):
        docs = documents()
        docs["reconciliation"]["checked_at"] = NOW - 200
        value = self.build(docs)
        self.assertEqual(value["sources"]["reconciliation"]["error"], "revision_unverified")
        self.assertEqual(value["sources"]["reconciliation"]["data"], {})

    def test_failed_dependent_reads_discard_evidence_from_another_installation(self):
        previous = self.build()
        for field, changed in (("config_ref", "4" * 40), ("engine_ref", "5" * 40),
                               ("installed_at", dt.datetime.fromtimestamp(NOW - 5, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))):
            for error in ("not_found", "source_timeout", "source_invalid"):
                with self.subTest(field=field, error=error):
                    docs = documents()
                    docs["installation"][field] = changed
                    value = self.build(docs, errors={"rendered": error, "reconciliation": error}, previous=previous)
                    for name in ("configuration", "reconciliation"):
                        source = value["sources"][name]
                        self.assertEqual(source["error"], "revision_unverified")
                        self.assertEqual(source["data"], {})
                        self.assertIsNone(source["last_success_at"])

    def test_failed_dependent_reads_retain_only_verified_installation_evidence(self):
        previous = self.build()
        value = self.build(errors={"rendered": "source_timeout", "reconciliation": "not_found"}, previous=previous)
        for name in ("configuration", "reconciliation"):
            self.assertEqual(value["sources"][name]["data"], previous["sources"][name]["data"])
            self.assertEqual(value["sources"][name]["last_success_at"], previous["sources"][name]["last_success_at"])
        for installation_error in ("not_found", "source_timeout", "source_unsafe"):
            value = self.build(errors={"installation": installation_error, "rendered": "not_found", "reconciliation": "not_found"}, previous=previous)
            for name in ("configuration", "reconciliation"):
                self.assertEqual(value["sources"][name]["error"], "revision_unverified")
                self.assertEqual(value["sources"][name]["data"], {})

    def test_future_installation_date_discards_previous_evidence(self):
        docs = documents()
        docs["installation"]["installed_at"] = dt.datetime.fromtimestamp(NOW + 1, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        value = self.build(docs, previous=self.build())
        installation = value["sources"]["installation"]
        self.assertEqual(installation["availability"], "unknown")
        self.assertEqual(installation["error"], "clock_uncertain")
        self.assertEqual(installation["data"], {})
        self.assertIsNone(installation["last_success_at"])

    def test_snapshot_schema_rejects_unknown_fields_versions_and_hostile_types(self):
        paths = (("schema_version",), ("clock_state",), ("sources", "health", "availability"),
                 ("sources", "health", "data", "checks"))
        for path in paths:
            value = self.build()
            target = value
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = {"private": "value"}
            self.assert_snapshot_error(lambda: snapshot.validate_snapshot(value, controller=CONTROLLER))
        value = self.build()
        value["sources"]["controller"]["data"]["private_key"] = "do-not-export"
        self.assert_snapshot_error(lambda: snapshot.validate_snapshot(value, controller=CONTROLLER))
        value = self.build()
        value["schema_version"] = 2
        self.assert_snapshot_error(lambda: snapshot.validate_snapshot(value, controller=CONTROLLER), "unsupported_schema")

    def test_readback_rejects_empty_or_impossible_available_evidence(self):
        for name in ("installation", "configuration", "controller", "health", "reconciliation"):
            value = self.build()
            value["sources"][name]["data"] = {}
            self.assert_snapshot_error(lambda: snapshot.validate_snapshot(value, controller=CONTROLLER))
        for mutate in (
            lambda v: v["sources"]["controller"]["data"].update(current=0, busy=3, maximum=1),
            lambda v: v["sources"]["controller"]["data"].update(matches_reviewed_maximum=False),
            lambda v: v["sources"]["configuration"]["data"].update(maximum_runners=99),
            lambda v: v["sources"]["configuration"]["data"].update(maximum_runners=3),
            lambda v: v["sources"]["configuration"]["data"].update(minimum_runners=1),
            lambda v: v["sources"]["configuration"]["data"].update(applied_commit="4" * 40),
            lambda v: v["sources"]["installation"]["data"].update(installed_at=NOW + 1),
            lambda v: v["sources"]["installation"]["data"].update(reviewed_maximum=0),
            lambda v: v["sources"]["installation"]["data"].update(capacity_budget=0),
            lambda v: v["sources"]["installation"]["data"].update(capacity_budget=3),
            lambda v: v["sources"]["installation"]["data"].pop("capacity_budget"),
            lambda v: v["sources"]["installation"]["data"].pop("architecture"),
            lambda v: v["sources"]["installation"]["data"].update(engine_ref="0" * 40),
            lambda v: v["sources"]["installation"]["data"].update(scale_set_name="unbound-scale-set"),
            lambda v: v["sources"]["installation"]["data"].update(reporting_configured=False, reporting_required=True),
            lambda v: v["sources"]["configuration"]["data"].update(runner_cpu_cores=0.5),
            lambda v: v["sources"]["configuration"]["data"].update(routing_labels=["self-hosted"]),
            lambda v: v["sources"]["configuration"]["data"].update(routing_labels=["example-ci-01-scale-set"]),
            lambda v: v["sources"]["configuration"]["data"].update(runner_memory_mib=511),
            lambda v: v["sources"]["health"]["data"].update(desired_state="drained"),
            lambda v: v["sources"]["health"]["data"]["checks"][1].update(status="critical"),
            lambda v: v["sources"]["reconciliation"]["data"].update(last_success_at=NOW + 1),
            lambda v: v["sources"]["reconciliation"]["data"].update(desired_commit="4" * 40),
            lambda v: v["sources"]["reconciliation"]["data"].pop("applied_commit"),
        ):
            value = self.build()
            mutate(value)
            self.assert_snapshot_error(lambda: snapshot.validate_snapshot(value, controller=CONTROLLER))

    def test_retained_data_requires_a_supported_source_version(self):
        for name in ("controller", "configuration", "health", "reconciliation"):
            value = self.build(errors={"rendered" if name == "configuration" else name: "source_timeout"}, previous=self.build())
            value["sources"][name]["source_version"] = None
            self.assert_snapshot_error(lambda: snapshot.validate_snapshot(value, controller=CONTROLLER))

    def test_placeholder_sources_cannot_claim_available_proof(self):
        for name in snapshot.UNSUPPORTED_SOURCES:
            value = self.build()
            value["sources"][name] = snapshot.evidence({}, observed_at=NOW)
            self.assert_snapshot_error(lambda: snapshot.validate_snapshot(value, controller=CONTROLLER))

    def test_malformed_storage_policy_is_not_reported_as_absent(self):
        docs = documents()
        docs["rendered"]["CI_FLEET_DOCKER_STORAGE_RETENTION"] = "invalid"
        self.assertEqual(self.build(docs)["sources"]["configuration"]["error"], "source_invalid")


class SnapshotReadbackTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.base = self.root / "var/lib/ci-fleet-observer"
        self.exports = self.base / "exports"
        self.exports.mkdir(parents=True)
        self.gid = os.getegid()
        self.base.chmod(0o750)
        self.exports.chmod(0o750)
        self.addCleanup(patch.stopall)
        patch.object(store, "OWNER_UID", os.geteuid()).start()
        patch.object(store, "INITIAL_GID", self.gid).start()
        self.reader = snapshot.SnapshotReader(self.base, observer_gid=self.gid, controller=CONTROLLER, backend_bearer=BEARER)

    def test_authentication_precedes_all_export_access_and_ignores_identity_headers(self):
        for headers in ({}, {"Remote-User": "operator"}, {"Authorization": "Bearer wrong"},
                        {"Authorization": "Bearer " + BEARER, "authorization": "Bearer " + BEARER},
                        {"Authorization": "Bearer " + "\u00e9" * 43}, None):
            with patch.object(snapshot, "read_endpoint") as read:
                with self.assertRaises(snapshot.SnapshotError) as caught:
                    self.reader.read(headers, now=NOW)
                self.assertEqual(caught.exception.code, "authentication_failed")
                read.assert_not_called()

    def test_publisher_holds_writer_lock_during_collection(self):
        def collect(*args, **kwargs):
            with self.assertRaises(store.StoreError) as error:
                with store.publication_lock(self.base, self.gid):
                    self.fail('overlapping publisher acquired the writer lock')
            self.assertEqual(error.exception.code, 'busy')
            return documents(), {}
        with patch.object(snapshot, 'collect_documents', side_effect=collect):
            snapshot.publish(CONTROLLER, observer_gid=self.gid, root=self.root, now=NOW)

    def test_publisher_preserves_unsupported_schema_and_wrong_identity_exports(self):
        for field, value, expected in (("schema_version", 2, "unsupported_schema"),
                                       ("source_version", 2, "unsupported_schema"),
                                       ("controller", "different-ci-01", "identity_mismatch")):
            with self.subTest(field=field):
                incumbent = snapshot.build_snapshot(CONTROLLER, documents(), {}, now=NOW + 10)
                if field == "source_version":
                    incumbent["sources"]["health"][field] = value
                else:
                    incumbent[field] = value
                store.publish_endpoint(self.base, incumbent, self.gid)
                before = self.exports.joinpath("endpoint.json").read_bytes()
                with patch.object(snapshot, "collect_documents", return_value=(documents(), {})) as collect:
                    with self.assertRaises(snapshot.SnapshotError) as error:
                        snapshot.publish(CONTROLLER, observer_gid=self.gid, root=self.root, now=NOW)
                    self.assertEqual(error.exception.code, expected)
                    collect.assert_not_called()
                self.assertEqual(self.exports.joinpath("endpoint.json").read_bytes(), before)

    def test_publisher_recovers_malformed_incumbent(self):
        path = self.exports / "endpoint.json"
        path.write_text("{")
        path.chmod(0o640)
        with patch.object(snapshot, "collect_documents", return_value=(documents(), {})):
            value = snapshot.publish(CONTROLLER, observer_gid=self.gid, root=self.root, now=NOW)
        self.assertEqual(store.read_endpoint(self.base, self.gid), value)

    def test_publish_and_authenticated_read_keep_sources_out_of_consumer_access(self):
        with patch.object(snapshot, "collect_documents", return_value=(documents(), {})):
            value = snapshot.publish(CONTROLLER, observer_gid=self.gid, root=self.root, now=NOW)
        with patch.object(snapshot, "collect_documents", side_effect=AssertionError("consumer must not collect")):
            read = self.reader.read({"Authorization": "Bearer " + BEARER, "X-Forwarded-User": "forged"}, now=NOW)
        self.assertEqual(read["sources"]["controller"]["data"]["busy"], 1)
        self.assertEqual(read["sources"]["controller"]["freshness"], "current")
        self.assertEqual(read["publisher_freshness"], "current")
        self.assertEqual(value["sources"]["controller"]["observed_at"], NOW - 10)
        self.assertNotIn(BEARER, self.exports.joinpath("endpoint.json").read_text())

    def test_freshness_ages_after_publisher_controller_and_docker_outage(self):
        store.publish_endpoint(self.base, snapshot.build_snapshot(CONTROLLER, documents(), {}, now=NOW), self.gid)
        read = self.reader.read({"Authorization": "Bearer " + BEARER}, now=NOW + 130)
        self.assertEqual(read["sources"]["controller"]["freshness"], "stale")
        self.assertEqual(read["sources"]["health"]["freshness"], "current")
        self.assertEqual(read["publisher_freshness"], "stale")
        self.assertEqual(read["sources"]["installation"]["freshness"], "recorded")
        read = self.reader.read({"Authorization": "Bearer " + BEARER}, now=NOW + 901)
        self.assertEqual(read["sources"]["health"]["freshness"], "stale")

    def test_failed_source_is_not_current_even_with_a_fresh_publication(self):
        value = snapshot.build_snapshot(CONTROLLER, documents(), {"controller": "source_timeout"},
                                        now=NOW + 10, previous=snapshot.build_snapshot(CONTROLLER, documents(), {}, now=NOW))
        store.publish_endpoint(self.base, value, self.gid)
        read = self.reader.read({"Authorization": "Bearer " + BEARER}, now=NOW + 10)
        self.assertEqual(read["sources"]["controller"]["freshness"], "unknown")
        self.assertEqual(read["sources"]["controller"]["last_success_at"], NOW - 10)
        self.assertEqual(read["publisher_freshness"], "current")

    def test_clock_rollback_and_unsynchronized_clock_never_refresh_evidence(self):
        value = snapshot.build_snapshot(CONTROLLER, documents(), {}, now=NOW)
        store.publish_endpoint(self.base, value, self.gid)
        read = self.reader.read({"Authorization": "Bearer " + BEARER}, now=NOW - 1)
        self.assertEqual(read["clock_state"], "uncertain")
        self.assertEqual(read["sources"]["controller"]["freshness"], "unknown")
        docs = documents()
        docs["health"]["checks"][0]["status"] = "warning"
        docs["health"]["status"] = "warning"
        store.publish_endpoint(self.base, snapshot.build_snapshot(CONTROLLER, docs, {}, now=NOW), self.gid)
        read = self.reader.read({"Authorization": "Bearer " + BEARER}, now=NOW)
        self.assertEqual(read["publisher_freshness"], "unknown")

    def test_reader_rejects_clock_claims_that_contradict_health_evidence(self):
        for status, claim in (("warning", "synchronized"), ("critical", "unknown"), ("ok", "uncertain")):
            with self.subTest(status=status, claim=claim):
                docs = documents()
                docs["health"]["checks"][0]["status"] = status
                docs["health"]["status"] = {"ok": "healthy", "warning": "warning", "critical": "unhealthy"}[status]
                value = snapshot.build_snapshot(CONTROLLER, docs, {}, now=NOW)
                value["clock_state"] = claim
                store.publish_endpoint(self.base, value, self.gid)
                with self.assertRaises(snapshot.SnapshotError):
                    self.reader.read({"Authorization": "Bearer " + BEARER}, now=NOW)

    def test_reader_rejects_reconciliation_without_installation_time_binding(self):
        for changed in ("old_check", "retained_old_check", "absent", "unavailable"):
            with self.subTest(changed=changed):
                value = snapshot.build_snapshot(CONTROLLER, documents(), {}, now=NOW)
                if changed in {"old_check", "retained_old_check"}:
                    source = value["sources"]["reconciliation"]
                    source["observed_at"] = source["last_success_at"] = NOW - 200
                    source["data"]["last_success_at"] = NOW - 200
                    if changed == "retained_old_check":
                        source["availability"] = "unavailable"
                        source["observed_at"] = None
                        source["error"] = "source_timeout"
                elif changed == "absent":
                    value["sources"]["installation"] = snapshot.evidence({"record_state": "absent"}, observed_at=NOW)
                    value["sources"]["configuration"] = snapshot.failure("revision_unverified")
                    del value["sources"]["controller"]["data"]["matches_reviewed_maximum"]
                else:
                    value["sources"]["installation"] = snapshot.failure("source_timeout", value["sources"]["installation"])
                    value["sources"]["configuration"] = snapshot.failure("revision_unverified")
                    del value["sources"]["controller"]["data"]["matches_reviewed_maximum"]
                store.publish_endpoint(self.base, value, self.gid)
                with self.assertRaises(snapshot.SnapshotError):
                    self.reader.read({"Authorization": "Bearer " + BEARER}, now=NOW)

    def test_reader_rejects_retained_configuration_from_another_installation(self):
        for field, changed in (("applied_commit", "4" * 40), ("engine_ref", "5" * 40), ("maximum_runners", 3),
                               ("minimum_runners", 1),
                               ("last_success_at", NOW - 200)):
            with self.subTest(field=field):
                value = snapshot.build_snapshot(CONTROLLER, documents(), {}, now=NOW)
                source = value["sources"]["configuration"]
                source["availability"] = "unavailable"
                source["observed_at"] = None
                source["error"] = "source_timeout"
                if field == "last_success_at":
                    source[field] = changed
                else:
                    source["data"][field] = changed
                store.publish_endpoint(self.base, value, self.gid)
                with self.assertRaises(snapshot.SnapshotError):
                    self.reader.read({"Authorization": "Bearer " + BEARER}, now=NOW)

    def test_failed_health_read_preserves_recent_clock_warning_until_new_evidence(self):
        docs = documents()
        docs["health"]["checks"][0]["status"] = "warning"
        docs["health"]["status"] = "warning"
        previous = snapshot.build_snapshot(CONTROLLER, docs, {}, now=NOW)
        value = snapshot.build_snapshot(CONTROLLER, docs, {"health": "source_unavailable"},
                                        now=NOW + 10, previous=previous)
        store.publish_endpoint(self.base, value, self.gid)
        read = self.reader.read({"Authorization": "Bearer " + BEARER}, now=NOW + 10)
        self.assertEqual(read["clock_state"], "uncertain")
        self.assertEqual(read["publisher_freshness"], "unknown")
        self.assertEqual(read["sources"]["controller"]["freshness"], "unknown")
        self.assertEqual(read["sources"]["health"]["availability"], "unavailable")
        self.assertEqual(read["sources"]["health"]["last_success_at"], previous["sources"]["health"]["observed_at"])
        docs["health"]["checks"][0]["status"] = "ok"
        docs["health"]["status"] = "healthy"
        docs["health"]["timestamp"] = NOW + 20
        recovered = snapshot.build_snapshot(CONTROLLER, docs, {}, now=NOW + 20, previous=value)
        store.publish_endpoint(self.base, recovered, self.gid)
        self.assertEqual(self.reader.read({"Authorization": "Bearer " + BEARER}, now=NOW + 20)["publisher_freshness"], "current")

    def test_future_source_times_make_all_freshness_unknown(self):
        future_installation = dt.datetime.fromtimestamp(NOW + 1, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for name, field, future in (("health", "timestamp", NOW + 1),
                                    ("controller", "generated_at", NOW + 1),
                                    ("reconciliation", "checked_at", NOW + 1),
                                    ("installation", "installed_at", future_installation)):
            with self.subTest(source=name):
                docs = documents()
                docs[name][field] = future
                value = snapshot.build_snapshot(CONTROLLER, docs, {}, now=NOW)
                store.publish_endpoint(self.base, value, self.gid)
                read = self.reader.read({"Authorization": "Bearer " + BEARER}, now=NOW)
                self.assertEqual(read["clock_state"], "uncertain")
                self.assertEqual(read["publisher_freshness"], "unknown")
                self.assertTrue(all(source["freshness"] == "unknown" for source in read["sources"].values()))
                self.assertEqual(read["sources"][name]["error"], "clock_uncertain")
                self.assertEqual(read["sources"][name]["data"], {})

    @unittest.skipUnless(os.geteuid() == 0, "requires isolated root-owned permission fixture")
    def test_unprivileged_observer_reads_export_but_cannot_read_private_sources(self):
        self.root.chmod(0o755)
        observer_gid = 65532
        for directory in (self.base, self.exports):
            os.chown(directory, 0, observer_gid)
        private = self.root / "private"
        private.mkdir(mode=0o700)
        credential = private / "credential"
        credential.write_text("nonfunctional-test-only")
        credential.chmod(0o600)
        store.publish_endpoint(self.base, snapshot.build_snapshot(CONTROLLER, documents(), {}, now=NOW), observer_gid)
        child = os.fork()
        if child == 0:
            try:
                os.setgroups([observer_gid])
                os.setgid(observer_gid)
                os.setuid(65534)
                reader = snapshot.SnapshotReader(self.base, observer_gid=observer_gid, controller=CONTROLLER, backend_bearer=BEARER)
                read = reader.read({"Authorization": "Bearer " + BEARER}, now=NOW)
                assert read["sources"]["controller"]["data"]["busy"] == 1
                try:
                    credential.read_bytes()
                except PermissionError:
                    pass
                else:
                    raise AssertionError("private credential readable")
            except BaseException:
                os._exit(1)
            os._exit(0)
        _, status = os.waitpid(child, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 0)


if __name__ == "__main__":
    unittest.main()
