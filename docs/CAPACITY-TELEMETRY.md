# Local capacity telemetry

The existing five-minute health timer records a capacity sample only while at least one managed runner is active. Samples remain on the controller host at `/var/lib/ci-fleet/capacity/samples.jsonl`; they are never added to status-reporting or heartbeat payloads.

Each sample contains only:

- timestamp and logical controller/pool ID;
- host CPU, memory, swap, disk-byte, and inode counters;
- anonymous per-runner CPU percentage and memory use/limit.

Container IDs, names, repositories, jobs, logs, environment variables, source, network counters, and credentials are not stored. The history directory is mode `0700`, the file is mode `0600`, malformed records are ignored, records older than eight days are discarded, and at most 2,500 samples are retained. Uninstall removes this fleet-owned local history.

## Weekly report

Run on the controller host; reads local state only and changes nothing:

```bash
sudo /opt/ci-fleet/manager/current/scripts/health.py capacity-report
```

The JSON report groups the preceding seven days by logical pool and gives nearest-rank p50/p95 values for every observed host and runner metric, plus sample and anonymous runner-observation counts. An empty report means no managed runner was observed during the period.

Review the report before changing runner count, per-runner resources, or pool budget. A capacity change still requires its own reviewed private-configuration change; this public repository does not create or modify that external change.
