# How to add a fleet host

Use this runbook to add a Proxmox VM, physical computer, remote-site machine, or VPS to an existing ci-fleet installation.

## Outcome

A host is a generic, location-independent Docker appliance. Configure it once. It must not contain repository names, project runtimes, project secrets, or project-specific test logic.

Each controller has a unique instance and scale-set name. Compatible controllers share the same routing label and runner group, so GitHub can route queued work to any available location.

| Setting | Per host | Shared by compatible hosts |
| --- | --- | --- |
| `CI_FLEET_INSTANCE` | Unique | No |
| `CI_FLEET_SCALE_SET_NAME` | Unique | No |
| `CI_FLEET_LABELS` | No | Yes |
| `CI_FLEET_RUNNER_GROUP` | No | Yes |
| Capacity and resource limits | Usually | No |
| GitHub organization | No | Yes |
| Repository allowlist | Never stored here | GitHub runner-group policy |

Example:

```dotenv
# Home
CI_FLEET_INSTANCE=home-ci-01
CI_FLEET_SCALE_SET_NAME=docker-ci-home-ci-01
CI_FLEET_LABELS=docker-ci
CI_FLEET_RUNNER_GROUP=trusted-private-ci

# Remote building
CI_FLEET_INSTANCE=warehouse-ci-01
CI_FLEET_SCALE_SET_NAME=docker-ci-warehouse-ci-01
CI_FLEET_LABELS=docker-ci
CI_FLEET_RUNNER_GROUP=trusted-private-ci

# VPS
CI_FLEET_INSTANCE=vps-ci-01
CI_FLEET_SCALE_SET_NAME=docker-ci-vps-ci-01
CI_FLEET_LABELS=docker-ci
CI_FLEET_RUNNER_GROUP=trusted-private-ci
```

## 1. Choose the host and failure boundary

The host must:

- run no production or development application workload;
- have no sensitive host mounts, SSH agent, or unrelated Docker workload;
- use local or virtualized storage suitable for Docker's write load;
- have reliable DNS, time synchronization, and outbound HTTPS access to GitHub and required registries;
- permit local administration without requiring public inbound access;
- be disposable or recoverable from a documented build and backup procedure.

Runners sharing one Docker daemon share a security boundary. Add a host instead of increasing same-host concurrency when projects need stronger separation or when one site's failure must not stop the fleet.

## 2. Install the generic host

Install a supported Linux distribution, Docker Engine, Docker Compose v2, Git, Bash, `curl`, `jq`, CA certificates, the QEMU guest agent when virtualized, and basic diagnostics.

Do not install Node, PHP, Python, Java, Composer, npm, database clients, or other project runtimes. Projects bring those in their own images.

Enable unattended security updates without automatic reboots. Configure Docker log rotation and host disk alerts. Follow [Host maintenance](HOST-MAINTENANCE.md).

## 3. Record backup and network readiness

Before placing credentials on the host:

1. verify console or local recovery access;
2. verify the host has a unique name and address;
3. create a VM backup, snapshot, or equivalent recoverable baseline;
4. record the backup destination and successful timestamp outside this public repository;
5. verify the host can reach GitHub and required container/package registries.

A remote site needs outbound connectivity; it does not need project-specific inbound access. If one location is offline, compatible jobs remain queued for another available host.

## 4. Install a pinned ci-fleet checkout

Use a reviewed immutable commit:

```bash
sudo install -d -m 0755 /opt/ci-fleet
# Populate /opt/ci-fleet from a reviewed ci-fleet commit.
git -C /opt/ci-fleet rev-parse HEAD
```

Do not follow a moving branch for unattended controller updates. Fleet image changes use reviewed rolling replacement and keep the previous version available for rollback.

## 5. Create host-local configuration

Start from `deploy/ci-fleet.env.example`:

```bash
sudo install -d -m 0700 /etc/ci-fleet/secrets
sudo install -m 0600 deploy/ci-fleet.env.example /etc/ci-fleet/ci-fleet.env
```

Set:

- a unique `CI_FLEET_INSTANCE`;
- a unique `CI_FLEET_SCALE_SET_NAME`;
- the fleet's shared `CI_FLEET_LABELS`;
- the shared `CI_FLEET_RUNNER_GROUP`;
- host-specific maximum concurrency and runner CPU/memory limits;
- `CI_FLEET_MIN_RUNNERS=0` so idle runners do not persist.

Repository names do not belong in this file.

Provision the GitHub App private key directly into `/etc/ci-fleet/secrets/github-app.pem` with mode `0600`. Prefer a separately generated App private key per host so one location can be revoked without replacing every host key. Never expose the key to runner or project containers.

## 6. Validate before starting

```bash
cd /opt/ci-fleet
set -a
. /etc/ci-fleet/ci-fleet.env
set +a
scripts/preflight.sh
docker compose -f deploy/compose.yaml build runner-image controller
scripts/preflight.sh
```

Continue only when preflight reports `PREFLIGHT_OK warnings=0`.

## 7. Start at zero and prove one job

Start with maximum concurrency one even if the host will eventually run more:

```bash
docker compose -f deploy/compose.yaml up -d --no-deps controller
docker compose -f deploy/compose.yaml logs --tail=100 controller
```

Confirm no runner exists while idle. Dispatch one manual, read-only job from an authorized private project and verify:

- one ephemeral runner appears;
- it accepts one job;
- it is destroyed afterward;
- no job-owned container, network, volume, or workspace remains;
- `scripts/healthcheck.sh` passes;
- scoped cleanup identifies no expired resource.

Follow [Live pilot](LIVE-PILOT.md) for the complete proof and rollback.

## 8. Enable unattended operations

Install the health and cleanup timers from [Host maintenance](HOST-MAINTENANCE.md). Monitor controller state, disk thresholds, Docker health, last successful job, cleanup failures, and pending reboot state.

The steady-state host should need no project-specific edits. Adding a project changes its repository, private fleet configuration, and GitHub runner-group policy—not this host.

## 9. Increase capacity safely

Raise `CI_FLEET_MAX_RUNNERS` only after measuring CPU, memory, disk, network, cache growth, collisions, cancellation, and cleanup. Keep explicit per-runner limits.

Use another host or location when it improves failure tolerance. Give every added host a new instance and scale-set name while retaining the shared routing label.

## 10. Drain, replace, or remove a host

1. Stop new capacity by setting maximum capacity to zero or stopping the controller.
2. Confirm no managed runner is active.
3. Run cleanup in dry-run mode, review candidates, then apply scoped cleanup.
4. Verify the host's unique scale set is absent from GitHub.
5. Revoke that host's GitHub App private key.
6. Securely delete host-local credentials.
7. Remove or restore the machine according to the site's recovery policy.

Other hosts and project workflows continue using the shared routing label. Replacing one host must not require editing any project workflow.
