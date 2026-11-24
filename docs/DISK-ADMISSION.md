# Disk-pressure job admission

The controller checks disk headroom before advertising capacity to GitHub,
acquiring a job offer, and creating a runner. While blocked, it polls with a
maximum capacity of zero and continues processing job starts, completions,
and cleanup. It leaves existing runners alone. The configured maximum takes
effect again after the disk check recovers. See [issue #166](https://github.com/RandomDevelopment/ci-fleet/issues/166).

## Limits and recovery

A stop condition on any monitored filesystem blocks admission. Recovery
requires all three recovery limits on every monitored filesystem.

| Measurement | Stop | Resume |
| --- | --- | --- |
| Unavailable bytes as a share of capacity | At least 90% | At most 85% |
| Available bytes | Below 10 GiB | At least 15 GiB |
| Inode use | At least 90% | At most 85% |

Missing, invalid, or stale evidence also blocks admission. A controller restart
requires the recovery limits. After recovery, a worker stays eligible until it
hits a stop condition or loses valid evidence.

Capacity uses `f_blocks * f_frsize`. Available bytes use `f_bavail * f_frsize`,
which excludes space reserved for root. The percentage is
`(capacity - available) / capacity`. Inode use is
`(f_files - f_ffree) / f_files`. The 10 GiB floor allows for two 1 GiB Docker
build-context uploads with 8 GiB left for other activity. The 5 GiB and
five-percentage-point recovery margins reduce repeated stop and resume cycles.
These are initial reserves. Measure concurrent workspace and build growth
before treating them as sufficient for a particular worker. The guard reserves
no bytes per job. Running jobs can continue consuming space.

The [storage retention policy](HOST-MAINTENANCE.md#docker-storage-retention) and
the installer's below-80% upgrade check have their own thresholds. A disk
admission block leaves both policies in effect.

## Local status

On a managed worker, use the controller's existing local status file.

```bash
sudo docker exec ci-fleet-controller-1 cat /run/ci-fleet/status.json |
  jq -e '.disk_admission'
```

`low_disk` gives the triggering threshold. Check each entry in `filesystems` for
its roles, capacity, available bytes, and inode counts. Roles that share a
filesystem share one entry. `blocked` with reason `recovering` means at least
one recovery limit still fails. `unknown` means the evidence cannot establish
headroom. Unknown counters use null. A stale sample can retain historical
counters with reason `measurement_stale`; publishing status leaves the
measurement timestamp intact. `ready` means disk admission is open. Existing
runner capacity, network and maintenance controls still apply.

The status is local to the controller. [Endpoint snapshots](ENDPOINT-SNAPSHOTS.md)
and [outbound status reports](STATUS-REPORTING.md) use their existing schemas.

## Measurement and installation

The existing capacity timer attempts a disk sample every 30 seconds, including
when the worker has no runners. Capacity history still records active-runner
samples. The host sampler reads Docker's data root and the backing filesystems
for the standard runner workspace and Docker proxy upload spool. It supports
the local `overlay2` driver and the containerd `overlayfs` snapshotter, including
a snapshotter on a separate filesystem. An unsupported driver, an unexpected
mount, or a failed inspection yields unknown evidence for affected roles.

The manager prepares a root-owned export. Compose mounts only that directory,
read-only, into the controller. The export includes the controller identity and
kernel boot ID to reject evidence from another controller or boot. Controller
status contains neither the boot ID nor host paths. The controller rejects
measurements over 75 seconds old or dated in the future. Sample timing depends
on the host and sampler completion. An in-flight poll or acquisition can race
a change in headroom.

Use a manager that supports the disk export before selecting a runtime that
requires it. Initial health and timer startup can succeed before the first
measurement allows jobs. Check the local admission status after convergence.
A successful rollback to an older engine restores that engine's admission
behavior. Updated managers remove their export after a successful downgrade
or uninstall.

## GitHub service behavior to verify before rollout

The pinned [scale-set listener](https://github.com/actions/scaleset/blob/v0.5.0/listener/listener.go)
allows zero advertised capacity and acknowledges a batch after its handler
succeeds. The controller skips `AcquireJobs` during a disk block, including
when an offer arrives in a poll that started before the block. The listener
can still acknowledge that batch after completing its other work. GitHub's
[SDK documentation](https://github.com/actions/scaleset/blob/v0.5.0/README.md#message-acknowledgment)
describes replay of unacknowledged messages. Whether GitHub re-offers an
acknowledged, unacquired job after recovery remains an
[open upstream question](https://github.com/actions/scaleset/issues/107#issuecomment-6759846997).

GitHub can also send `JobAssigned` directly and dispatch work to a registered
runner. The controller blocks new runner creation during pressure, but cannot
recall a job GitHub has already assigned. Verify that zero-capacity polling
prevents new direct assignments to a prewarmed worker. Also verify that work
queued across a pressure transition runs after recovery, including the case
where an offer arrives in an in-flight poll. A fake service can verify local
polling, completion, and recovery behavior. The GitHub routing and re-offer
cases need service evidence before rollout.

Use [exact-source and base validation](LOCAL-VALIDATION.md) for engine checks.
A disposable worker used to prove admission also needs at least 15 GiB
available on each monitored filesystem at startup, with enough additional
headroom to meet the 85% limits and run the workload. Cover cold startup with
zero runners, prewarmed runners, both supported storage layouts, and recovery
after active-job completion. Verify scoped cleanup for success, failure,
cancellation, and timeout. [Template issue #49](https://github.com/RandomDevelopment/ci-fleet-config-template/issues/49)
tracks adoption guidance pending the engine change.
