# ADR 0002: Adopt the delivery-fleet name

- Status: Accepted; migration deferred
- Date: 2026-07-14
- Tracks: #25

## Context

The project began as a shared CI runner fleet. Its intended scope now covers three separated capabilities:

- ephemeral CI workers;
- testing and staging environments;
- production deployment control.

The name `ci-fleet` understates that scope.

## Decision

Use **delivery-fleet** as the umbrella project name. Keep worker, tester, and deployer as distinct components with separate credentials, permissions, lifecycles, and host-placement choices.

Do not rename the repository, GitHub App, images, environment variables, or the running pilot before the first live pilot succeeds. The existing `ci-fleet` identity remains the compatibility baseline until a reviewed migration plan is executed.

## Migration gate

After the live pilot passes:

1. inventory every public and private reference to `ci-fleet`;
2. define compatibility aliases and a deprecation period;
3. prepare rollback for repository, image, configuration, and installed-host changes;
4. migrate in stages without interrupting existing workers;
5. update public documentation and examples to use `delivery-fleet`.

## Consequences

The project has a name broad enough for build, test, and deployment orchestration without combining their trust boundaries. Issue #25 remains open until the staged rename is complete.
