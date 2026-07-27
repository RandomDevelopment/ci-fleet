# Quickstart: your first fleet job

This is the beginner path. It takes you from zero to one verified job on
your own ephemeral CI fleet, then points at advanced material. Every step
states where it runs and whether it changes anything.

Advanced topics — schema-v3 internals, capacity promotion, health
monitoring, multi-site fleets — are linked at the end. You do not need
them for this path.

## Step 0: What ci-fleet does

ci-fleet runs your GitHub Actions jobs on your own Docker hosts using
**ephemeral** runners: a small controller service on each host watches
GitHub for queued jobs, starts a throwaway runner container for exactly
one job, and destroys it afterward. Long-lived controller credentials
are never exposed to jobs — but runners do receive short-lived
registration configuration, and a job can receive `GITHUB_TOKEN` or
explicitly configured project secrets. Runners are also
**host-privileged**: each job gets the host Docker socket, which is
host-root-equivalent, so only trusted private repositories may ever be
authorized. Projects bring their own toolchains in their own containers.
Capacity (how many jobs run at once) lives in reviewed private Git
configuration, never in application workflows.

One controller on one Docker host is a complete fleet.

## Step 1: Install one controller

Runs on: a fresh Linux Docker host. Changes state: yes.

1. Check the host qualifies: Docker, Compose v2, Git, Bash, outbound
   HTTPS to GitHub, no project workloads, and a declared failure
   boundary (disposable or recoverable). See
   [Adding a host](ADDING-A-HOST.md) sections 1-3. The installer itself
   additionally requires a systemd-based host with `python3`, `tar`,
   `install`, `flock`, and standard coreutils — it fails closed with a
   named missing command if one is absent.
2. Create the GitHub App and runner group the controller will use. The
   App must be installed on the organization with organization-level
   **Self-hosted runners: Read and write** permission; the concrete
   bootstrap settings are in [Live pilot runbook](LIVE-PILOT.md)
   sections 2-3. Then place the host-local identity files (root-owned,
   mode `0600`) as in [Adding a host](ADDING-A-HOST.md) section 5.
3. Create your private configuration repository from the public
   [configuration template](https://github.com/RandomDevelopment/ci-fleet-config-template)
   and initialize it completely: run its `./scripts/init.sh` so the
   fictional `example-org`/`example-app` placeholders are replaced, then
   declare one `controllers` entry with `min_runners: 0`,
   `max_runners: 1`, and a pinned engine commit. Validate with
   `./scripts/validate.sh --strict` before continuing.
4. Give the host a way to read that private repository: either a
   narrowly scoped host-local read-only Git credential, or a temporary
   pinned Git bundle / local checkout transferred from your management
   machine (the installer accepts a local checkout path as
   `--config-repo`). Without one of these, the installer's
   noninteractive fetch fails closed.
5. Clone the public engine repository on the host and check out the
   exact reviewed engine commit your private configuration pins — the
   installer uses code and validators from that checkout and runs as
   root, so it must be the reviewed commit, not a moving branch. Then
   apply it (`--install` for a fresh host; `--adopt` is only for
   converting an existing manually installed controller):

   ```bash
   git clone https://github.com/RandomDevelopment/ci-fleet.git
   git -C ci-fleet checkout PINNED_ENGINE_COMMIT
   sudo ci-fleet/scripts/install-worker-controller.sh \
     --install \
     --config-repo OWNER/PRIVATE-CONFIG-REPO \
     --ref RESOLVED_CONFIG_COMMIT \
     --controller YOUR-CONTROLLER-ID
   ```

6. Verify: the same command with `--check` reports `CHECK_OK`, and the
   GitHub runner group shows the scale set idle at zero runners.

## Step 2: Connect one repository

Runs on: GitHub + the project repository + private configuration.
Changes state: yes, but no host changes — onboarding never touches a
fleet host.

1. Add the repository to the pool's `allowed_repositories` in private
   configuration and validate with `./scripts/validate.sh --strict`.
2. Authorize the repository in the GitHub runner group.
3. Give the project a workflow whose jobs use the pool's shared routing
   label (for example `runs-on: docker-ci`).

Full contract: [Adding a project](ADDING-A-PROJECT.md).

## Step 3: Run and verify one job

Runs on: dispatched from GitHub, but steps execute inside an ephemeral
runner on your fleet host with host-root-equivalent Docker access.
Changes state: yes, transiently — the job creates a runner container and
may create Docker containers, networks, and volumes; the proof below
verifies all of it is cleaned up.

1. Trigger one trivial job with `runs-on` set to the shared label —
   either the starter example in this repository at
   `examples/workflows/live-pilot.yml.example`, or a one-step project
   job. Declare `permissions: contents: read` (or another explicit
   minimal set) on the workflow so the job never inherits a read-write
   `GITHUB_TOKEN` default.
2. Verify the job ran on your fleet: the Actions job metadata names the
   runner, and the controller host's logs show that runner was created
   by your scale set. (Runner names derive from the controller ID, not
   necessarily the scale-set name, so use controller or scale-set
   evidence rather than a name-prefix guess.)
3. Complete the isolated first-job proof from
   [Live pilot runbook](LIVE-PILOT.md): read-only job permissions,
   controller health, scoped cleanup, and zero remaining job-owned
   containers, networks, or volumes. Job success plus a returned-to-zero
   runner count alone does not prove the host is clean.
4. Verify the controller returned to zero idle runners.

If the job stays queued: compare the job's complete `runs-on` expression
against the scale set's configured routing label, then check runner-group
repository authorization. A label or group mismatch is a configuration
bug, not a capacity problem.

## Step 4: Next steps (advanced)

- [Git-authored controller desired state](DESIRED-STATE.md) — the
  schema-v3 model, lifecycle states, checkpoints, and rollback.
- [Capacity promotion](CAPACITY-PROMOTION.md) — raising
  `max_runners`/budgets safely.
- [Fleet health monitoring](HEALTH-MONITORING.md) — heartbeats and
  missed-heartbeat detection.
- [Migrating existing CI](MIGRATING-EXISTING-CI.md) — moving a real
  project's test suite onto the fleet.
- [Architecture](ARCHITECTURE.md) and
  [Project CI standard](PROJECT-STANDARD.md) — the full contract.
