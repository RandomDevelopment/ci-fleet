# Git-authored controller desired state

Schema v3 makes a reviewed private configuration repository the authority for controller routing, capacity, lifecycle, and engine revision within the fixed `RandomDevelopment/ci-fleet` public engine repository. A configuration-only change cannot redirect root execution to another repository. The target host keeps credentials and rendered runtime state outside Git.

## Responsibility boundary

| Location | Owns |
| --- | --- |
| Public ci-fleet repository | Schema, validator, renderer, installer, controller, maintenance, and fictional examples |
| Private configuration repository | Real project allowlists, runner pools, logical controller IDs, capacity budgets, controller state, resource limits, and pinned engine revisions |
| Root-owned host files | GitHub App identity, private key path, rendered environment, checkpoints, and installation state |
| Application repository | Independent CI tasks and shards; never fleet size or controller identity |

Host addresses, VM IDs, storage names, backup identifiers, SSH details, tokens, private keys, and rendered `.env` files are rejected from the Git-authored configuration.

## Schema v3

Each runner pool declares:

- a logical GitHub runner group;
- stable shared routing labels;
- explicitly allowed repositories;
- `public_repositories: false`;
- an infrastructure `capacity_budget`;
- `job_submission_policy: all-independent-jobs`.

Each controller has a unique object key and declares:

- its pool and logical location;
- `active`, `drained`, or `disabled` state;
- a unique scale-set name;
- `experimental`, `stable`, or `retiring` lifecycle;
- a full pinned ci-fleet engine commit;
- a zero managed minimum and reviewed maximum runner capacity;
- CPU cores and memory per ephemeral runner.

Active and drained controllers reserve their configured maximum against the pool budget. A drained controller has zero effective runtime capacity but keeps its reservation, so an undrain cannot silently overcommit the pool. Disabled controllers reserve no capacity.

Managed prewarmed runners are not currently supported: `min_runners` is fixed at zero in schema, semantic validation, rendering, and preflight. This keeps idle privileged workers absent and prevents reviewed configuration from passing validation only to fail host adoption.

The authoritative fictional contract is in [`templates/config-repository`](../templates/config-repository/README.md).

## Application parallelism rule

Application workflows submit every independent task and shard. They must not use GitHub Actions `strategy.max-parallel` to represent how many fleet workers exist. GitHub queues excess jobs; private infrastructure policy decides how many run simultaneously.

An application may use a concurrency limit only for a documented external-system restriction such as a vendor rate limit or a single-writer test fixture. That exception must not be based on current worker count.

## Prepare host-local identity

Create the root-owned file once:

```bash
sudo install -d -m 0700 /etc/ci-fleet/secrets
sudo install -m 0600 host/host.env.example /etc/ci-fleet/host.env
sudo install -m 0600 /secure/source/github-app.pem /etc/ci-fleet/secrets/github-app.pem
```

Edit `/etc/ci-fleet/host.env` locally. Managed installs intentionally reject alternate host-config paths so scheduled drift checks always verify the same identity file. It contains only the GitHub App client ID, installation ID, private-key path, and runner TTL. Both the host environment and PEM must be root-owned mode `0600`, and the TTL must be at least one hour. Neither file is committed, and the PEM is never printed by the installer.

GitHub App and runner-group creation remain the bootstrap responsibility tracked by [issue #27](https://github.com/RandomDevelopment/ci-fleet/issues/27). The installer fails closed when those prerequisites are absent.

## Install a fresh controller

Run the command from a reviewed checkout of ci-fleet on the target Linux Docker machine:

```bash
sudo ./scripts/install-worker-controller.sh \
  --install \
  --config-repo example-org/example-fleet-config \
  --ref 1111111111111111111111111111111111111111 \
  --controller example-ci-01
```

`--ref` must be a full configuration commit SHA. The installer never follows a moving branch. For a private remote repository, configure a narrowly scoped read-only Git credential on the host before running the command. Do not embed credentials in the URL or command line. A local pinned Git checkout is also accepted.

The installer:

1. fetches only the requested configuration commit;
2. validates the complete schema and capacity relationships;
3. selects exactly one logical controller;
4. renders `/etc/ci-fleet/ci-fleet.env` without secret values;
5. fetches and verifies the pinned public engine commit;
6. creates a root-only controller checkpoint;
7. drains the current controller and waits for every managed runner to finish, including orphaned runners left after a stopped or crashed controller;
8. runs managed preflight and builds the pinned runner and controller images;
9. installs health, cleanup, and pinned-state drift unit definitions;
10. starts the controller only when its desired state is active and verifies runtime health;
11. atomically records redacted installation state, then enables the maintenance timers.

A successful second `--install` run reports `NO_CHANGE` and performs no unnecessary replacement. A successful engine upgrade advances both the runtime release and the maintenance installer-manager to the same pinned commit; rollback restores both.

## Adopt an existing controller

Use adoption for a manually installed controller:

```bash
sudo ./scripts/install-worker-controller.sh \
  --adopt \
  --config-repo example-org/example-fleet-config \
  --ref 1111111111111111111111111111111111111111 \
  --controller example-ci-01
```

If `/etc/ci-fleet/host.env` does not exist, adoption extracts only the approved host identity fields from the existing root-owned `/etc/ci-fleet/ci-fleet.env`. It never copies project settings or secret values into Git. The running controller is paused, existing managed runners are allowed to finish, and activation proceeds only after the host is idle.

## Check, upgrade, roll back, and remove

```bash
# Read-only comparison with one pinned configuration commit
sudo ./scripts/install-worker-controller.sh --check \
  --config-repo example-org/example-fleet-config \
  --ref 1111111111111111111111111111111111111111 \
  --controller example-ci-01

# Apply a newer reviewed configuration and/or pinned engine
sudo ./scripts/install-worker-controller.sh --upgrade \
  --config-repo example-org/example-fleet-config \
  --ref 2222222222222222222222222222222222222222 \
  --controller example-ci-01

# Restore the latest root-only controller checkpoint
sudo ./scripts/install-worker-controller.sh --rollback

# Drain and remove controller services and rendered state
sudo ./scripts/install-worker-controller.sh --uninstall
```

Uninstall removes the controller, timers, rendered environment, and active installation marker. It deliberately preserves `/etc/ci-fleet/host.env`, the secrets directory, and checkpoints so credential destruction and audit retention remain explicit operator decisions.

No mode uses global Docker prune or removes unrelated workloads.

## Drift and reviewed updates

`ci-fleet-drift.timer` checks the host every fifteen minutes against the exact configuration SHA recorded at installation. It detects host edits, missing or stale runtime and installer-manager releases, runtime-state mismatch, altered metadata, and missing maintenance timers without applying changes.

The host does not automatically follow or execute a moving branch. A new configuration becomes effective only when an operator or authorized external controller supplies its reviewed full commit SHA to `--upgrade`. Automatic dispatchers may watch a protected branch and invoke that exact command after merge, using read-only repository contents permission; their identity must remain host-side and unavailable to job runners.

## Drain and retirement

1. Merge a private configuration change setting the controller to `drained`.
2. Run `--upgrade` with that reviewed commit.
3. Verify zero managed runners and zero effective capacity.
4. Remove the controller's GitHub registration and revoke its host identity when it will not return.
5. Set it to `disabled` or remove its declaration in a later reviewed change.
6. Delete or repurpose the machine according to the installation's infrastructure policy.

Legacy project-specific hosts remain until CI, promotion, and deployment no longer reference them. Deleting or replacing a generic controller does not require application workflow changes because projects route through shared labels.

## Failure and recovery behavior

Before mutation, the installer records the prior rendered environment, installation metadata, runtime release, installer-manager release, and maintenance unit/timer state under `/var/lib/ci-fleet/checkpoints`. Each checkpoint is staged and atomically renamed with a completion marker; rollback ignores partial staging directories. Build and validation happen before the active release changes. A failed activation or health check drains the candidate, restores those artifacts, restarts the prior controller only when no managed runner is active, and verifies prior-release health before reporting rollback success. A host-local installer lock serializes every check and mutation. Runtime and installer-manager releases are staged on their respective target filesystems and renamed atomically so a failed copy cannot masquerade as an installed immutable release.

These controller checkpoints do not replace machine backups. Operators still create and verify VM snapshots, physical-host recovery media, or equivalent infrastructure backups according to their local policy.

This contract implements the engine portions of [issue #32](https://github.com/RandomDevelopment/ci-fleet/issues/32) and integrates the installer, documentation, phone-first bootstrap, and capacity work tracked by [#21](https://github.com/RandomDevelopment/ci-fleet/issues/21), [#24](https://github.com/RandomDevelopment/ci-fleet/issues/24), [#27](https://github.com/RandomDevelopment/ci-fleet/issues/27), and [#30](https://github.com/RandomDevelopment/ci-fleet/issues/30).
