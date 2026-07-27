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
one job, and destroys it afterward. Jobs never receive the controller's
long-lived credential, but they can receive short-lived registration
configuration, `GITHUB_TOKEN`, and explicitly configured project secrets.
They are also **host-privileged**: each job gets the host Docker socket,
which is host-root-equivalent, so only trusted private repositories may
ever be authorized. Projects bring their own toolchains in their own containers.
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
3. Create the private configuration repository from the public
   [configuration template](https://github.com/RandomDevelopment/ci-fleet-config-template),
   then run its `scripts/init.sh` as described in that repository's README.
   Review the complete generated `fleet.json` and replace every fictional
   organization, project, runner-group, and controller value; editing only
   `controllers` leaves unsafe example mappings behind. Start with
   `min_runners: 0`, `max_runners: 1`, and a pinned engine commit.
4. Give the host a way to read that private repository: either a
   narrowly scoped host-local read-only Git credential, or a temporary
   pinned Git bundle / local checkout transferred from your management
   machine (the installer accepts a local checkout path as
   `--config-repo`). Without one of these, the installer's
   noninteractive fetch fails closed.
5. Check out the engine at the exact reviewed commit declared by the
   private configuration and verify `git rev-parse HEAD` matches it. Do not
   run the root installer from an unreviewed checkout or moving branch.
6. Apply it (`--install` for a fresh host; `--adopt` is only for
   converting an existing manually installed controller):

   ```bash
   sudo ./scripts/install-worker-controller.sh \
     --install \
     --config-repo OWNER/PRIVATE-CONFIG-REPO \
     --ref RESOLVED_CONFIG_COMMIT \
     --controller YOUR-CONTROLLER-ID
   ```

7. Verify: the same command with `--check` reports `CHECK_OK`, and the
   GitHub runner group shows the scale set idle at zero runners.

## Step 2: Connect one repository

Runs on: GitHub + the project repository + private configuration.
Changes state: yes, but no host changes — onboarding never touches a
fleet host.

1. Add the repository to the pool's `allowed_repositories` in private
   configuration and validate with `./scripts/validate.sh --strict`.
2. Authorize the repository in the GitHub runner group.
3. Copy `examples/workflows/live-pilot.yml.example` from the reviewed
   engine checkout into the project, keep its explicit read-only
   `permissions`, and set `runs-on` to the pool's shared routing label (for
   example `docker-ci`). Do not rely on the repository's default token
   permissions.

Full contract: [Adding a project](ADDING-A-PROJECT.md).

## Step 3: Run and verify one job

Dispatched through: GitHub. Job steps run inside the ephemeral runner on
the fleet Docker host with host-root-equivalent socket access. Changes
state: yes — the controller creates a runner container and the proof job
creates scoped Docker resources, all of which must be removed afterward.

1. Dispatch the copied first-job lifecycle proof once with `runs-on` set
   to the shared label.
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
