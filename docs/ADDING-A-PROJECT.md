# How to add a project

Use this runbook to authorize a trusted private project to consume an existing ci-fleet.

## Outcome

Project onboarding does not change, rebuild, restart, or re-register any fleet host or worker container. Generic runners learn the repository only when GitHub assigns a job.

A project is onboarded in three control planes:

1. the project repository supplies its Docker test contract and workflow;
2. private fleet configuration declares policy and expected capability;
3. GitHub runner-group access authorizes the repository.

Repository names are never baked into the public runner image, controller image, host environment, or individual ephemeral runner.

## 1. Confirm the trust boundary

The shared validation pool is for trusted private repositories only. A job with Docker access is host-root-equivalent.

Do not authorize:

- public repositories;
- untrusted fork pull-request code;
- deployment or internal-management jobs;
- repository-writing or release jobs;
- a project whose maintainers are not trusted for the shared Docker-host boundary.

Use separate runner groups and hosts for privileged or differently trusted workloads.

## 2. Choose the runner-group repository policy

GitHub runner groups can authorize selected repositories or all organization repositories. Keep GitHub's public-repository access override disabled.

### Selected repositories

Use this when private repositories have different maintainers or trust levels. Add each project once to the runner-group allowlist. This is a GitHub policy change, not a host or container change.

The private fleet configuration should remain the reviewable source of intended access. Until policy synchronization is automated, compare it with GitHub during onboarding and periodic audits.

### All private repositories

Use this only when every present and future private repository in the organization is trusted to execute host-privileged Docker workloads. It minimizes onboarding steps but expands the effect of a compromised or accidentally unsafe repository.

Changing between these policies never requires rebuilding fleet hosts.

## 3. Add private fleet policy

In the organization-owned private configuration repository:

- declare the repository;
- select the shared validation pool and routing label;
- keep public-repository access false;
- declare required secret names without values;
- keep deployment and release pools separate;
- validate the configuration before merge.

Never store credentials, private keys, tokens, real environment files, or host inventories in configuration.

## 4. Implement the project contract

The project must provide:

```text
scripts/ci/plan.json
scripts/ci/run.sh <task> --shard INDEX/TOTAL
scripts/ci/run.sh fast
scripts/ci/run.sh full
```

Follow [Project CI Standard](PROJECT-STANDARD.md). Existing projects must follow [Migrating Existing CI](MIGRATING-EXISTING-CI.md) and complete the [Compliance Checklist](COMPLIANCE-CHECKLIST.md).

Application runtimes, dependencies, service containers, migrations, test fixtures, task selection, and run-owned cleanup belong to the project. The fleet host must not provide them.

## 5. Add the read-only workflow

Start from `examples/workflows/parallel-ci.yml.example` and:

- pin ci-fleet actions to a reviewed immutable commit;
- declare `permissions: contents: read`;
- select only the normal shared CI label;
- expand the project task plan;
- set five-minute timeouts on ordinary jobs;
- pass only explicitly required test secrets;
- keep release, branch mutation, deployment, and internal-network access out of the workflow.

The workflow should not name a host, site, controller, instance, or scale set. It selects the shared capability label, allowing GitHub to route work to any healthy location.

## 6. Prove the migration

1. Validate all direct task/shard commands through project containers.
2. Run fast and full aggregates locally.
3. Dispatch one harmless manual shard.
4. Compare old and new CI on the same commits.
5. Verify success, failure, cancellation, timeout, cleanup, and repeated concurrency.
6. Record shard timings and rebalance work approaching the ceiling.
7. Keep the old required path available until rollback is proven.
8. Cut over required checks only after the compliance checklist passes.

Adding more hosts during this process requires no project workflow change because every compatible host shares the routing label.

## 7. Remove a project

1. Remove the repository from the GitHub runner-group allowlist, or move it outside the scope of an all-private policy.
2. Remove or disable its fleet workflow.
3. Remove its entry from private fleet configuration.
4. Revoke project test secrets that are no longer needed.
5. Confirm no queued job still targets the shared label.

Do not edit or rebuild fleet hosts. Other projects and locations continue operating normally.

## Onboarding checklist

- [ ] Repository is private and trusted for the shared Docker-host boundary.
- [ ] Private configuration declares it and validates.
- [ ] GitHub runner-group policy authorizes it.
- [ ] Public repository access remains disabled.
- [ ] Project Docker contract passes locally.
- [ ] Ordinary workflow is read-only and uses the shared label.
- [ ] Privileged jobs use separate routing.
- [ ] Manual proof, parallel comparison, cleanup tests, and rollback pass.
- [ ] Existing required CI remains until cutover is complete.
