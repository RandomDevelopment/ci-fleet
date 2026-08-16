# Accepted design decisions

## Managed controller desired state after the isolated proof

**Status:** accepted on 2026-07-22 for isolated CI fleet hosts.

The isolated one-job proof required by [Issue #7](https://github.com/RandomDevelopment/ci-fleet/issues/7) passed on 2026-07-15. It proved the organization-owned App boundary, selected private runner group, `MIN=0` / `MAX=1` controller, read-only job permissions, one-job ephemeral runner lifecycle, scoped cleanup, zero final job residue, controller health, and preservation of existing project runners.

[Issue #32](https://github.com/RandomDevelopment/ci-fleet/issues/32) and its reviewed implementation in PR #33 accept schema-v3 Git-authored controller desired state as the next migration phase. The managed installer may install, adopt, check, upgrade, roll back, or uninstall an isolated **ordinary-CI controller** only from:

- a merged, publicly reachable `RandomDevelopment/ci-fleet` engine commit;
- a merged, secret-free private configuration commit;
- host-local root-owned credentials and identity; and
- a recoverable checkpoint with the documented health, drain, drift, and rollback gates.

This decision does not authorize application production deployment, privileged delivery on ordinary-CI runners, unreviewed capacity increases, public-repository runner access, unrestricted Docker cleanup, legacy-runner retirement, or VM deletion. Those remain separately gated by repository policy and operator approval.

## Project name

**Status:** accepted on 2026-08-16; retain `ci-fleet`.

`ci-fleet` names the mature shared subsystem: a portable fleet of ephemeral CI workers. Tester hosts and production deployers are deliberately separate roles, credentials, services, and trust boundaries; naming the repository after all three would suggest an integration the architecture forbids. A rename would also invalidate or migrate pinned action references, image and Compose identities, environment variables, private configuration, App and runner-group names, installed hosts, links, clones, and operator procedures before either additional role is production-ready. That churn has no current operating benefit.

The evaluated alternatives were `delivery-fleet`, `build-test-deploy-fleet`, and `software-delivery-fleet`. All three were available in the `RandomDevelopment` GitHub namespace on 2026-08-16. GitHub name search found no exact `build-test-deploy-fleet` or `software-delivery-fleet` repository; `delivery-fleet` is used by unrelated logistics projects and is ambiguous. The current name remains shorter, searchable, integration-neutral, and accurate for the repository's implemented production boundary.

Reconsider only after tester and deployer roles are implemented, independently proven, and operators demonstrably need one public umbrella identity. If that happens, write a new ADR first and stage the migration: reserve the name; inventory every public and private consumer; preserve GitHub redirects and compatibility aliases; update docs, badges, action references, module/package paths, images, labels, Compose projects, environment variables, configuration templates, Apps, runner groups, scale sets, installed hosts, scripts, and prompts; canary new references while old references remain valid; then deprecate in a documented release window. Roll back by restoring the old canonical name and aliases before removing any compatibility path. No repository, image, App, runner group, host, or external consumer is renamed by this decision.
