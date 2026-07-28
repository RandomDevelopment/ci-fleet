# Fleet health monitoring

Every managed controller runs a redacted local health check every five minutes. An external monitor must also detect a host that cannot report because it is offline.

## Local check

```bash
sudo /opt/ci-fleet/manager/current/scripts/healthcheck.sh
sudo /opt/ci-fleet/manager/current/scripts/healthcheck.sh --json
sudo cat /var/lib/ci-fleet/health/latest.json
```

Exit codes are `0` for healthy or intentional maintenance, `1` for warning, and `2` for unhealthy. The JSON schema is versioned and the latest result is replaced atomically. It contains controller identity, desired lifecycle state, status, timestamp, and redacted check results only.

The check covers:

- root and Docker filesystem space and inodes;
- available memory, swap use, per-CPU load, and OOM evidence from the last 24 hours;
- Docker availability, controller state/restarts, and configured versus effective capacity;
- inactive, unhealthy, restarting, and stale fleet-labelled resources, including week-old build cache;
- cleanup, drift, health, and update services/timers;
- failed package state, pending reboot, and clock synchronization;
- an optional host-local backup check;
- optional authenticated outbound status delivery.

It reports but never prunes, restarts, or repairs resources. Project source, logs, environment values, tokens, and private keys are never included.

## Threshold overrides and hooks

Defaults are intentionally conservative: disk and inode warning/critical at 80/90%, available memory warning/critical at 15/8%, sustained swap use under five-minute memory pressure warning/critical at 25/50%, per-CPU fifteen-minute load warning/critical at 1.0/1.5, and controller restart warning at 3.

Optional overrides belong in `/etc/ci-fleet/monitoring.env`, owned by root with mode `0600`:

```text
CI_FLEET_HEALTH_DISK_WARN_PERCENT=80
CI_FLEET_HEALTH_DISK_CRITICAL_PERCENT=90
CI_FLEET_HEALTH_INODE_WARN_PERCENT=80
CI_FLEET_HEALTH_INODE_CRITICAL_PERCENT=90
CI_FLEET_HEALTH_MEMORY_WARN_AVAILABLE_PERCENT=15
CI_FLEET_HEALTH_MEMORY_CRITICAL_AVAILABLE_PERCENT=8
CI_FLEET_HEALTH_SWAP_WARN_PERCENT=25
CI_FLEET_HEALTH_SWAP_CRITICAL_PERCENT=50
CI_FLEET_HEALTH_LOAD_WARN_PER_CPU=1.0
CI_FLEET_HEALTH_LOAD_CRITICAL_PER_CPU=1.5
CI_FLEET_HEALTH_RESTART_WARN_COUNT=3
CI_FLEET_HEALTH_BACKUP_CHECK=/usr/local/sbin/ci-fleet-backup-check
CI_FLEET_HEALTH_STATUS_URL=https://status.example.invalid/v1/status
CI_FLEET_HEALTH_STATUS_KEY_FILE=/etc/ci-fleet/secrets/status-reporting.key
```

The backup hook must be an absolute, executable, root-owned file that is not group- or world-writable. Its output is discarded; only its exit status is reported. Status delivery requires HTTPS and a unique 32-128 byte root-owned key file with mode `0600`. The installer never creates, prints, commits, or removes host-local monitoring credentials, so rollback preserves them. Delivery failures are warnings and never block runners or reconciliation.

See [authenticated controller status reporting](STATUS-REPORTING.md) for the v1 schema, request authentication, receiver, retention, API, and threat model.

## External missed-report detection

The authenticated receiver stores each controller's `generated_at` and returns it through the read-only API. An external monitor compares the latest report with reviewed desired controller inventory and treats an active controller with no report inside the grace period as unhealthy. Drained or disabled lifecycle state remains a desired-state decision, not something an absent controller can assert.

The legacy file-based `health.py heartbeats` evaluator remains available for existing integrations, but new deployments should consume the authenticated API described in [STATUS-REPORTING.md](STATUS-REPORTING.md).

## Operations

- **Disk/inodes:** inspect fleet-labelled resources and run `scripts/cleanup.sh` in report mode first. Never use global Docker prune.
- **Docker/controller:** drain if possible, inspect Docker and controller journals, then apply only reviewed desired state.
- **Drift/timer failure:** run the named service manually and `install-worker-controller.sh --check`; repair by applying the reviewed pinned configuration, not by editing rendered files.
- **Memory/OOM/load:** let active jobs drain, inspect kernel evidence, and adjust reviewed infrastructure capacity or runner resources.
- **Updates/reboot:** drain before rebooting; verify all timers and the health result afterward.
- **Missed heartbeat:** verify the receiver first, then use the provider console or out-of-band access. Inbound SSH is not required.
- **Add/replace:** enroll the logical controller through reviewed private desired state, configure its host-local heartbeat credential, and verify a fresh external record before relying on it.
- **Retire:** set lifecycle/state through reviewed desired state first. Delete no host, runner, or production resource without separate authorization.
