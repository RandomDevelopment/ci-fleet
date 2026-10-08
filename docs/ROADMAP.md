# Roadmap

Current phase: controlled migration. The isolated one-job pilot passed on 2026-07-15, and reviewed schema-v3 desired state is accepted for managed ordinary-CI controller lifecycle on isolated fleet hosts. [Accepted design decisions](DESIGN-DECISIONS.md) define the authorized scope and remaining gates. This roadmap does not establish current host state or completion of downstream migrations.

## Phase 0: Discovery

Completed: initial read-only audit of two existing projects and their runner VMs.

Before each migration, verify:

- selected-repository runner-group authorization;
- remaining host update and firewall checks;
- monitoring and backup checks;
- privileged workflow separation decisions.

Exit condition: migration constraints are understood without changing working CI.

## Phase 1: Design decisions

Accepted for the isolated pilot and managed ordinary-CI controller lifecycle. Production deployment and privileged delivery remain separately gated.

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

Completed on 2026-07-15. [Issue #7](https://github.com/RandomDevelopment/ci-fleet/issues/7) records the read-only one-job lifecycle proof, scoped cleanup, zero final job residue, controller health, and preservation of existing project runners.

The proof-of-concept checklist was:

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
- existing CI remains unchanged and its checks pass;
- rollback requires removing only the experimental runner and workflow.

## Phase 3: Parallel project validation

Current migration gate, assessed separately for each project through the [migration guide](MIGRATING-EXISTING-CI.md) and [compliance checklist](COMPLIANCE-CHECKLIST.md).

- Add a project-owned test image and thin calling workflow to one existing project.
- Run old and new CI paths in parallel.
- Compare results, runtime, resource use, and residual state.
- Repeat for the second project.

Exit condition: each participating project passes on the new fleet, completes its compliance checklist, and verifies rollback without removing the old path.

## Phase 4: Migration

- Move approved read-only CI to the shared organization runner group.
- Keep release, deployment, schema-writing, and internal-network jobs separated.
- Preserve rollback instructions.
- Retire project-specific runners only after an observation period.

## Phase 5: Distributed capacity

The schema-v3 contract and installer are available for reviewed ordinary-CI adoption. Additional hosts and capacity increases require their own evidence and approval.

- Manage runner pools and controllers through the schema-v3 private desired-state contract.
- Enroll or adopt hosts with the idempotent worker-controller installer.
- Add a second independently provisioned Docker host.
- Verify identical deployment and recovery.
- Add demand-based scaling.
- Validate host draining, rolling updates, and failure handling.
- Increase same-host concurrency only after collision, disk, and cleanup tests pass.

## Tester, deployer, and production readiness

Tester and deployer components remain under review. Each role needs separate credentials, services, trust boundaries, validation, and rollback evidence. The completed CI pilot does not authorize production deployment or privileged delivery on ordinary-CI runners. Production readiness remains gated by per-project migration evidence and separate approval for production paths.
