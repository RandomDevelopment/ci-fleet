# Worker snapshots

This is the read-only worker snapshot contract for [#118](https://github.com/RandomDevelopment/ci-fleet/issues/118) under the accepted [endpoint UI design](ENDPOINT-UI-DESIGN.md). The repository provides one privileged publication command and an authenticated file reader used by the [worker views](WORKER-VIEWS.md). Packaging, service activation, and live-host acceptance remain in #122. Bootstrap still uses its temporary authenticated callback. Reconciliation captures the desired engine only after existing validation and the opt-in independent configuration-approval check.

## Producer and consumer interfaces

`scripts/endpoint_snapshot.py` provides `build_snapshot`, `validate_snapshot`, `publish`, and `SnapshotReader`. The producer uses the protected source adapter in `endpoint_snapshot_sources.py` and filesystem rules in `endpoint_snapshot_store.py`. All use Python's standard library.

The privileged publication entrypoint requires an already prepared export tree and an existing observer group:

```bash
sudo python3 scripts/endpoint_snapshot.py \
  --controller example-ci-01 \
  --observer-group ci-fleet-observer
```

This is a foundation interface, not an enrollment or installation procedure. Preparation, approved credentials, role admission, and service activation belong to #122. The command does not create directories, groups, units, credentials, or network rules. It prints only a controlled success/failure code and returns nonzero when publication fails. A future caller must isolate that failure from CI and reconciliation.

`SnapshotReader` accepts a fixed export base, observer GID, expected controller identity, and an already provisioned backend bearer. Its `read(headers)` method authenticates before any filesystem access and ignores forwarded identity headers. The caller must supply the bearer through the separately approved credential mechanism. The worker HTTP application accepts a prepared loopback listener and applies the ADR's request and response limits and `Cache-Control: no-store`.

## Snapshot version 1

The strict local envelope has `schema_version`, `role`, `controller`, `published_at`, `clock_state`, and `sources`. The role is `worker`. Each fixed source has `source_version`, `availability`, `observed_at`, `last_success_at`, `error`, and an allowlisted `data` object. Unknown versions or fields fail validation. The reader accepts the original v1 foundation source set and supplies unsupported boot and session entries on readback. Update the reader before publishing the expanded v1 source set. Older controller, health, and reconciliation producers may omit the new proof fields.

Availability is `available`, `not_configured`, `unsupported`, `unavailable`, or `unknown`. Missing measurements remain absent. Foundation sources can retain last-success data with its original timestamp after a failed read. The added adapters discard their data when the observation or its binding fails. Sources fail independently.

Failed configuration and reconciliation reads retain evidence only when both snapshots have the same verified installation record and the retained observation is at or after that installation. A changed or unavailable installation discards dependent evidence as `revision_unverified`, even when the read fails before projection. Publication preserves incumbents with unsupported envelope/source versions or a different identity and fails before collecting sources. Only missing or malformed incumbents permit first-publication recovery.

Readback also rejects reconciliation evidence without a verified installation or with an observation before its installation time. Available and retained configuration must match the installation's revisions, capacity limit and installation timestamp. Controller software versions follow the existing status-report token contract, including the direct Compose build's `dev` fallback. That version remains running evidence, separate from the installation's pinned engine revision.

Configuration uses schema-v3 routing-label syntax without a separate count limit. The existing export byte bound still applies. Minimum runners must be zero; effective maximum must equal the reviewed maximum while active and zero while drained or disabled. A failed controller read retains original counts/version/time but drops its derived policy comparison. Readback validates any comparison that remains against current verified policy.

Installation records require a positive reviewed maximum and capacity budget. Active and drained maxima cannot exceed the budget; disabled records preserve their reviewed future maximum. Runner resources require integer CPU cores of at least one and memory of at least 512 MiB. Retained data always requires source version 1. Health includes its reported desired state and, when installation is verified, must match its policy state and follow its installation time. Converged reconciliation requires both commits to equal the installed configuration. Projection and readback enforce these rules; failed health reads discard incompatible retained policy evidence.

The installation projection requires every allowlisted field guaranteed by its producer, including architecture, scale-set identity and reporting/network-policy flags. Pool and scale-set names use schema-v3 slug syntax, and the scale set must include the controller identity. Reporting cannot be required when unconfigured. Revisions cannot be all zeroes. Configuration verifies the rendered controller state, scale-set identity, configured maximum and budget against installation; routing excludes `self-hosted` and the local scale-set name. A health summary cannot understate the severity of its retained checks or contradict its maintenance policy. Projection and readback share data validation.

Rendered numeric fields must use canonical unsigned decimal strings within the signed 64-bit runtime range. Health requires the current producer's unconditional baseline probe, stale-resource, maintenance, package, reboot, clock and backup IDs. Conditional network, proxy, sampler, reconciliation and operating-system service checks remain optional. Failed-source envelopes must match the documented error and retention rules; identity, clock and revision failures retain no data, version or successful timestamp. Unsupported placeholders cannot claim historical evidence, and retained observations require complete versioned data.

The authenticated read response adds derived `freshness` to each source and `publisher_freshness` to the envelope. It preserves measurement times. Controller, listener poll, identity presence, filesystem selection, image verification, capacity and publisher evidence become stale after 120 seconds. Health, reporting delivery, reconciliation, desired engine and transaction evidence become stale after 900 seconds. Staging evidence depends on both the current filesystem selection and the desired-engine observation. Boot, installation, configuration and controller startup checks are recorded events. Clock rollback or known clock uncertainty makes freshness unknown. The producer preserves the incumbent export if the next publication time would move backward. It can resume once the clock reaches the retained publication time. A failed health read retains a recent last-success clock warning within the 900-second window. Read responses contain derived fields that the publisher rejects.

| Source | Approved data and provenance |
| --- | --- |
| Installation | Fixed root-owned mode-0600 `install-state.json`, schema 1 and matching controller. Publishes installation record presence, schema-v3 lifecycle (`experimental`, `stable`, or `retiring`), policy state, installed time, applied configuration commit, recorded engine policy, architecture, pool, reviewed maximum, capacity budget, scale-set name, and configured reporting/network-policy flags. Its installed time remains the source time. |
| Configuration | Fixed protected rendered `ci-fleet.env`, parsed without a shell. Requires rendered schema-v3 and matching installation controller, configuration commit, and engine revision. Exports routing labels, runner CPU/memory limits, policy minimum/maximum, and storage-retention configured state. Addresses, credential references, repository URLs, environment contents, proxy values and expected image IDs are excluded. |
| Controller | Bounded privileged read of the existing container's `/run/ci-fleet/status.json`. Validates identity, original generated time, actual version and integer counts. Compares its declared maximum with recorded policy without treating that comparison as readiness. Uses the canonical local container and Unix Docker socket; inherited Docker endpoints/contexts are not used. |
| Health | Fixed root-owned mode-0644 `health/latest.json`, matching controller and schema 1. Copies reported status and approved check IDs/severities at their original timestamp. Admission `capacity` and the capacity sampler service become distinct `capacity` and `capacity_service` diagnostics. Package-update state and its service become `updates` and `updates_service`; dynamic container, build-cache, capacity timer and reconciliation service checks remain visible. Omits detail text and numeric helper fallbacks. These are reported check results, not independent probe or timer-activation attestations. |
| Reconciliation | Protected `reconcile/state.json`. Copies controlled status, desired/applied commits, check time and last-success time. Requires a matching installation context and a check at or after its recorded installation. |
| Boot and session | Linux `/proc/stat` supplies the boot time. Controller status supplies the time the authenticated message session opened. The producer discards pre-boot session evidence and omits host and session identifiers. |
| Enrollment identity | After rendered configuration matches installation, checks GitHub App identity settings and the key file's protected metadata. Exports only `present` or `absent`. The check never opens the key file. |
| Enrollment authentication and scale set | The controller records the successful scale-set check and authenticated session opening at startup. The adapter checks the boot time, running revision, configuration commit, expected scale-set name and routing labels before publishing the recorded results. |
| Enrollment listener | Uses the last successful response to an existing authenticated message poll. Before the first successful poll, readiness is unknown. A controller heartbeat, an open session, or a running process alone supplies no poll evidence. |
| Enrollment reporting and owner confirmation | The health collector records its existing signed delivery outcome and completion time, including optional reporting configured on the host. Publication and readback bind delivery evidence to the applied configuration commit. A suppressed send supplies no evidence. The collector explicitly marks signed reporting absent when it uses the legacy heartbeat path or has no reporting destination. GitHub App owner confirmation has no durable producer and remains unsupported. |
| Runtime and manager selection | Reads the installer's protected runtime and manager selection pointers and checks each release's revision marker. Staging requires a selected manager that matches the independently desired engine while the selected runtime still matches the installed engine. When a successful installer run selects only the manager, reconciliation records the candidate as pending and keeps the installed commit and last-known-good record. Selection identifies the filesystem revision. It does not attest release integrity, service activation, or a completed installer transaction. |
| Desired engine | The reconciler records the controller's engine revision and validation time from the approved default-branch configuration. An installed-only check or explicit recovery preserves that result and its time. A lock timeout leaves the existing record intact. A rejected default-branch candidate or older reconciliation record yields no desired-engine evidence. |
| Image verification | Reuses the existing managed-image verifier for the protected, portable controller and runner bindings. A bounded read-only Docker check verifies the local image objects, platform and daemon/store binding against the rendered policy. Exports the successful check time, engine revision and architecture. Legacy installs without portable proof remain unsupported. The check does not identify the image a container is using. |
| Capacity | Uses the active pool's valid anonymous samples from the last seven days. Exports the latest included sample time, sample and runner-observation counts, and p50/p95 for approved CPU, memory, swap, root/Docker disk and inode metrics. An idle, empty or unreadable history supplies no current measurement. Individual samples and job associations stay private. |
| Transaction | Uses the controlled pre-transaction rejection phase/reason, terminal verified rollback, or installer outcome preserved by reconciliation. The record must identify the controller and match the applied configuration. Convergence, legacy records without controller identity, and free-text messages alone supply no transaction proof. |

The jobs source carries the generation, pending status, and listener and coverage evidence described below. The installation engine field records applied policy. The controller's reported software revision, selected filesystem release, desired engine and verified image bindings each have their own source.

Health helpers can substitute zero counts or a configured engine revision after failed reads. The snapshot excludes those fallback values and uses existing health reports and capacity samples without rerunning their probes. The Docker status read has a 64 KiB output cap and a five-second deadline. Image verification has a separate five-second budget. The capacity history read is capped at 128 MiB and the sampler's 26,000-record limit. Its aggregation has a 15-second processing budget. The snapshot reports unavailable when the history exceeds those bounds.

Disk-admission details are not part of this v1 projection. A future controller
field does not automatically become an approved export, and counts or health
severity cannot substitute for its decision. The
[disk-pressure observation note](DISK-PRESSURE-OBSERVATION.md) records the
current-source evidence limits and non-normative proposal for #166/#118/#120.
Snapshot v1's listener poll evidence does not describe disk admission.

## Protected publication

The base `/var/lib/ci-fleet-observer` and its `exports` child must already be root-owned with the dedicated observer GID and mode `0750`. The existing private `/var/lib/ci-fleet` and `/etc/ci-fleet` trees keep their permissions.

The store pins directory descriptors, rejects symlink ancestry and unsafe writable parents, and validates regular files, ownership, exact mode, group, one hard link, size, and JSON before accepting data. Private export directories and files reject access/default ACLs, including inherited permissions beyond the observer group. Root-owned sticky `/tmp` is allowed for isolated development fixtures; it does not allow a writable export base. JSON has a 32-level nesting limit and rejects duplicate keys, nonfinite numbers and invalid UTF-8.

The export directory permits only `endpoint.json`, `jobs.json`, and their fixed `.tmp` counterparts. Bounds remain 64 KiB per endpoint file, 4 MiB per job file, and 16 MiB overall. A nonblocking Linux directory-descriptor lock covers incumbent readback, source collection, validation and replacement without another file. A competing producer fails with `busy` before collecting sources. Recovery removes only validated temporary files, including legitimate root-owned mode-0600 files interrupted before group assignment. It never serves or promotes temporary contents.

The producer exclusively creates `endpoint.json.tmp` at mode `0600`, assigns its observer GID before setting mode `0640`, flushes the complete bounded payload, atomically replaces the final file, and flushes the directory. Failure before replacement preserves the incumbent export. Reads authenticate first, close descriptors after bounded reads, and cannot access private source stores through the consumer API.

## Worker job history

The existing authenticated scale-set listener feeds a bounded, nonblocking
queue that admits up to 16 batches of up to 256 events each. The controller writes to an already prepared,
root-owned `/var/lib/ci-fleet-observer/private` directory at mode `0700`.
It keeps `records` and its small state files at mode `0600`. Each record
occupies a checksummed 4 KiB slot. Changes rewrite individual slots. The
5,000 slots and four capped state and staging names use less than 20 MiB of
logical file space. A background sweep clears retained payloads after seven
days even when the listener is idle. This directory is independent of the
controller status file and anonymous capacity history.

Before polling, the controller attempts to commit an open-session marker
within its startup budget. A failed or delayed observer startup leaves CI
available and the observation source unavailable. A verified listener
shutdown clears the marker after the queue flushes. An interrupted session,
invalid slot, full event queue, or retention cap leaves controlled evidence
of the gap. Each restart ends the previous session's claim of current
activity. A later session can prove jobs it observes directly. It cannot
recover missing events. Runner exit leaves an unknown result, and a
recovered runner can have an unknown job association.

The privileged publication command uses the existing export lock. Run it on
the observer's 30-second cadence when #122 prepares the service. The jobs
source reports the listener's successful poll time and separately timestamps
the publisher's check. The publisher verifies the private records file and
the controller's lifetime writer lock before accepting listener liveness.
The publisher checks the records file against the committed file identity and
checks the populated-slot count when building a new projection. Counts report only `TotalAvailableJobs` and
`TotalAssignedJobs` in statistics from this scale-set session, with the time
the controller observed them. The scopes and counts have no effect on
scheduling. The covered time range uses local observations. Queue,
scale-set assignment, runner assignment and finish times come from the
source when supplied. Start and completion observation times are local to
the listener. Run URLs use the configured GitHub origin, currently
`https://github.com`, and validated owner, repository and workflow run ID.
The opaque job ID has no verified REST correlation.

`jobs.json` caps the projection at 4 MiB. It orders nonterminal records
first, then terminal records by source finish time when available or local
completion time. It reports retained, exported and omitted counts, including
omitted nonterminal records. The generation changes when records or coverage
change. A boot-bound monotonic allowance and durable in-flight reservation
enforce at least 60 seconds between full-write attempts. An uncertain
allowance requires a fresh 60-second cooldown. Liveness-only publication keeps
the previous job file.

`SnapshotReader.read_job_history(headers)` authenticates before reading either
export. It caches only the validated current generation. Running activity
requires a matching generation, a clear pending flag, fresh publisher and
listener evidence, and sufficient projection coverage. Otherwise the
authenticated read shows unknown current activity while preserving terminal
results and original observation times. The aggregate activity flag also
accounts for retained coverage gaps and loss of nonterminal records. A
directly observed running job can remain current while aggregate coverage is
unknown. Missing summary evidence defaults
to unknown activity. The authenticated read also returns the validated
publication fingerprint for pagination and the endpoint evidence used to
assess current activity. The API accepts only the fixed job file.

## Verification

```bash
python3 scripts/test_endpoint_snapshot.py
python3 scripts/test_endpoint_snapshot_sources.py
python3 scripts/test_endpoint_snapshot_store.py
python3 scripts/test_endpoint_jobs.py
(cd controller && go test -v -run 'TestJob(History|Observer)')
```

The job fixtures report allocated and logical bytes and elapsed write times in the test output. The focused tests cover source identity, timestamp provenance, missing or invalid evidence, redaction, unknown and stale states, authentication before file access, protected permissions and paths, interrupted publication, and bounded subprocess and file reads. The repository's [full validation](LOCAL-VALIDATION.md) records test durations and stage logs. Private-directory provisioning, the controller mount, publisher activation, and live runner and reconciliation continuity during observation failures require #122 acceptance on an approved host.
