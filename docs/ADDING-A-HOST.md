# How to add a fleet host

Use this runbook to add a virtual machine, physical computer, remote-site machine, or VPS to an existing ci-fleet installation.

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

## 4. Declare the host in private Git configuration

Create or update one schema-v3 controller entry in the organization's private configuration repository. Give it a unique logical ID and scale-set name, assign its shared runner pool, start with reviewed capacity, and pin a full ci-fleet engine commit.

Run that repository's strict validator before merge. Do not put the machine's address, VM ID, storage, backup identifier, or credential in Git.

See [Git-authored controller desired state](DESIRED-STATE.md) for the complete contract.

## 5. Prepare host-local identity

From a reviewed ci-fleet checkout:

```bash
sudo install -d -m 0700 /etc/ci-fleet/secrets
sudo install -m 0600 host/host.env.example /etc/ci-fleet/host.env
sudo install -m 0600 /secure/source/github-app.pem /etc/ci-fleet/secrets/github-app.pem
```

Edit `/etc/ci-fleet/host.env` locally. Prefer a separately generated GitHub App private key per host so one location can be revoked without replacing every host key. Never expose the key to runner or project containers.

## 6. Install or adopt with one command

```bash
sudo ./scripts/install-worker-controller.sh \
  --install \
  --config-repo example-org/example-fleet-config \
  --ref 1111111111111111111111111111111111111111 \
  --controller example-ci-01
```

Use `--adopt` when converting an existing manual controller. The installer validates the complete configuration, renders host-local runtime state, installs the pinned engine, creates a controller checkpoint, builds images, installs maintenance timers, and verifies health.

The configuration repository credential, when required, must be read-only and host-side. Credentials are never accepted in command arguments.

## 7. Prove one job

Confirm no runner exists while idle. Dispatch one manual, read-only job from an authorized private project and verify:

- one ephemeral runner appears;
- it accepts one job;
- it is destroyed afterward;
- no job-owned container, network, volume, or workspace remains;
- `scripts/healthcheck.sh` passes;
- scoped cleanup identifies no expired resource.

Follow [Live pilot](LIVE-PILOT.md) for the complete proof and rollback.

## 8. Verify unattended operations

The installer enables health, scoped cleanup, and pinned desired-state drift timers. Run each service once and inspect its journal as described in [Host maintenance](HOST-MAINTENANCE.md). Configure redacted local checks and external missed-heartbeat detection as described in [Fleet health monitoring](HEALTH-MONITORING.md).

The steady-state host should need no project-specific edits. Adding a project changes its repository, private fleet configuration, and GitHub runner-group policy—not this host.

## 9. Increase capacity safely through Git

Raise the controller's schema-v3 `max_runners` and its pool `capacity_budget` through a reviewed private configuration change only after measuring CPU, memory, disk, network, cache growth, collisions, cancellation, and cleanup. Keep explicit per-runner limits.

Keep the one-runner pilot preflight unchanged. For any later increase, follow [Post-pilot capacity promotion](CAPACITY-PROMOTION.md), run `scripts/capacity-preflight.sh` before and after the controller-only recreation, and retain the new maximum only when the separately authorized workload and cleanup proof satisfy the predeclared resource policy.

Use another host or location when it improves failure tolerance. Give every added host a new instance and scale-set name while retaining the shared routing label.

## 10. Drain, replace, or remove a host

1. Merge a private configuration change setting the controller to `drained`.
2. Apply that exact commit with `install-worker-controller.sh --upgrade`.
3. Confirm zero managed runners and run scoped cleanup.
4. Run `install-worker-controller.sh --uninstall` when the host will not return.
5. Verify the host's unique scale set is absent from GitHub.
6. Revoke that host's GitHub App private key and explicitly remove preserved credentials.
7. Set the controller to `disabled` or remove it from private configuration.
8. Delete or repurpose the machine according to the site's recovery policy.

Other hosts and project workflows continue using the shared routing label. Replacing one host must not require editing any project workflow.
