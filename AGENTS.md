# Agent Instructions

## Purpose

This repository defines a portable, public, self-hosted GitHub Actions CI fleet. It must remain generic enough to run on Docker hosts located on virtual machines, physical computers, or VPS infrastructure.

## Current phase

The project is in discovery and architecture definition. Do not create production deployment code until the design decisions are recorded and the isolated proof of concept is approved.

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
- Keep normal CI, release, deployment, and internal-network workloads separable.
- Make cleanup concurrency-aware and scoped to a workflow run.
- Do not use unrestricted global Docker pruning as per-job cleanup.
- Pin production dependencies and container images to reviewed versions or digests.

## Verification expectations

Runnable changes must eventually include:

- formatting and static validation;
- container image build verification;
- secret scanning;
- configuration validation;
- an isolated end-to-end runner test;
- cleanup verification after success, failure, cancellation, and timeout;
- proof that long-lived controller credentials are unavailable to jobs.

## Change policy

Keep early changes small and reversible. Existing project CI must continue operating until the new fleet path has passed parallel validation and has an explicit rollback procedure.
