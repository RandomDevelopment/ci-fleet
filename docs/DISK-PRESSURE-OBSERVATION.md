# Disk pressure: observation and evidence gaps

Status: current-source inventory and **non-normative proposal**, reviewed against
engine `a56fad2c47bf6b3f80e839af69615a2b42fc1461`. This document adds no producer,
snapshot field, collector, UI, threshold, or host authorization. The proposed
[#166](https://github.com/RandomDevelopment/ci-fleet/issues/166) admission producer
and its enforcement guarantees require separate implementation and review.
Update or supersede this base-pinned inventory when #166 is integrated; approval
to implement a candidate does not establish acceptance or deployment.

## Reuse the existing evidence

[PR #73](https://github.com/RandomDevelopment/ci-fleet/pull/73) merged local capacity
telemetry; [PR #128](https://github.com/RandomDevelopment/ci-fleet/pull/128) merged the
protected snapshot foundation. Do not recreate either collector.

| Existing source | What it establishes | What it does not establish |
| --- | --- | --- |
| [`controller/status.go`](../controller/status.go), `controllerStatus` | Controller identity, software version, current/busy/maximum counts and status generation time | Disk admission, listener readiness, or why capacity is restricted |
| [`scripts/health.py`](../scripts/health.py), `collect_capacity_snapshot` / `record_capacity` | Active-runner samples: host CPU/memory/swap, root/Docker used bytes and inodes, anonymous runner CPU/memory | Available bytes, filesystem capacity history, workspace/spool mapping, I/O latency, PSI, or job attribution |
| [`scripts/endpoint_snapshot.py`](../scripts/endpoint_snapshot.py), `project_controller` / `project_health` | Allowlisted counts/version and reviewed-maximum comparison, controlled health check IDs/severities, original source timestamps | Admission details, raw health helper values, capacity history, cleanup receipts or overall readiness |
| [`scripts/cleanup_storage.py`](../scripts/cleanup_storage.py), `cleanup_storage` | Bounded maintenance report and rounded filesystem utilization where inspection succeeds | A durable structured cleanup outcome, net reclaimed bytes, or current admission state |

The [snapshot v1 contract](ENDPOINT-SNAPSHOTS.md#snapshot-version-1) remains
unchanged: readiness-related sources and capacity history are unsupported.
`project_controller` deliberately projects only its approved fields; a new
`disk_admission` field in controller output would **not** automatically reach a
snapshot consumer. A future extension needs an explicitly reviewed versioning,
projection, readback-validation and old-export compatibility decision. Prefer the
existing privileged bounded status read (five seconds, 64 KiB) for future snapshot
consumption unless a reviewed decision replaces it; never give the read-only
consumer Docker access or access to private source stores. Outbound
[status-report v1](STATUS-REPORTING.md) is a different contract and is unchanged.

## Proposed admission observation semantics

These are proposed requirements and open questions for #166 review, not a JSON
schema or a claim about current SDK job-acquisition enforcement. Names such as
`disk_admission` and `blocked_since` are illustrative, not reserved fields. The
controller owns the admission decision; measurement may originate in a separately
reviewed bounded host export. Measurement and decision ownership are distinct,
and both must preserve the actual measurement time:

- Separate component decision (`ready`, `blocked`, `unknown`) from source
  availability and freshness. Disk `ready` does not override drain, maintenance,
  network, authentication or listener restrictions, or establish worker readiness.
- Use a controlled reason, actual measurement timestamp, and effective restriction
  and recovery condition. Status-file publication time must not refresh a cached
  measurement. A `blocked_since` value means an observed transition only; if no
  durable continuity exists across restart, do not invent one.
- Report capacity and available bytes for every monitored filesystem, not a single
  unexplained aggregate. Use only approved role labels (proposed: workspace,
  Docker, spool); group roles proved to share a filesystem without exporting
  device identifiers, mount paths or addresses. Never sum aliases. Failure to
  resolve a role is unknown, not permission to substitute another filesystem.
- Available means bytes usable by the measured workload context, not simply total
  minus used bytes. Define the producer's filesystem accounting and percentage
  formula explicitly. Preserve missing byte values as absent/null, never zero;
  actual measured zero remains a valid low-space result. Do not infer inode
  admission from a byte check; scope and recovery rules require their own decision.
- Recovery fields should expose the effective reviewed conditions and comparison
  semantics, not recommend new numeric policy. Require fresh evidence for every
  monitored role and the producer's hysteresis before claiming disk recovery.
  Inspection failure and stale evidence cannot count as recovery.
- Keep missing/unsupported producer evidence, failed/malformed inspection, stale
  last-known evidence, and a current producer `unknown` distinguishable. Future
  timestamps or uncertain clock ordering cannot establish freshness. Preserve
  original observation time on retained evidence; never relabel historical
  `ready` as current after a failed read.

The existing snapshot reader already separates availability from derived
freshness and treats controller evidence as stale after 120 seconds. That is not
a proposed admission-probe TTL: a future extension must evaluate the embedded
measurement time too, with a producer-supported bound. Do not change current
reporting rates or claim durable diagnostics from the current status file.

## Cleanup is independent evidence

Follow [host maintenance](HOST-MAINTENANCE.md#docker-storage-retention), not an
observation-driven deletion rule. Suppressed admission does not prove an idle
host. Idle runners and running, restarting or paused uncoordinated leftovers can
still prevent applying maintenance. Existing installer/proxy locks, workload
checks, uncertainty and current/checkpoint image protections remain authoritative.

Distinguish these outcomes only when evidence supports them:

- **Deferred:** maintenance did not establish permission to apply. A successful
  unit exit can still represent deferral.
- **Command completed:** a removal/prune request returned successfully. It may
  have reclaimed nothing. Text saying `REMOVE`, object counts, cache eligibility,
  Docker reclaimable estimates and timer enablement do not establish freed bytes.
- **Measured headroom change:** comparable before/after filesystem observations
  establish a net available-byte change over a stated interval. Concurrent writes
  or other cleanup prevent attributing that delta solely to one command. Rounded
  percentages can hide both small changes and continuing pressure.
- **Admission recovery:** fresh controller evidence satisfies its own recovery
  conditions. This is separate from cleanup completion, the installer's unchanged
  below-80% gate, a later applying scheduled pass, and successful reconciliation.

The existing report is text, not a new telemetry API. Do not parse it into a
fabricated structured receipt or expose raw logs. Durable sanitized outcome
publication remains a follow-up under
[#120](https://github.com/RandomDevelopment/ci-fleet/issues/120); scheduled live
retention acceptance remains [#111](https://github.com/RandomDevelopment/ci-fleet/issues/111).

## Bounded investigation of consumer stalls

[#139](https://github.com/RandomDevelopment/ci-fleet/issues/139) records build copy,
cached-layer/metadata and image-removal stalls, plus a fake-subprocess timeout.
The records also include brief I/O pressure samples and manual disk recovery;
none establishes a storage-capacity cause. A healthy proxy ping or shared-lock
count alone does not establish mutation progress or a deadlock.

A read-only evidence refresh on 2026-11-09 confirmed the consumer
[plan at tested head `04df04cf55e8640dfd99a7d3cbb84991ff6a075e`](https://github.com/RandomDevelopment/asic-testbench/blob/04df04cf55e8640dfd99a7d3cbb84991ff6a075e/scripts/ci/plan.json)
declares shard counts totaling 23; this was a sum of the pinned plan, not execution
of its expansion tool or evaluation of the consumer's current head. The GitHub
jobs API for
[run 38795110132, attempt 4](https://github.com/RandomDevelopment/asic-testbench/actions/runs/38795110132/attempts/4)
confirmed all 23 task steps succeeded and their artifact uploads failed. The
quota explanation is retained issue evidence, not inferred from those step
conclusions. The run artifact listing now returns no artifacts, so archived raw
payloads were not revalidated. Earlier timings remain attributed to the issue's
retained report, not new host measurements. No host was contacted or workload run.

For a separately authorized disposable reproduction, retain the exact source,
engine/image/tool versions and existing 23-shard plan. Compare supported cold and
warm-cache cases without global pruning, a consumer concurrency cap, capacity
changes or timeout relaxation. Record queue delay separately from checkout,
build phases, assertions, canonical cleanup and fallback cleanup. Preserve the
five-minute job ceiling and the consumer's recorded 225-second payload,
35-second fallback cleanup and two five-second termination graces. A warm rerun
or passing assertions without timely canonical completion is not acceptance.

Use existing anonymous capacity samples first; pair them with bounded,
timestamped diagnostic measurements only through separately authorized access:

| Hypothesis, not diagnosis | Evidence needed to distinguish it |
| --- | --- |
| CPU scheduling/throttling | Host interval CPU, load/CPU pressure and relevant cgroup throttling; the fake-subprocess regression is a useful non-Docker control |
| Memory pressure | Available memory, swap, memory pressure and OOM evidence aligned with the task interval |
| Space/inode exhaustion | Actual capacity/available bytes and inodes for each relevant filesystem role, with admission measurement age |
| I/O or filesystem latency | I/O pressure, bounded device latency/queue observations and phase timings; free capacity alone cannot exclude latency |
| Cache/metadata work | Cold/warm provenance, bounded Docker query/build/removal latency, image-store mode/version and context size; image record counts are not byte accounting |
| Maintenance/proxy/contention | Authorized activity timeline and request progress under the existing gates; ping success or lock counts alone cannot identify stuck requests |

Stop at the unchanged workload/diagnostic deadlines and retain missing evidence
as missing. Complete exact-head results, cleanup and applicable migration proof
remain necessary. Do not publish private paths, device inventories, addresses,
job logs or raw command output in this repository, or join anonymous telemetry to
job identities. As of 2026-11-09, no accessible exclusively allocated execution
machine was confirmed for this work; full exact-source `local_validate.py`
acceptance and live reproduction were blocked, not waived.

## Follow-up boundaries

Existing [#50](https://github.com/RandomDevelopment/ci-fleet/issues/50) history can
inform growth, but cannot alone justify a numeric absolute reserve or resume
threshold. See [capacity evidence limits](CAPACITY-TELEMETRY.md#disk-pressure-evidence-limits).
[#118](https://github.com/RandomDevelopment/ci-fleet/issues/118) owns a reviewed
snapshot extension; [#121](https://github.com/RandomDevelopment/ci-fleet/issues/121)
owns eventual read-only presentation. Neither is implemented by this note.

Configuration-template admission guidance must track the final #166 producer and
its observation/versioning gaps before engine merge or release. Existing
[template #44](https://github.com/RandomDevelopment/ci-fleet-config-template/issues/44)
tracks cleanup/recovery guidance; do not duplicate it or confuse issue tracking
with a published template release, adopter promotion or live-host acceptance.
