# Operator workflows

Use this page to choose an outcome, confirm the responsible role, and either follow the current runbook or stop at an explicit product gate. It does not replace the linked runbooks.

## Status legend

- **Current** — implemented on the default branch and documented for the stated experimental scope.
- **Prototype only** — useful for isolated evaluation, not a managed or production path.
- **In review** — not available from the default branch; do not install it as current behavior.
- **Planned** — no supported command exists yet.
- **Not production-ready** — the repository does not authorize production use.

## Choose an outcome

| Outcome | Status | Runs in | Changes state? | Current path or stopping condition |
| --- | --- | --- | --- | --- |
| Install or converge an ordinary-CI worker/controller | Current, experimental | Prepared Linux Docker host, using reviewed private configuration | Yes | Follow the [worker quickstart](QUICKSTART.md) and [desired-state lifecycle](DESIRED-STATE.md). |
| Add another worker host or location | Current, experimental | Private configuration, then the new Linux host | Yes | Follow [adding a host](ADDING-A-HOST.md); the application repositories do not change. |
| Authorize and onboard an application project | Current, experimental | Application repository, private configuration, and GitHub runner-group policy | Yes; no host mutation | Follow [adding a project](ADDING-A-PROJECT.md), then the [migration procedure](MIGRATING-EXISTING-CI.md). |
| Create controller GitHub credentials and runner-group prerequisites | Current manual procedure; automation in review | GitHub web UI and the target host | Yes | Follow [GitHub App setup](GITHUB-APP-SETUP.md) and the [runner-group procedure](LIVE-PILOT.md#2-create-the-organization-runner-group). [Issue #27](https://github.com/RandomDevelopment/ci-fleet/issues/27) and [PR #75](https://github.com/RandomDevelopment/ci-fleet/pull/75) track automated bootstrap and its required organization-owner evidence; do not use it from the default branch yet. |
| Run an isolated first-job proof | Prototype only | GitHub Actions and one isolated worker host | Yes, transiently | Follow the [live pilot](LIVE-PILOT.md). Stop if the matching job queue is not proven empty. |
| Install a persistent test/staging environment host | In review | Separate test host | N/A on the default branch | Follow [test-environment host](TESTER-HOST.md); [issue #23](https://github.com/RandomDevelopment/ci-fleet/issues/23) and [PR #76](https://github.com/RandomDevelopment/ci-fleet/pull/76) track the installer and prepared-host evidence. Do not install it from the default branch yet. |
| Install a deployment host | In review | Separate deployment host | N/A on the default branch | [Issue #22](https://github.com/RandomDevelopment/ci-fleet/issues/22) and [PR #69](https://github.com/RandomDevelopment/ci-fleet/pull/69) track the installer and its required real-host evidence. Do not install it from the default branch yet. |
| Deploy to production | Not production-ready | Separate production boundary | N/A | Stop. The project status and production evidence do not authorize this workflow. |
| Publish or update from the standalone configuration template | Planned release path | Management workstation and a private configuration repository | Yes | Use the template vendored in the exact reviewed engine commit today. A standalone immutable release/compatibility signal remains [open](https://github.com/RandomDevelopment/ci-fleet-config-template/issues/12). |

## Roles must remain separate

| Role | Purpose | May hold | Must never hold or do |
| --- | --- | --- | --- |
| CI worker/controller | Run short ordinary-CI jobs in disposable runner containers | Narrow runner-registration identity and generic host state | Deployment credentials, production authority, project runtimes on the host, or untrusted public-PR jobs |
| Test/staging environment host | Keep an isolated deployed candidate available for integration or browser testing | Test-only credentials and disposable test data | Production credentials, production data, ordinary runner jobs, or production promotion |
| Deployment host | Apply an explicitly approved immutable artifact through application-owned deployment logic | Environment-specific deployment identity | Ordinary runner jobs, source builds for promotion, or another environment's credentials |
| Application project | Define its test image, task plan, and thin Actions workflow | Project code and project-scoped CI secrets | Fleet controller credentials, host inventory, or host-specific capacity |
| Private configuration repository | Declare reviewed pools, controllers, capacity, and required secret names | Secret-free logical desired state | Secret values, host addresses, project runtime code, or unreviewed engine refs |

A Linux machine must not combine the worker, tester, and deployer roles. Runner containers share a Docker daemon security boundary with their host; container separation does not make credentials on that host safe from a host-privileged job.

## Where each step happens

| Surface | Operator action | Mutates state? |
| --- | --- | --- |
| GitHub web UI | Create/install the GitHub App, create the restricted runner group, review configuration changes, authorize a repository, dispatch the proof job | Yes |
| Management workstation | Prepare and validate secret-free desired state, open configuration/application pull requests, record immutable commit IDs | Yes when committed or pushed |
| Private configuration repository | Review runner pools, logical controllers, capacity, repository allowlists, lifecycle, and engine pins | Yes when merged |
| Application repository | Add the project-owned test image, task plan, CI entrypoint, and thin workflow | Yes when merged |
| Linux worker host | Place host-local identity files, run the installer, and verify cleanup/health | Yes for lifecycle operations; `--check` is read-only |
| Hypervisor or hosting control plane | Provision the isolated Linux machine, network, storage, and recovery boundary | Yes, but outside this repository; stop and hand off to its authorized operator |

Never put credentials, private host details, or real infrastructure inventory in this public repository or in secret-free desired state.

## Current one-command worker lifecycle

Prerequisites and credential placement are deliberate stopping points, not work hidden inside the installer. Complete the [quickstart](QUICKSTART.md) through reviewed configuration and host-local identity setup first.

Runs on: the prepared Linux worker host. Mutates state: **yes** (`--install`).

```bash
sudo ./scripts/install-worker-controller.sh --install --config-repo example-org/example-fleet-config --ref 1111111111111111111111111111111111111111 --controller example-ci-01
```

Runs on: the Linux worker host. Mutates state: **no** (`--check`).

```bash
sudo ./scripts/install-worker-controller.sh --check --config-repo example-org/example-fleet-config --ref 1111111111111111111111111111111111111111 --controller example-ci-01
```

The same script owns `--adopt`, `--upgrade`, `--rollback`, and `--uninstall`. Read the mode-specific prerequisites, stop conditions, expected report, and rollback behavior in [desired-state lifecycle](DESIRED-STATE.md) before using a mutating mode.

## End-to-end worker checklist

### Before mutation

- [ ] The role is an ordinary-CI worker only; no tester, deployer, production, or unrelated workload shares the host.
- [ ] The engine ref is a reviewed full commit reachable from the public default branch.
- [ ] The desired-state ref is a reviewed merge commit from the private configuration repository.
- [ ] The configuration validates strictly and contains fictional/public-safe data only where examples are involved.
- [ ] Host-local credentials exist with the ownership and mode required by the runbook; no credential value appears in Git or command arguments.
- [ ] GitHub App installation, runner-group restriction, and the controlled-first-job queue condition are proven.
- [ ] The operator has an explicit host recovery or replacement boundary.

### After mutation

- [ ] Installer result is successful and records the intended controller, engine ref, and lifecycle state.
- [ ] `--check` reports `CHECK_OK` against the same immutable inputs.
- [ ] The controlled proof job uses read-only permissions and the expected shared routing label.
- [ ] The job is attributed to the intended scale set/controller.
- [ ] No ephemeral runner container or job-owned Docker resource remains.
- [ ] A fresh health evaluation succeeds and the controller returns to its configured idle state.
- [ ] Existing project CI remains enabled until parallel validation and rollback gates are complete.

If any item cannot be proven, stop without broadening permissions or improvising another role's installer.

## Final report template

Store this report in the operator's approved private record, never in a public issue when it would reveal infrastructure or repository inventory.

```text
Operation:
Role:
Result: SUCCESS | STOPPED | FAILED
Engine commit:
Configuration commit:
Controller or logical target:
Installer mode:
Validation performed:
GitHub-side evidence:
Cleanup and health evidence:
Rollback or replacement point:
Warnings:
Next safe action:
```

The report records identifiers and outcomes, not credential values, tokens, private keys, addresses, or internal host details.

## Troubleshooting stops

| Symptom | Check | Safe next action |
| --- | --- | --- |
| Job remains queued | Complete `runs-on` expression, runner-group authorization, controller health, and scale-set identity | Correct reviewed policy or labels; do not raise capacity blindly. |
| Installer cannot read private configuration | Credential visibility for the root-run installer and scheduled drift service | Provide the documented narrow read path; do not copy a repository-writing token onto the worker. |
| Health service looks inactive | It is a timer-driven oneshot, so stale `systemctl status` is not a fresh evaluation | Start the health service explicitly, then run installer `--check` as documented in the quickstart. |
| Host is not clean after a job | Fleet ownership labels scoped to the runner/job, not global Docker state | Stop onboarding, preserve evidence, and use scoped cleanup only; never run global prune. |
| A guide asks for an unavailable role | Status table above | Stop at the linked issue or review gate; do not combine roles or invent a production path. |

For detailed errors and recovery commands, use the runbook linked from the selected outcome rather than reconstructing the workflow from this summary.
