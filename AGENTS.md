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
- Make cleanup concurrency-aware and scoped to a workflow run.
- Do not use unrestricted global Docker pruning as per-job cleanup.
- Pin production dependencies and container images to reviewed versions or digests.
- Do not remove or weaken existing required CI until parallel validation and rollback verification are complete.
- Do not install project language runtimes in the generic runner image.
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
- comparison against the existing CI path during migration.

## Change policy

Keep changes small and reversible. Existing project CI must continue operating until the new fleet path has passed parallel validation, completed the compliance checklist, and has an explicit tested rollback procedure.
