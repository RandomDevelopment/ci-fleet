# Post-pilot capacity promotion

Use this procedure only after the strict one-runner pilot has passed. It validates and changes one already isolated controller; it does not authorize a project workflow by itself.

Schema-v3 private desired state owns capacity. Never edit `/etc/ci-fleet/ci-fleet.env` directly: it is rendered state and the drift checker will replace local changes.

## Capacity policy

Declare the policy before observing a larger workload:

- requested MAX is the exact reviewed controller `max_runners`;
- `MIN` remains zero;
- both the controller `max_runners` and pool `capacity_budget` change in one reviewed private configuration PR;
- the configured instance, scale set, routing label, runner group, Docker socket group, and per-runner limits remain unchanged;
- no managed runner, project job resource, unrelated running container, controller OOM, or current-boot kernel OOM evidence may exist;
- Docker filesystem use stays below `CI_FLEET_DISK_WARN_PERCENT` (80 by default);
- reserve 1 CPU and 512 MiB for the controller, 1 CPU and 1 GiB for Docker overhead, and the greater of 1 CPU/15% plus 2 GiB/20% for the operating system;
- admit `target MAX × CI_FLEET_RUNNER_CPUS` and `target MAX × CI_FLEET_RUNNER_MEMORY_MIB` only when the allocation and reserves fit; and
- require currently available memory to cover all target runners plus controller and Docker reservations.

Project containers use the host Docker daemon as siblings of the runner container. The separately authorized live proof must therefore observe whole-host CPU, memory, disk, collisions, and cleanup.

During that proof, retain the requested MAX only if every five-second sample keeps CPU busy below 85%, available memory at or above the greater of 2 GiB or 20% of total memory, and Docker filesystem use below 80%. Any OOM, unrelated workload, controller/Docker failure, runner above the requested maximum, observer gap, or cleanup residue requires restoration.

## 1. Gate dispatch and verify the current state

Block dispatches that can target this controller. Confirm no queued, assigned, or running fleet job and no instance-owned runner in any state. Keep dispatch gated through post-change verification.

Run the current installed health check from a clean process. Require the configured and effective current MAX to match, controller health, and an empty instance-scoped cleanup dry-run. The target-capacity check in the next step replaces the pilot-only preflight for later increases.

## 2. Validate target capacity without changing it

From the installed reviewed release, run:

```bash
TARGET_MAX=REVIEWED_CONTROLLER_MAX
sudo env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin HOME=/root bash -c '
  set -a
  . /etc/ci-fleet/ci-fleet.env
  set +a
  /opt/ci-fleet/current/scripts/capacity-preflight.sh --phase pre-change --target-max "$1"
' _ "$TARGET_MAX"
```

Require:

```text
CAPACITY_PREFLIGHT_OK phase=pre-change target_max=REVIEWED_CONTROLLER_MAX configured_max=PREVIOUS_MAX effective_max=PREVIOUS_MAX
```

Record only the safe budget summary.

## 3. Review and merge private desired state

In the secret-free private configuration repository:

1. raise only the selected controller's `max_runners` to the reviewed target;
2. raise its pool `capacity_budget` only as needed to admit that reviewed controller maximum;
3. keep `min_runners`, runner resources, identity, lifecycle, routing, trust, and engine pin unchanged;
4. run the complete strict validator, policy tests, and committed-secret scan;
5. obtain the repository's configured exact-head review and CI gates; and
6. merge normally.

Record the previous and new full private configuration commits. Do not edit a rendered host file or bypass the desired-state PR.

## 4. Apply the exact merged configuration

Reconfirm the idle gates, then apply the exact merged private commit through the installed manager:

```bash
sudo /opt/ci-fleet/manager/current/scripts/install-worker-controller.sh \
  --upgrade \
  --config-repo ORGANIZATION/PRIVATE-CONFIGURATION \
  --ref NEW_PRIVATE_CONFIGURATION_COMMIT \
  --controller CONTROLLER_ID
```

The installer must create a protected checkpoint, drain the selected instance, validate the managed target, change only the selected controller, start it, and pass health before reporting convergence. A failed activation must restore the checkpoint and keep dispatch closed.

Do not recreate runner jobs, remove volumes, or touch unrelated Docker resources.

## 5. Verify effective state

Require one running controller, restart count zero, one intended scale set, the unchanged routing label and runner group, no idle runner, and the exact reviewed target MAX.

Run:

```bash
TARGET_MAX=REVIEWED_CONTROLLER_MAX
sudo env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin HOME=/root bash -c '
  set -a
  . /etc/ci-fleet/ci-fleet.env
  set +a
  /opt/ci-fleet/current/scripts/capacity-preflight.sh --phase post-change --target-max "$1"
  /opt/ci-fleet/current/scripts/healthcheck.sh
' _ "$TARGET_MAX"
```

Require both to pass, plus a clean desired-state `--check` and empty instance-scoped cleanup dry-run.

## 6. Run one separately authorized proof

Start bounded runner, task-job, project-resource, and host-metric observers before dispatch. Dispatch exactly the approved workload once. Do not retry a failed proof or exceed the reviewed target MAX.

Observe runner creation/destruction, continuous target-way overlap, no runner above the requested maximum, whole-host resource thresholds, Docker/controller health, exact project identity, and automatic cleanup. A workload that never drives the full reviewed target does not qualify. Repeat health, drift, and cleanup dry-run checks after all jobs terminate.

## 7. Retain or restore

Retain the requested MAX only when every predeclared gate passes and no manual cleanup is required.

On any failure, keep dispatch gated and apply the recorded previous private configuration commit through the same reviewed `--upgrade` path:

```bash
sudo /opt/ci-fleet/manager/current/scripts/install-worker-controller.sh \
  --upgrade \
  --config-repo ORGANIZATION/PRIVATE-CONFIGURATION \
  --ref PREVIOUS_PRIVATE_CONFIGURATION_COMMIT \
  --controller CONTROLLER_ID
```

Require the restored PREVIOUS_MAX, clean desired-state check, health pass, zero runners/jobs/residue, one intended scale set, and empty instance-scoped cleanup dry-run before reopening dispatch. Preserve installer checkpoint and proof evidence without recording credential values.
