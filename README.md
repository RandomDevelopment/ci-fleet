# ci-fleet

Portable, ephemeral, Dockerized GitHub Actions runner infrastructure for self-hosted environments.

> Status: architecture and isolated proof-of-concept planning. This repository does not yet contain a production-ready runner deployment.

## Goal

Run identical GitHub Actions worker containers across one or many Docker hosts while allowing each project to define its own containerized test environment.

A host may be a Proxmox VM, physical computer, remote-site machine, or VPS. Adding a host or increasing runner capacity should not require redesigning project workflows.

## Core model

- The fleet supplies generic ephemeral GitHub runners.
- Each runner accepts one job and is then destroyed.
- Each project supplies its own test Dockerfile, services, and commands.
- Normal CI uses a shared organization-level runner pool.
- Release, deployment, and internal-network jobs remain separated.
- Hosts apply automatic security maintenance and capacity-aware cleanup.
- Long-lived credentials remain in the controller or an external secret manager, never in job containers.

## Why this exists

The initial infrastructure audit found persistent repository-specific runners, inconsistent cleanup, project dependencies installed directly on some runner hosts, fixed Docker names and ports that prevent concurrency, and privileged jobs sharing labels with ordinary validation.

The first implementation will therefore be deliberately small: one experimental runner and one manual read-only smoke workflow running beside existing CI.

## Project boundaries

| Location | Responsibility |
| --- | --- |
| `ci-fleet` | Runner image, lifecycle controller, host bootstrap, maintenance, cleanup policy, reusable workflow interfaces |
| Project repository | Test image, services, test commands, fixtures, migrations, project secrets, run-scoped cleanup |
| Host-local configuration | Real organization settings, capacity, credentials, network policy, monitoring and maintenance windows |

## Security

A runner with access to a Docker daemon must be treated as host-privileged. Multiple runner containers sharing one Docker daemon increase concurrency but do not provide independent security boundaries.

Never commit credentials or real deployment configuration. See [SECURITY.md](SECURITY.md) and [docs/SECRETS.md](docs/SECRETS.md).

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Sanitized discovery summary](docs/DISCOVERY-SUMMARY.md)
- [Roadmap](docs/ROADMAP.md)
- [Secrets model](docs/SECRETS.md)
- [Agent instructions](AGENTS.md)
- [Third-party notices](THIRD_PARTY_NOTICES.md)

## First milestone

The first proof of concept must:

1. register one runner under a new experimental label;
2. accept exactly one manually triggered read-only job;
3. use `permissions: contents: read`;
4. leave no job-owned container, network, volume, or workspace residue;
5. keep all existing project CI unchanged;
6. demonstrate a rollback that removes only the experimental path.

## License

Original work in this repository is released under [the Unlicense](LICENSE).
