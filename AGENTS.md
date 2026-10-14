# Agent Instructions

## Purpose

This repository defines a portable, public, self-hosted GitHub Actions CI fleet and the mandatory contract used to migrate existing projects onto it. It must remain generic enough to run on Docker hosts located on virtual machines, physical computers, or VPS infrastructure.

## Current phase

The isolated proof of concept is approved and the project is in controlled migration. The accepted scope and remaining gates are recorded in `docs/DESIGN-DECISIONS.md`. Managed ordinary-CI controller lifecycle is permitted only through reviewed schema-v3 desired state; production deployment paths remain separately gated.

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
- Never commit credentials, registration tokens, private keys, real environment files, internal host inventories, host addresses, service addresses, or unredacted infrastructure reports. The sole address exception is reviewed Docker `default_address_pools[].base` CIDRs and optional `default_bridge_cidr` gateway/interface CIDRs in private Git-authored fleet policy because they are required capacity policy. This exception does not allow credentials, SSH details, secrets, VM, storage, or backup identifiers, unrelated infrastructure addresses, or rendered runtime configuration.
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

Keep changes small and reversible. Existing project CI must continue operating until the new fleet path has passed parallel validation, completed the compliance checklist, and has an explicit tested rollback or documented reinstall procedure appropriate to the host's failure boundary.


## Documentation impact

- Always assess documentation impact when working on an issue, preparing a commit, or creating, updating, or reviewing a PR. Repeat the assessment if the scope changes.
- Check whether the change affects documented behavior, usage, setup, configuration, APIs, architecture, deployment, or operations. Read the relevant documentation and compare it with the implementation.
- When implementing a change, update affected documentation in the same commit or PR. Follow the repository's documentation and changelog conventions. Preserve historical release notes and immutable records.
- When reviewing, report missing or inaccurate documentation as a finding.
- Before declaring the work complete, check updated examples, commands, links, and configuration names. Run applicable documentation checks.
- When creating or updating a PR, state the documentation impact in its description. In all cases, include the assessment in the completion summary. List updated documents, or explain why no documentation changes are needed. Report any remaining gaps.

## Configuration template follow-up

- Assess every feature rollout for impact on the standalone `RandomDevelopment/ci-fleet-config-template`: schema, desired state, initializer, validation, capability evidence, examples, setup, and operator guidance. Include host features that need only adoption documentation.
- When support or guidance is affected, automatically search existing template issues and create or reuse a matching open issue without waiting for a separate user request. Follow the [contributor procedure](docs/CONTRIBUTING.md#configuration-template-follow-up). Record the follow-up by engine merge or release, whichever happens first.
- Identify the source engine issue/PR, full reviewed commit, gap, affected template files, acceptance checks, capability staging, migration requirements, and documentation impact. Mark unmerged features pending and update the follow-up with the full merged commit when available.
- Link the template issue in the engine PR description and completion report, or explain why no template change is needed. If issue lookup, creation, or update fails, report the missing tracking as outstanding rollout work; do not claim template synchronization.
- A tracked issue is not a completed template change. Engine availability, a published template release, adopter engine promotion, and live-host acceptance require separate evidence and authorization. This workflow grants no template merge, release, private configuration change, or host activation authority. Keep ordinary CI read-only; no new automation credentials are required.
