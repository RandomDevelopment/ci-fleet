# Post-pilot capacity promotion

Use this procedure only after the strict one-runner pilot has passed. It validates and changes one already isolated controller; it does not authorize a project workflow by itself.

`scripts/preflight.sh` remains the initial pilot gate and accepts only `MIN=0`, `MAX=1`. `scripts/capacity-preflight.sh` is a separate, read-only post-pilot gate. It never edits host configuration.

## Capacity policy

Declare the policy before observing a larger workload:

- requested MAX is explicit, positive, and bounded;
- `MIN` remains zero;
- the configured instance, scale set, routing label, runner group, Docker socket group, and per-runner limits must equal the running controller's effective values;
- no managed runner, project job resource, unrelated running container, controller OOM, or current-boot kernel OOM evidence may exist;
- Docker filesystem use must be below `CI_FLEET_DISK_WARN_PERCENT` (80 by default);
- reserve the controller's Compose limit: 1 CPU and 512 MiB;
- reserve 1 CPU and 1 GiB for Docker overhead;
- reserve for the operating system the greater of 1 CPU or 15% of logical CPU capacity;
- reserve for the operating system the greater of 2 GiB or 20% of physical memory;
- admit `target MAX × CI_FLEET_RUNNER_CPUS` and `target MAX × CI_FLEET_RUNNER_MEMORY_MIB` only when those allocations plus all reserves fit;
- require currently available memory to cover all target runners plus controller and Docker reservations.

Runner limits are controller admission inputs. Project containers use the host Docker daemon as siblings of the runner container, so the separately authorized live proof must still observe whole-host CPU, memory, disk, collision, and cleanup behavior.

During a live proof, retain the target only if every five-second sample keeps CPU busy below 85%, available memory at or above the greater of 2 GiB or 20% of total memory, and Docker filesystem use below 80%. Any OOM, unrelated workload, controller/Docker failure, third runner, observer gap, or cleanup residue requires restoration.

## 1. Verify idle pilot state

Gate all dispatches that can target this controller. Confirm no queued, assigned, or running fleet job and no instance-owned runner in any state. Keep the dispatch gate closed until post-change verification completes.

From the reviewed checkout, load the root-only host configuration without tracing or printing it:

```bash
set -Eeuo pipefail
set +x
cd /opt/ci-fleet
set -a
. /etc/ci-fleet/ci-fleet.env
set +a
scripts/preflight.sh
```

Require exactly `PREFLIGHT_OK warnings=0`. This proves the original `MIN=0`, `MAX=1` pilot contract remains valid.

## 2. Validate target capacity without changing it

```bash
scripts/capacity-preflight.sh --phase pre-change --target-max 2
```

Require `CAPACITY_PREFLIGHT_OK phase=pre-change target_max=2 configured_max=1 effective_max=1`. Record only the safe budget summaries.

## 3. Create one protected backup

Use one UTC timestamp and refuse to overwrite an existing path:

```bash
backup_dir=/etc/ci-fleet/backups
stamp=$(date -u +%Y%m%dT%H%M%SZ)
backup="$backup_dir/ci-fleet.env.before-max2.$stamp"
install -d -o root -g root -m 0700 "$backup_dir"
test ! -e "$backup"
install -o root -g root -m 0600 /etc/ci-fleet/ci-fleet.env "$backup"
cmp -s /etc/ci-fleet/ci-fleet.env "$backup"
sha256sum "$backup" | cut -d' ' -f1
```

Record the path and checksum only. Never print or copy the backup contents, and do not delete the backup during the proof.

## 4. Change one exact setting

Before editing, require exactly one active assignment and the pilot value:

```bash
test "$(grep -c '^CI_FLEET_MAX_RUNNERS=' /etc/ci-fleet/ci-fleet.env)" -eq 1
grep -qx 'CI_FLEET_MAX_RUNNERS=1' /etc/ci-fleet/ci-fleet.env
```

Edit only that line to `CI_FLEET_MAX_RUNNERS=2`. Then verify without printing a diff:

```bash
test "$(stat -c '%u:%g:%a' /etc/ci-fleet/ci-fleet.env)" = 0:0:600
test "$(grep -c '^CI_FLEET_MAX_RUNNERS=' /etc/ci-fleet/ci-fleet.env)" -eq 1
grep -qx 'CI_FLEET_MAX_RUNNERS=2' /etc/ci-fleet/ci-fleet.env
cmp -s \
  <(grep -v '^CI_FLEET_MAX_RUNNERS=' "$backup") \
  <(grep -v '^CI_FLEET_MAX_RUNNERS=' /etc/ci-fleet/ci-fleet.env)
env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin HOME=/root \
  docker compose --env-file /etc/ci-fleet/ci-fleet.env -f deploy/compose.yaml config --quiet
```

Do not run the pilot preflight against MAX=2; it must continue to reject that value. Every Compose command below uses `env -i` so stale values exported when the pilot file was sourced cannot override the explicit `--env-file` during promotion or rollback.

## 5. Recreate only the controller

Reconfirm zero runners and zero jobs immediately before stopping. Stop only the controller with enough grace for scale-set deletion:

```bash
env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin HOME=/root \
  docker compose --env-file /etc/ci-fleet/ci-fleet.env -f deploy/compose.yaml stop -t 60 controller
```

Verify the exact old scale set is absent and there is no runner before starting the replacement. Do not delete an apparent duplicate until ownership and zero active jobs are proven.

```bash
env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin HOME=/root \
  docker compose --env-file /etc/ci-fleet/ci-fleet.env -f deploy/compose.yaml \
  up -d --no-deps --force-recreate --timeout 60 controller
```

Do not recreate `runner-image`, remove volumes, rebuild images, or touch unrelated resources.

## 6. Verify effective state

Wait boundedly for one sanitized `controller ready` record reporting `minRunners=0` and `maxRunners=2`. Require one running controller, restart count zero, one intended scale set, the unchanged experimental routing label and runner group, and no idle runner.

Reload the host configuration and run:

```bash
scripts/capacity-preflight.sh --phase post-change --target-max 2
scripts/healthcheck.sh
```

Require both to pass. The post-change preflight compares configured state with a filtered set of effective controller values and never prints arbitrary container environment entries.

## 7. Run one separately authorized proof

Start bounded runner, task-job, project-resource, and host-metric observers before dispatch. Dispatch exactly the approved workload once. Do not retry a failed proof and do not raise MAX above two.

Observe runner creation/destruction, actual two-way overlap, no third runner, whole-host CPU/memory/disk thresholds, Docker/controller health, exact project identity, and automatic cleanup. Run the instance-scoped cleanup dry-run and healthcheck after all jobs terminate.

## 8. Retain or restore (rollback)

Retain MAX=2 only when the authorized workload succeeds, actual two-way job and runner overlap is proven, every predeclared resource threshold passes, no manual cleanup is required, all runner/project residue is zero, post-change capacity preflight passes, cleanup dry-run is empty, and healthcheck passes.

On any failure, keep dispatch gated, wait for exact job termination, and restore the backup:

```bash
env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin HOME=/root \
  docker compose --env-file /etc/ci-fleet/ci-fleet.env -f deploy/compose.yaml stop -t 60 controller
install -o root -g root -m 0600 "$backup" /etc/ci-fleet/ci-fleet.env
cmp -s "$backup" /etc/ci-fleet/ci-fleet.env
env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin HOME=/root \
  docker compose --env-file /etc/ci-fleet/ci-fleet.env -f deploy/compose.yaml config --quiet
env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin HOME=/root \
  docker compose --env-file /etc/ci-fleet/ci-fleet.env -f deploy/compose.yaml \
  up -d --no-deps --force-recreate --timeout 60 controller
```

Verify a sanitized ready record with `MIN=0`, `MAX=1`, then run `scripts/preflight.sh`, `scripts/healthcheck.sh`, and the instance-scoped cleanup dry-run. Require zero runners, zero jobs, one intended scale set, no duplicate controller, and no residue before reopening dispatch.
