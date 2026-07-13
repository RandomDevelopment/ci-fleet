# Project Compliance Checklist

A project MUST complete this checklist before its existing required CI is moved to ci-fleet.

## Project contract

- [ ] `scripts/ci/run.sh fast` exists and is executable.
- [ ] `scripts/ci/run.sh full` exists when the project has integration tests.
- [ ] Both commands run project validation inside project-owned containers.
- [ ] The runner host does not provide the project language runtime.
- [ ] Local and CI execution use the same entrypoint.

## Isolation

- [ ] Compose project names are unique per run and attempt.
- [ ] No fixed `container_name` exists.
- [ ] No fixed host port is published.
- [ ] Tests use internal service DNS where possible.
- [ ] Cache names are scoped by repository and purpose.
- [ ] Run-owned Docker resources remain identifiable after interruption.

## Cleanup

- [ ] Cleanup is registered before resources are created.
- [ ] Success leaves no disposable run resources.
- [ ] Test failure leaves no disposable run resources.
- [ ] Cancellation recovery has been verified.
- [ ] Repeated runs do not accumulate inactive volumes.
- [ ] Cleanup never performs an unrestricted global prune.

## Permissions and secrets

- [ ] Ordinary CI explicitly declares `permissions: contents: read`.
- [ ] Fork pull-request jobs do not receive secrets or write tokens.
- [ ] Normal CI has no deployment or release credentials.
- [ ] Required test secrets are explicitly named.
- [ ] No blanket secret inheritance is used by default.
- [ ] Logs, artifacts, caches, and images contain no secrets.

## Privileged separation

- [ ] Deployment jobs use a separate runner group.
- [ ] Release and repository-writing jobs use a separate runner group.
- [ ] Internal-network jobs use a restricted runner group.
- [ ] Read-only CI cannot select a privileged group through project-controlled input.

## Migration proof

- [ ] Manual experimental run passed.
- [ ] Old and new CI ran on the same commits.
- [ ] Results and artifacts matched.
- [ ] Resource consumption was recorded.
- [ ] Forced failure cleanup passed.
- [ ] Cancellation cleanup passed.
- [ ] Existing required checks remained available during validation.
- [ ] Rollback instructions were tested.
- [ ] Project-specific runners were retained through the observation period.
