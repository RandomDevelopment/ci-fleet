# Local capacity telemetry

The dedicated capacity timer checks every 30 seconds and records a sample only while at least one managed runner is active. This interval captures normal jobs shorter than the five-minute health period without increasing heartbeat or status-report traffic. Samples remain on the controller host at `/var/lib/ci-fleet/capacity/samples.jsonl`; they are never added to status-reporting or heartbeat payloads.

Each sample contains only:

- timestamp and logical controller/pool ID;
- interval host CPU utilization plus memory, swap, disk-byte, and inode counters;
- anonymous per-runner CPU percentage and memory use/limit.

Container IDs, names, repositories, jobs, logs, environment variables, source, network counters, and credentials are not stored. The history directory must be a root-owned, non-symlink mode-`0700` directory and the file must be mode `0600`. Malformed records are ignored atomically, records older than eight days are discarded even while the pool is idle, and history is compacted from at most 26,000 records back to 24,000 (more than eight days at the scheduled 30-second interval). Normal samples append one record. A protected index avoids decoding history on every tick. A full validation runs at least hourly, when the oldest sample expires, when the record limit is reached, or when the file size changes outside sampling. Compaction rewrites only when retention, malformed input, or the rotation threshold requires it. Sampling requires a synchronized clock, and a backward clock correction preserves future-dated records until they become eligible for the report. Committed uninstall or downgrade to an engine without telemetry removes this fleet-owned local history. Failed uninstall and downgrade keep it available for rollback. Host-probe and runner-stat failures fail the capacity service and persist as a private failure event until a health report records them successfully. A later successful sampler tick does not clear that event.

## Weekly report

Run on the controller host; reads local state only and changes nothing:

```bash
sudo python3 /opt/ci-fleet/current/scripts/health.py capacity-report
```

The JSON report groups the preceding seven days by logical pool and gives nearest-rank p50/p95 values for every observed host and runner metric, plus sample and anonymous runner-observation counts. An empty report means no managed runner was observed during the period.

Review the report before changing runner count, per-runner resources, or pool budget. A capacity change still requires its own reviewed private-configuration change; this public repository does not create or modify that external change.
