# Documentation

Use this index to find ci-fleet concepts, requirements, examples, and step-by-step procedures. Repository Markdown is the authoritative documentation and is versioned with the implementation it describes.

## Start here

| I want to… | Read |
| --- | --- |
| Understand the whole system | [Architecture](ARCHITECTURE.md) |
| Understand how ephemeral runners are created | [Runner controller design](CONTROLLER-DESIGN.md) |
| Decide whether it fits my infrastructure | [Architecture](ARCHITECTURE.md) and the root [README](../README.md) |
| Try the experimental implementation safely | [Live pilot runbook](LIVE-PILOT.md) |
| Add another Docker host, VM, computer, or VPS | [Adding a host](ADDING-A-HOST.md) |
| Manage controller configuration from Git | [Git-authored controller desired state](DESIRED-STATE.md) |
| Add a private project to the shared runner pool | [Adding a project](ADDING-A-PROJECT.md) |
| Convert an existing GitHub Actions workflow | [Migrating existing CI](MIGRATING-EXISTING-CI.md) |
| Make a project compliant | [Project CI standard](PROJECT-STANDARD.md) and [compliance checklist](COMPLIANCE-CHECKLIST.md) |
| Split tests across parallel workers | [Project CI standard](PROJECT-STANDARD.md) and the [parallel workflow example](../examples/workflows/parallel-ci.yml.example) |
| Configure automatic updates and cleanup | [Host maintenance](HOST-MAINTENANCE.md) |
| Handle GitHub App, workflow, or deployment secrets | [Secrets model](SECRETS.md) and [security policy](../SECURITY.md) |
| Run private CI or deployment for a public project | [Public projects, private delivery, and private configuration](PUBLIC-PRIVATE-CONFIGURATION.md) |
| Review current priorities | [Roadmap](ROADMAP.md) |
| See what informed the design | [Discovery summary](DISCOVERY-SUMMARY.md) |

## Concepts

| Term | Meaning |
| --- | --- |
| Fleet host | A generic Linux machine, VM, or VPS running Docker and a controller. It contains no project runtime. |
| Controller | Host-side service that watches GitHub demand and creates or removes ephemeral runners. |
| Ephemeral runner | A disposable GitHub Actions runner container that accepts one job and is destroyed. |
| Project test container | The project-owned Docker image containing its Node, PHP, Python, database, or other test environment. |
| Runner group | GitHub organization policy that controls which repositories may use a runner pool. |
| Scale set | One controller's uniquely named runner capacity advertised to GitHub. |
| Shared routing label | The stable `runs-on` capability used by compatible projects, regardless of which physical host accepts the job. |
| Test shard | One bounded slice of a larger test suite, designed to run independently and usually finish within five minutes. |
| Private delivery configuration | Repository allowlists, logical controllers, capacity budgets, environment policy, and required secret names kept in a private but secret-free repository. |
| Desired state | A reviewed schema-v3 declaration of runner pools and logical controllers stored in a private, secret-free Git repository. |

## Design and boundaries

- [System architecture](ARCHITECTURE.md)
- [Runner controller design](CONTROLLER-DESIGN.md)
- [Public projects, private delivery, and private configuration](PUBLIC-PRIVATE-CONFIGURATION.md)
- [Secrets model](SECRETS.md)
- [Security policy](../SECURITY.md)

## Standards and contracts

These pages are normative for compatible projects and hosts:

- [Project CI standard](PROJECT-STANDARD.md)
- [Migration procedure](MIGRATING-EXISTING-CI.md)
- [Compliance checklist](COMPLIANCE-CHECKLIST.md)
- [Host maintenance standard](HOST-MAINTENANCE.md)
- [Git-authored controller desired state](DESIRED-STATE.md)
- [Secrets model](SECRETS.md)
- [Security policy](../SECURITY.md)

## Operator how-tos

- [Run the live pilot](LIVE-PILOT.md)
- [Add a host](ADDING-A-HOST.md)
- [Install, adopt, check, upgrade, roll back, or remove a controller](DESIRED-STATE.md)
- [Add a project](ADDING-A-PROJECT.md)
- [Deploy the current experimental prototype](DEPLOYMENT-PROTOTYPE.md)
- [Maintain, drain, clean, update, and reboot hosts](HOST-MAINTENANCE.md)
- [Use the public configuration-repository scaffold](../templates/config-repository/README.md)

The schema-v3 worker-controller installer is implemented as `scripts/install-worker-controller.sh`. It remains experimental; use the [desired-state guide](DESIRED-STATE.md), pin both configuration and engine commits, and retain the manual pilot runbook for isolated first-job proof.

## Project integration examples

- [Read-only experimental workflow](../examples/workflows/experimental-smoke.yml.example)
- [Private-repository live pilot](../examples/workflows/live-pilot.yml.example)
- [Parallel five-minute job matrix](../examples/workflows/parallel-ci.yml.example)
- [Project task plan](../examples/project/scripts/ci/plan.json)
- [Standard project CI entrypoint](../examples/project/scripts/ci/run.sh)
- [Project test Dockerfile](../examples/project/Dockerfile.test)
- [Isolated Docker Compose configuration](../examples/project/compose.ci.yaml)

Examples use fictional values and are starting points. Pin reviewed actions and images before production use.

## Documentation rules

- Mandatory instructions live in this repository.
- Implementation changes update affected documentation in the same pull request.
- Real secrets, hostnames, internal addresses, and private repository inventories never appear in public examples.
- The GitHub Wiki is not an independent source of operational truth.
- A future generated documentation site may improve browsing, but repository Markdown remains its source.
