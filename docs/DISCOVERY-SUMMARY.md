# Discovery Summary

This is a sanitized summary of the initial read-only infrastructure audit. It intentionally excludes private repository contents, machine identifiers, addresses, credentials, and internal network topology.

## Verified current state

- Existing self-hosted runners are persistent services installed directly on virtual machines.
- Existing runners are scoped to individual repositories rather than a shared organization pool.
- Jobs control each runner VM's Docker daemon and therefore must be treated as host-privileged.
- One initial project performs application validation substantially inside Docker and Compose.
- A second initial project runs its primary language toolchain directly on the runner host and uses Docker only for a database dependency.
- Existing workflows contain a mixture of read-only validation and privileged write or deployment behavior.
- Some privileged jobs currently share runner labels with ordinary validation jobs.
- Current project cleanup is inconsistent across repositories.
- Fixed host ports and fixed Docker resource names prevent safe concurrency for some jobs.
- Significant reusable Docker build cache and inactive volume residue can accumulate.
- Automatic security updates exist on at least one current host, but drain-aware reboot handling and fleet-wide verification are not yet established.
- Fleet-specific disk alerts, BuildKit garbage collection policy, and centralized workspace cleanup were not verified.

## Design consequences

- The first runner must use a new experimental label and must not replace existing CI.
- The first workflow must be manual and explicitly read-only.
- Deployment, release, schema-writing, and internal-network jobs must remain outside the shared validation pool.
- Initial capacity is one job per Docker host or isolated Docker daemon until collision and cleanup behavior is proven.
- Project-specific runtime dependencies belong in project test images, not the fleet runner image.
- Cleanup must be layered: project cleanup, runner destruction, and host garbage collection.
- A reboot or forced cancellation must not be assumed to execute project cleanup.
- Long-lived controller credentials must remain unavailable to job containers.

## Remaining discovery

Before production migration:

- verify organization runner administration and runner-group capabilities;
- review private fork workflow access to secrets and write tokens;
- verify update configuration on every initial host;
- define firewall and network access policy;
- verify backup and recovery expectations;
- define runner draining;
- define monitoring and disk-pressure responses;
- remove project-level fixed ports and Docker names;
- create a project-owned test image for any project still depending on host language runtimes.
