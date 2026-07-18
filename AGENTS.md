# Agent Instructions

## Purpose

This repository defines a portable, public, self-hosted GitHub Actions CI fleet and the mandatory contract used to migrate existing projects onto it. It must remain generic enough to run on Docker hosts located on virtual machines, physical computers, or VPS infrastructure.

## Current phase

The project is in architecture definition and isolated proof-of-concept planning. Do not create or alter production deployment paths until the design decisions are recorded and the isolated proof of concept is approved.

## Sources of truth

- `docs/PROJECT-STANDARD.md`: mandatory rules for participating projects.
- `docs/MIGRATING-EXISTING-CI.md`: required staged migration process.
- `docs/COMPLIANCE-CHECKLIST.md`: cutover gate.
- `docs/ARCHITECTURE.md`: fleet and trust boundaries.
- `docs/SECRETS.md`: credential boundaries.
- `docs/ADDING-A-HOST.md`: repeatable host and location enrollment.
- `docs/ADDING-A-PROJECT.md`: project authorization without host changes.

Agents modifying this repository or adapting a project MUST apply these documents. Do not create a project-specific exception silently. Record any necessary exception as an explicit design decision with risks and removal criteria.

## Required boundaries

- Keep original project code compatible with the Unlicense.
- Do not copy GPL-licensed implementation code into this repository.
- Preserve required notices for third-party code or substantial examples.
- Never commit credentials, registration tokens, private keys, real environment files, internal host inventories, private IP addresses, or unredacted infrastructure reports.
- Use examples and placeholders for organization-specific configuration.
- Store long-lived GitHub credentials only in a controller or external secret manager.
- Do not expose long-lived controller credentials to job runner containers.
- Prefer ephemeral, one-job runners.
- Treat every group of runners sharing one Docker daemon as one security boundary.
- Keep normal CI, release, deployment, repository-writing, and internal-network workloads separable.
- Make cleanup concurrency-aware and scoped to a workflow run, task, and shard.
- Keep ordinary task-matrix jobs at a five-minute hard timeout and expected test payload at four minutes or less.
- Preserve `fast` and `full` as aggregates, but schedule granular deterministic task shards on the fleet.
- Do not use unrestricted global Docker pruning as per-job cleanup.
- Pin production dependencies and container images to reviewed versions or digests.
- Do not remove or weaken existing required CI until parallel validation and rollback verification are complete.
- Do not install project language runtimes in the generic runner image.
- Do not put repository names, project allowlists, or project-specific logic in runner images or host configuration.
- Keep project workflows independent of host, site, instance, and scale-set names; route through shared capability labels.
- Keep worker capacity in reviewed private infrastructure configuration. Application workflows submit all independent jobs and must not use `max-parallel` to model fleet size.
- Treat schema-v3 controller desired state as authoritative for routing, capacity, lifecycle, and engine revision; host-local files contain only rendered state and credentials.
- Do not allow projects to select privileged runner groups through untrusted inputs.

## Verification expectations

Runnable changes must eventually include:

- formatting and static validation;
- container image build verification;
- secret scanning;
- configuration validation;
- an isolated end-to-end runner test;
- cleanup verification after success, failure, cancellation, and timeout;
- proof that long-lived controller credentials are unavailable to jobs;
- proof that ordinary CI uses read-only permissions;
- task-plan validation and deterministic matrix expansion;
- measured shard duration and five-minute timeout enforcement;
- comparison against the existing CI path during migration.

## Change policy

Keep changes small and reversible. Existing project CI must continue operating until the new fleet path has passed parallel validation, completed the compliance checklist, and has an explicit tested rollback procedure.
