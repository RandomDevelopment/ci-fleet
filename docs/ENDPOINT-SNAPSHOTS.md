# Worker snapshot foundation

This implements the local snapshot foundation for [#118](https://github.com/RandomDevelopment/ci-fleet/issues/118) under the accepted [endpoint UI design](ENDPOINT-UI-DESIGN.md). It does not install an observer, open port 80, change desired-state policy, or declare an engine capability. Browser delivery remains in #121 and packaging in #122. Existing health, bootstrap, reporting, controller lifecycle, and configuration approval continue unchanged.

## Producer and consumer interfaces

`scripts/endpoint_snapshot.py` provides `build_snapshot`, `validate_snapshot`, `publish`, and `SnapshotReader`. The producer uses the protected source adapter in `endpoint_snapshot_sources.py` and filesystem rules in `endpoint_snapshot_store.py`. All use Python's standard library.

The privileged publication entrypoint requires an already prepared export tree and an existing observer group:

```bash
sudo python3 scripts/endpoint_snapshot.py \
  --controller example-ci-01 \
  --observer-group ci-fleet-observer
```

This is a foundation interface, not an enrollment or installation procedure. Preparation, approved credentials, role admission, and service activation belong to #122. The command does not create directories, groups, units, credentials, or network rules. It prints only a controlled success/failure code and returns nonzero when publication fails. A future caller must isolate that failure from CI and reconciliation.

`SnapshotReader` accepts a fixed export base, observer GID, expected controller identity, and an already provisioned backend bearer. Its `read(headers)` method authenticates before any filesystem access and ignores forwarded identity headers. The caller must supply the bearer through the separately approved credential mechanism; it must never derive configuration from browser input or reuse controller credentials. This PR adds no credential loader or HTTP listener. The transport must enforce the ADR's request and response limits and `Cache-Control: no-store`.

## Snapshot version 1

The strict local envelope has `schema_version`, `role`, `controller`, `published_at`, `clock_state`, and `sources`. The role is `worker`. Each fixed source has `source_version`, `availability`, `observed_at`, `last_success_at`, `error`, and an allowlisted `data` object. Unknown versions or fields fail validation. Adapter version 1 recognizes the current unversioned controller/reconciliation input shapes without changing those producers.

Availability is `available`, `not_configured`, `unsupported`, `unavailable`, or `unknown`. Missing measurements remain absent. A failed source may retain its original last-success data and timestamp, but cannot claim a current observation. Wrong identity, unverified revision binding, or future timestamps discard those values. Sources fail independently.

The authenticated read response adds derived `freshness` to each source and `publisher_freshness` to the envelope. It does not change stored measurements or observation times. Controller and publisher evidence becomes stale after 120 seconds; health and reconciliation after 900 seconds. Installation/configuration records are event evidence and use `recorded`, not a recent live check. Clock rollback or known clock uncertainty makes freshness unknown. The producer refuses a publication time earlier than the incumbent export and preserves that export. Its reader reports unknown freshness during the rollback; publication can resume once the clock reaches the retained publication time. A failed health read retains a recent last-success clock warning within that 900-second window; a new successful clock check can clear it. The read response is not a publishable snapshot.

| Source | Approved data and provenance |
| --- | --- |
| Installation | Fixed root-owned mode-0600 `install-state.json`, schema 1 and matching controller. Publishes installation record presence, schema-v3 lifecycle (`experimental`, `stable`, or `retiring`), policy state, installed time, applied configuration commit, recorded engine policy, architecture, pool, reviewed maximum, capacity budget, scale-set name, and configured reporting/network-policy flags. Its installed time remains the source time. |
| Configuration | Fixed protected rendered `ci-fleet.env`, parsed without a shell. Requires rendered schema-v3 and matching installation controller, configuration commit, and engine revision. Exports routing labels, runner CPU/memory limits, policy minimum/maximum, and storage-retention configured state. Addresses, credential references, repository URLs, environment contents, proxy values and expected image IDs are excluded. |
| Controller | Bounded privileged read of the existing container's `/run/ci-fleet/status.json`. Validates identity, original generated time, actual version and integer counts. Compares its declared maximum with recorded policy without treating that comparison as readiness. Uses the canonical local container and Unix Docker socket; inherited Docker endpoints/contexts are not used. |
| Health | Fixed root-owned mode-0644 `health/latest.json`, matching controller and schema 1. Copies reported status and approved check IDs/severities at their original timestamp. Admission `capacity` and the capacity sampler service become distinct `capacity` and `capacity_service` diagnostics. Package-update state and its service become `updates` and `updates_service`; dynamic container, build-cache, capacity timer and reconciliation service checks remain visible. Omits detail text and numeric helper fallbacks. These are reported check results, not independent probe or timer-activation attestations. |
| Reconciliation | Protected `reconcile/state.json`. Copies controlled status, desired/applied commits, check time and last-success time. Requires a matching installation context and a check at or after its recorded installation. Does not copy free-text messages or infer transaction/rollback proof. |

Readiness, GitHub authentication, scale-set matching, identity presence, reporting success and owner confirmation each have separate unsupported sources. Filesystem runtime/manager selection, manager staging, independently desired engine revision, measured image verification, capacity telemetry, job history and transaction proof also remain unsupported. The installation engine field is recorded policy; the controller version is actual running evidence. Neither proves image contents. The ADR's boot/session evidence is also absent from this foundation envelope. These adapters and envelope coverage remain follow-up work under #118 and the linked issues. #118 stays open until its remaining evidence coverage and integration acceptance are complete.

Health helpers can substitute zero counts or a configured engine revision after failed reads. This producer does not consume those fallbacks. It does not rerun health/capacity probes, make GitHub requests, inspect credentials, read logs, or increase outbound reporting. The Docker status read has a 64 KiB output cap and an absolute five-second deadline; other protected reads have their own bounded reads.

## Protected publication

The base `/var/lib/ci-fleet-observer` and its `exports` child must already be root-owned with the dedicated observer GID and mode `0750`. The existing private `/var/lib/ci-fleet` and `/etc/ci-fleet` trees keep their permissions.

The store pins directory descriptors, rejects symlink ancestry and unsafe writable parents, and validates regular files, ownership, exact mode, group, one hard link, size, and JSON before accepting data. Private export directories and files reject access/default ACLs, including inherited permissions beyond the observer group. Root-owned sticky `/tmp` is allowed for isolated development fixtures; it does not allow a writable export base. JSON has a 32-level nesting limit and rejects duplicate keys, nonfinite numbers and invalid UTF-8.

The export directory permits only `endpoint.json`, `jobs.json`, and their fixed `.tmp` counterparts. Bounds remain 64 KiB per endpoint file, 4 MiB per job file, and 16 MiB overall. A nonblocking Linux directory-descriptor lock covers incumbent readback, source collection, validation and replacement without another file. A competing producer fails with `busy` before collecting sources. Recovery removes only validated temporary files, including legitimate root-owned mode-0600 files interrupted before group assignment. It never serves or promotes temporary contents.

The producer exclusively creates `endpoint.json.tmp` at mode `0600`, assigns its observer GID before setting mode `0640`, flushes the complete bounded payload, atomically replaces the final file, and flushes the directory. Failure before replacement preserves the incumbent export. Reads authenticate first, close descriptors after bounded reads, and cannot access private source stores through the consumer API.

## Verification

```bash
python3 scripts/test_endpoint_snapshot.py
python3 scripts/test_endpoint_snapshot_sources.py
python3 scripts/test_endpoint_snapshot_store.py
```

The focused tests cover source identity, timestamp provenance, missing/invalid evidence, strict redaction, unknown/stale state, authentication before file access, protected permissions and paths, interrupted publication, and bounded subprocess/file reads. Repository validation also runs them. Host installation, socket activation, approved credential provisioning, real Docker-outage operation and role acceptance remain separately gated.
