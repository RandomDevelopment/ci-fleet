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
