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
2. On the **fleet host**, clone the public engine repository and check
   out the exact reviewed engine commit you intend to pin — later steps
   copy templates from it and run its installer as root, so it must be
   the reviewed commit, not a moving branch. The pin must be a commit
   merged into and reachable from the engine's public default branch;
   never pin an unmerged local branch tip. Cloning the public engine
   needs no repository-writing credential.

   ```bash
   git clone https://github.com/RandomDevelopment/ci-fleet.git
   git -C ci-fleet checkout PINNED_ENGINE_COMMIT
   git -C ci-fleet merge-base --is-ancestor HEAD origin/main && echo merged
   ```

3. On the **management workstation** (GitHub web UI), create the GitHub
   App and runner group the controller will use. The App must be
   installed on the organization with organization-level
   **Self-hosted runners: Read and write** permission; the concrete
   bootstrap settings are in [Live pilot runbook](LIVE-PILOT.md)
   sections 2-3. Then, on the **fleet host**, place the host-local
   identity files (root-owned, mode `0600`) from the checked-out
   engine's templates as in [Adding a host](ADDING-A-HOST.md) section 5.
4. On the **management workstation**, create your private configuration
   repository from the configuration template **versioned inside the
   pinned engine checkout** — the schema, initializer, and validator the
   pinned engine actually enforces live under its
   `templates/config-repository`, while the standalone public template
   may have advanced past your engine pin:

   On the management workstation, clone the same pinned engine commit
   (the previous clone lives on the fleet host), then copy its vendored
   template:

   ```bash
   git clone -q https://github.com/RandomDevelopment/ci-fleet.git
   git -C ci-fleet checkout PINNED_ENGINE_COMMIT
   cp -r ci-fleet/templates/config-repository my-fleet-config
   cd my-fleet-config
   ./scripts/init.sh \
     --organization YOUR-ORG \
     --project YOUR-APP-SLUG \
     --repository YOUR-ORG/ACTUAL-REPOSITORY-NAME \
     --controller YOUR-CONTROLLER-ID \
     --location primary-site \
     --runner-group YOUR-CREATED-RUNNER-GROUP \
     --capacity-budget 1 \
     --max-runners 1 \
     --engine-ref PINNED_ENGINE_COMMIT
   ```

   `--project` is a logical lowercase slug; pass the real GitHub
   repository name separately with `--repository` so
   `allowed_repositories` names a repository that exists. Commit the
   initialized result, validate, push to a new **private** GitHub
   repository, open a pull request, and **merge it**:

   ```bash
   git init -q && git add -A && git commit -qm "Initialize fleet configuration"
   ./scripts/validate.sh --strict
   ```

   The commit must contain the initialized `fleet.json`; validating the
   working tree without committing it would push the untouched example
   configuration. Managed controller managed controller
   lifecycle is permitted only from a reviewed, merged private
   configuration commit. Resolve and record that merge commit SHA
   (`RESOLVED_CONFIG_COMMIT` below); do not install from an unmerged
   branch.
5. Give the fleet host a way to read that private repository **as
   root**: the installer fetches under `sudo` and the recurring drift
   check runs as `User=root`, so a credential in an operator's
   user-scoped Git configuration is invisible to both. Either configure
   a narrowly scoped read-only credential in root's Git configuration,
   or transfer a pinned local checkout from your management workstation
   via a temporary Git bundle (the installer accepts a local checkout
   path as `--config-repo`). Without one of these, the noninteractive
   fetch fails closed. If you use the local-checkout path, keep that
   checkout in place afterward — the recorded path is reused by
   scheduled drift checks — and remove only the bundle.
6. On the **fleet host**, apply the merged configuration from the
   reviewed engine checkout (`--install` for a fresh host; `--adopt` is
   only for converting an existing manually installed controller):

   ```bash
   sudo ci-fleet/scripts/install-worker-controller.sh \
     --install \
     --config-repo OWNER/PRIVATE-CONFIG-REPO \
     --ref RESOLVED_CONFIG_COMMIT \
     --controller YOUR-CONTROLLER-ID
   ```

7. Verify: the same command with `--check` reports `CHECK_OK`, and the
   GitHub runner group shows the scale set idle at zero runners.

## Step 2: Connect one repository

Runs on: management workstation (GitHub + the project repository +
private configuration). Changes state: yes, but no host changes —
onboarding never touches a fleet host.

1. Stage the proof workflow first: commit the dispatch-only job below
   to the project repository as `.github/workflows/fleet-proof.yml`.
   Do this **before** authorization so the first eligible job is the
   controlled proof, never a push-triggered or previously queued job.

   ```yaml
   name: Fleet first-job proof
   on:
     workflow_dispatch:
   permissions:
     contents: read
   jobs:
     proof:
       runs-on: docker-ci   # your pool's shared routing label
       timeout-minutes: 5
       steps:
         - run: echo "fleet proof on ${RUNNER_NAME:-unknown}"
   ```
2. Confirm the repository you passed as `--repository` to `init.sh` is
   in the pool's `allowed_repositories` (the initializer put it there;
   add it only if you skipped initialization), and validate with
   `./scripts/validate.sh --strict`.
3. Authorize the repository in the GitHub runner group.

Full contract: [Adding a project](ADDING-A-PROJECT.md).

## Step 3: Run and verify one job

Runs on: dispatched from GitHub, but steps execute inside an ephemeral
runner on your fleet host with host-root-equivalent Docker access.
Changes state: yes, transiently — the job creates a runner container and
may create Docker containers, networks, and volumes; the proof below
verifies all of it is cleaned up.

1. Dispatch the staged proof workflow. Its explicit
   `permissions: contents: read` and `timeout-minutes: 5` keep the job
   from inheriting a read-write `GITHUB_TOKEN` default or occupying the
   single runner past the ordinary-CI ceiling. Do not copy a nested
   runner image reference into a job: image tags derive from each
   controller's engine pin, and a workflow hard-coding one host's tag
   can be routed to a host where that image does not exist. (The older
   `examples/workflows/live-pilot.yml.example` targets the experimental
   label and the `:dev` runner image and is not a managed-install
   starter.)
2. Verify the job ran on your fleet: the Actions job metadata names the
   runner, and the controller host's logs show that runner was created
   by your scale set. (Runner names derive from the controller ID, not
   necessarily the scale-set name, so use controller or scale-set
   evidence rather than a name-prefix guess.)
3. On the fleet host, prove cleanup and health explicitly — job success
   plus a returned-to-zero runner count alone does not prove the host
   is clean:

   ```bash
   # No ephemeral runner containers remain (the long-lived controller
   # container is also fleet-managed, so filter by kind=runner):
   sudo docker ps -aq \
     --filter label=io.randomdevelopment.ci-fleet.managed=true \
     --filter label=io.randomdevelopment.ci-fleet.kind=runner
   # No run-owned project resources remain. Compliant jobs name Compose
   # projects ci-<repo>-<run-id>-<attempt>-<task>-<shard>:
   sudo docker ps -aq --filter "name=^ci-"
   sudo docker network ls -q --filter "name=^ci-"
   sudo docker volume ls -q --filter "name=^ci-"
   # Run a fresh health evaluation, then check installed state:
   sudo systemctl start ci-fleet-health.service
   sudo systemctl is-failed ci-fleet-health.service  # expect: inactive/failed must NOT be failed
   sudo ci-fleet/scripts/install-worker-controller.sh \
     --check --config-repo OWNER/PRIVATE-CONFIG-REPO \
     --ref RESOLVED_CONFIG_COMMIT --controller YOUR-CONTROLLER-ID
   ```

   The health service is a timer-driven oneshot — `systemctl status`
   alone only shows its previous run, so start it explicitly after the
   proof job. Every resource listing above must be empty and the check
   must report `CHECK_OK`. The [Live pilot runbook](LIVE-PILOT.md)
   documents the full isolated first-job proof including read-only job
   permissions.
4. Verify the controller returned to zero idle runners in the GitHub
   runner group.

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
