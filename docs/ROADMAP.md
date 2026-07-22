# Roadmap

## Phase 0: Discovery

Completed: initial read-only audit of two existing projects and their runner VMs.

Still required:

- organization runner-group access;
- remaining host update and firewall checks;
- monitoring and backup checks;
- privileged workflow separation decisions.

Exit condition: migration constraints are understood without changing working CI.

## Phase 1: Design decisions

- Select the initial controller implementation.
- Define the runner image boundary.
- Define the reusable workflow contract.
- Define runner groups and trust boundaries.
- Define credential provisioning.
- Define cleanup and disk-pressure policy.
- Define unattended update and drain behavior.
- Define external logging and monitoring requirements.

Exit condition: accepted architecture decisions and a reversible proof-of-concept plan.

## Phase 2: Isolated proof of concept

- Build one runner image.
- Deploy one test runner without modifying existing runners.
- Register it with a new experimental label.
- Add one manual, read-only smoke workflow.
- Use explicit `permissions: contents: read`.
- Verify success, failure, cancellation, timeout, cleanup, and runner replacement.
- Record disk usage before and after the job.
- Confirm that long-lived controller credentials are unavailable to the job.

Exit condition:

- the runner processes exactly one job;
- no job-owned container, network, volume, or workspace residue remains;
- existing CI remains unchanged and green;
- rollback requires removing only the experimental runner and workflow.

## Phase 3: Parallel project validation

- Add a project-owned test image and thin calling workflow to one existing project.
- Run old and new CI paths in parallel.
- Compare results, runtime, resource use, and residual state.
- Repeat for the second project.

Exit condition: both projects pass on the new fleet without removing the old path.

## Phase 4: Migration

- Move approved read-only CI to the shared organization runner group.
- Keep release, deployment, schema-writing, and internal-network jobs separated.
- Preserve rollback instructions.
- Retire project-specific runners only after an observation period.

## Phase 5: Distributed capacity

- Manage runner pools and controllers through the schema-v3 private desired-state contract.
- Enroll or adopt hosts with the idempotent worker-controller installer.
- Add a second independently provisioned Docker host.
- Verify identical deployment and recovery.
- Add demand-based scaling.
- Validate host draining, rolling updates, and failure handling.
- Increase same-host concurrency only after collision, disk, and cleanup tests pass.
