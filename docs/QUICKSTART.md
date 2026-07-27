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
one job, and destroys it afterward. Runners are secret-free and
unprivileged; projects bring their own toolchains in their own
containers. Capacity (how many jobs run at once) lives in reviewed
private Git configuration, never in application workflows.

One controller on one Docker host is a complete fleet.

## Step 1: Install one controller

Runs on: a fresh Linux Docker host. Changes state: yes.

1. Check the host qualifies: Docker, Compose v2, Git, Bash, outbound
   HTTPS to GitHub, no project workloads, and a declared failure
   boundary (disposable or recoverable). See
   [Adding a host](ADDING-A-HOST.md) sections 1-3.
2. Create the GitHub App and runner group the controller will use, and
   place the host-local identity files (root-owned, mode `0600`) as in
   [Adding a host](ADDING-A-HOST.md) section 5.
3. Declare one `controllers` entry in your private configuration
   repository (created from the public
   [configuration template](https://github.com/RandomDevelopment/ci-fleet-config-template))
   with `min_runners: 0`, `max_runners: 1`, and a pinned engine commit.
4. Apply it:

   ```bash
   sudo ./scripts/install-worker-controller.sh \
     --adopt \
     --config-repo OWNER/PRIVATE-CONFIG-REPO \
     --ref RESOLVED_CONFIG_COMMIT \
     --controller YOUR-CONTROLLER-ID
   ```

5. Verify: the same command with `--check` reports `CHECK_OK`, and the
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

Runs on: GitHub. Changes state: no (ordinary CI only).

1. Trigger one trivial job — the fleet canary's
   `Fleet concurrency canary` workflow with count `1`, or a one-step
   project job — with `runs-on` set to the shared label.
2. Verify from the job's metadata: it ran on your scale set
   (runner name prefix matches the scale-set name), succeeded, and the
   ephemeral runner disappeared afterward.
3. Verify the controller returned to zero idle runners.

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
