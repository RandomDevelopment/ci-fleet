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
one job, and destroys it afterward. Runners are **host-privileged**:
each job gets the host Docker socket, which is host-root-equivalent. A
compromised job can therefore reach everything on the host, including
host-local controller credentials — this is why only trusted private
repositories may ever be authorized, and why host-local identity files
stay root-owned. Jobs also receive short-lived registration
configuration and may receive `GITHUB_TOKEN` or explicitly configured
project secrets. Projects bring their own toolchains in their own containers.
Capacity (how many jobs run at once) lives in reviewed private Git
configuration, never in application workflows.

One controller on one Docker host is a complete fleet.

## Step 1: Install one controller

Runs on: split between a management workstation (Git and GitHub work)
and the fresh Linux Docker host (installation only). Changes state: yes.
Keep repository-writing credentials off the fleet host: anything with
write access retained there is reachable by later host-root-equivalent
jobs through the Docker socket.

1. Check the host qualifies: Docker, Compose v2, Git, Bash, outbound
   HTTPS to GitHub, no project workloads, and a declared failure
   boundary (disposable or recoverable). See
   [Adding a host](ADDING-A-HOST.md) sections 1-3. The installer itself
   additionally requires a systemd-based host with `python3`, `tar`,
   `install`, `flock`, and standard coreutils — it fails closed with a
   named missing command if one is absent.
2. Clone the public engine repository and check out the exact reviewed
   engine commit you intend to pin — later steps copy templates from it
   and run its installer as root, so it must be the reviewed commit, not
   a moving branch. The pin must be a commit merged into and reachable
   from the engine's public default branch; never pin an unmerged local
   branch tip.

   ```bash
   git clone https://github.com/RandomDevelopment/ci-fleet.git
   git -C ci-fleet checkout PINNED_ENGINE_COMMIT
   git -C ci-fleet merge-base --is-ancestor HEAD origin/main && echo merged
   ```

3. Create the GitHub App and runner group the controller will use. The
   App must be installed on the organization with organization-level
   **Self-hosted runners: Read and write** permission; the concrete
   bootstrap settings are in [Live pilot runbook](LIVE-PILOT.md)
   sections 2-3. Then place the host-local identity files (root-owned,
   mode `0600`) from the checked-out engine's templates as in
   [Adding a host](ADDING-A-HOST.md) section 5.
4. Create your private configuration repository from the public
   [configuration template](https://github.com/RandomDevelopment/ci-fleet-config-template)
   and initialize it completely — pass the runner group you just created,
   your controller ID, and the pinned engine commit, so no fictional
   `example-org`/`example-app` placeholders survive:

   ```bash
   ./scripts/init.sh \
     --organization YOUR-ORG \
     --project YOUR-APP \
     --controller YOUR-CONTROLLER-ID \
     --location primary-site \
     --runner-group YOUR-CREATED-RUNNER-GROUP \
     --capacity-budget 1 \
     --max-runners 1 \
     --engine-ref PINNED_ENGINE_COMMIT
   ```

   Then validate with `./scripts/validate.sh --strict`, open a pull
   request, and **merge it**: managed controller lifecycle is permitted
   only from a reviewed, merged private configuration commit. Resolve
   and record that merge commit SHA (`RESOLVED_CONFIG_COMMIT` below);
   do not install from an unmerged branch.
4. Give the host a way to read that private repository: either a
   narrowly scoped host-local read-only Git credential, or a pinned
   local checkout transferred from your management machine via a
   temporary Git bundle (the installer accepts a local checkout path as
   `--config-repo`). Without one of these, the installer's
   noninteractive fetch fails closed. If you use the local-checkout
   path, keep that checkout in place afterward — the recorded path is
   reused by scheduled drift checks — and remove only the bundle.
5. Apply the merged configuration from the reviewed engine checkout
   (`--install` for a fresh host; `--adopt` is only for converting an
   existing manually installed controller):

   ```bash
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

1. Confirm the repository you passed as `--project` to `init.sh` is in
   the pool's `allowed_repositories` (the initializer put it there; add
   it only if you skipped initialization), and validate with
   `./scripts/validate.sh --strict`.
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

1. Trigger one trivial one-step project job with `runs-on` set to the
   shared label, `permissions: contents: read` declared explicitly, and
   `timeout-minutes: 5`, so the job never inherits a read-write
   `GITHUB_TOKEN` default and cannot occupy the single runner past the
   ordinary-CI ceiling. Do not copy a nested runner image reference into
   the job: image tags derive from each controller's engine pin, and a
   workflow hard-coding one host's tag can be routed to a host where
   that image does not exist. (The older
   `examples/workflows/live-pilot.yml.example` targets the experimental
   label and the `:dev` runner image and is not a managed-install
   starter.)
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
