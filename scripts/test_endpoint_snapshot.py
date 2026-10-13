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
                         "lifecycle": "disposable", "configured_max_runners": 4, "capacity_budget": 8,
                         "architecture": "amd64", "scale_set_name": "example-scale-set",
                         "status_reporting_configured": True, "status_reporting_required": False},
        "rendered": {"CI_FLEET_INSTANCE": CONTROLLER, "CI_FLEET_CONFIG_REF": "1" * 40,
                     "CI_FLEET_ENGINE_REF": "2" * 40, "CI_FLEET_DESIRED_STATE_SCHEMA": "3",
                     "CI_FLEET_LABELS": "docker-ci", "CI_FLEET_RUNNER_CPUS": "2",
                     "CI_FLEET_RUNNER_MEMORY_MIB": "4096", "CI_FLEET_MIN_RUNNERS": "0",
                     "CI_FLEET_MAX_RUNNERS": "4"},
        "controller": {"controller": CONTROLLER, "software_version": "3" * 40,
                       "current": 2, "busy": 1, "maximum": 4, "generated_at": NOW - 10},
        "health": {"schema_version": 1, "controller": CONTROLLER, "status": "healthy", "timestamp": NOW - 20,
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

    def test_invalid_counts_times_and_policy_values_fail_independently(self):
        for field, bad in (("current", False), ("busy", 3), ("maximum", -1), ("generated_at", NOW + 1),
                           ("software_version", "arbitrary-private-value")):
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
            lambda v: v["sources"]["configuration"]["data"].update(applied_commit="4" * 40),
            lambda v: v["sources"]["installation"]["data"].update(installed_at=NOW + 1),
            lambda v: v["sources"]["reconciliation"]["data"].update(last_success_at=NOW + 1),
        ):
            value = self.build()
            mutate(value)
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
        store.publish_endpoint(self.base, snapshot.build_snapshot(CONTROLLER, docs, {}, now=NOW), self.gid)
        read = self.reader.read({"Authorization": "Bearer " + BEARER}, now=NOW)
        self.assertEqual(read["publisher_freshness"], "unknown")

    def test_reader_rejects_clock_claims_that_contradict_health_evidence(self):
        for status, claim in (("warning", "synchronized"), ("critical", "unknown"), ("ok", "uncertain")):
            with self.subTest(status=status, claim=claim):
                docs = documents()
                docs["health"]["checks"][0]["status"] = status
                value = snapshot.build_snapshot(CONTROLLER, docs, {}, now=NOW)
                value["clock_state"] = claim
                store.publish_endpoint(self.base, value, self.gid)
                with self.assertRaises(snapshot.SnapshotError):
                    self.reader.read({"Authorization": "Bearer " + BEARER}, now=NOW)

    def test_failed_health_read_preserves_recent_clock_warning_until_new_evidence(self):
        docs = documents()
        docs["health"]["checks"][0]["status"] = "warning"
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
