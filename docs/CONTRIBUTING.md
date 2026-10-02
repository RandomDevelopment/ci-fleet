# Contributing to ci-fleet

This document defines the contributor contract that governs every commit and
pull request in `RandomDevelopment/ci-fleet`. It is mandatory for all authors
and agents editing this repository.

## Release model

- **Versioning**: [Semantic Versioning 2.0.0](https://semver.org/). Tags may use
  an optional leading `v` (e.g. `v1.2.3`), but the version payload is always a
  valid SemVer string.
- **Commit format**: [Conventional Commits 1.0.0](https://www.conventionalcommits.org/).
  The project squash-merges to `main`, so the squash title is the canonical
  release entry and the PR title must itself be a conventional subject.
- **Pre-1.0 / `0.y.z`**: the project is in initial development. A `0.y.z`
  release has an unstable public API: anything may change at any time without
  notice. PATCH is still permitted for pure internal fixes, but MINOR and
  MAJOR carry no stability guarantee until a `1.0.0` is tagged. Do not invent
  or publish a release merely to satisfy versioning rules; releases are gated
  by `docs/CONTRIBUTING.md` and the operator review window below.
- **No force-push** of published history and no rebased rewrites of shared
  branches. Use `git revert` for corrections.

## Conventional Commits 1.0.0

```
<type>[optional scope][!]: <description>
```

- The `<type>` MUST be one of: `build`, `chore`, `ci`, `docs`, `feat`, `fix`,
  `perf`, `refactor`, `revert`, `style`, `test`.
- The `<scope>` is optional and nested in parentheses, e.g. `feat(runner):`.
- Append `!` before the colon to mark a breaking change.
- The `<description>` is a single line; the complete subject (type, scope,
  marker, separator, and description) is limited to <=100 characters and the
  description begins with a lowercase letter (lowercase ASCII type + scope is
  the convention; the subject itself may contain capitals for identifiers).
- Separate the subject from the body with exactly one blank line.
- Footers use `Token: value` form. A breaking change MAY also be declared with
  a `BREAKING CHANGE:` footer (uppercase, per spec).

### SemVer mapping

| Commit                                 | Bump   |
| ---                                    | ---    |
| `fix:` or `perf:`, `refactor:`, `chore:`, `ci:`, `build:`, `style:`, `test:`, `docs:` (no `!`) | PATCH  |
| `feat:` (no `!`)                       | MINOR  |
| `feat!:`, `fix!:`, any `!`, or `BREAKING CHANGE:` footer | MAJOR  |

### Examples

```
feat: add capacity telemetry endpoint
fix(runner): close leak on job cancellation
docs: record five-minute CI shard contract
ci: enforce conventional commits and semantic versioning
perf: cache host capability lookup
refactor: de-duplicate reconcile drift detection
feat!: replace the legacy controller entrypoint
```

```
fix(ci): stop recommending mutable image tags

The previous guidance used `:latest`, which violates pinning requirements.

Closes #42
Reviewed-by: An Operator <an-operator@example.org>
```

```
feat(controller): drop the legacy reconcile command

BREAKING CHANGE: `install-worker-controller.sh --reconcile` is removed.
Operators must use `--upgrade` instead.
```

## Public API / compatibility contract

The versioned public API of ci-fleet consists of the following stable
interfaces. A breaking change to any entry increments the MAJOR version.

1. **Configuration schema** — `templates/config-repository/fleet.schema.json`,
   `schema_version: 3`. Managed projects submit Git-authored desired state
   validated against this schema.
2. **Task-plan schema** — `examples/project/scripts/ci/plan.schema.json`,
   `schema_version: 1`. The matrix-expansion contract consumed by project
   workflows.
3. **Status-report evidence format** — `schemas/status-report-v1.json`,
   `schema_version: 1`. The format emitted by `scripts/status_receiver.py` and
   consumed by health/monitoring tooling.
4. **Engine rollout evidence format** —
   `templates/config-repository/engine-rollout-evidence.json`,
   `schema_version: 1`.
5. **Installer command contract** —
   `scripts/install-worker-controller.sh` with `--install`, `--adopt`,
   `--check`, `--upgrade`, `--rollback`, `--uninstall` and the
   `--config-repo`, `--ref`, `--controller` arguments.
6. **Host-role command contracts** — the systemd unit command lines under
   `host/systemd/*`, including the cleanup, health, drift, and reconcile
   timer/entry-point contracts.
7. **Generated task matrix** — the `include` output produced by
   `.github/actions/plan/plan.py`, consumed by project workflow matrices.

Non-API commits (docs, tests, CI, chore, style) never bump the public version
for API purposes; CI enforces PATCH-level change at minimum.

## Release gate

A version is released (tagged on `main`) only when:

- the tagged commit passes all CI checks;
- the change set is reviewed and the SemVer bump matches the Conventional
  Commits classification;
- an operator has confirmed the live pilot evidence for any engine rollout
  evidence schema change.

Do not tag a release to force a version number. This repository is pre-1.0;
avoid `1.0.0` until the controlled migration and compliance checklist
(`docs/COMPLIANCE-CHECKLIST.md`) are complete.

## Prerelease and build metadata

- Prerelease identifiers are supported by the validator but are not used for
  `main`-sourced tags. Use `.0` patch sequences or branch-local tags only.
- Build metadata (`+build.<source>`) is permitted on tags but MUST NOT affect
  SemVer precedence ordering.

## Validation

`scripts/validate_commits.py` enforces the Conventional Commits grammar, the
SemVer validator, and a `--suggest-bump` helper. `scripts/test_validate_commits.py`
is the regression suite. Both run in CI (see the `commit-convention` job in
`.github/workflows/validate.yml`).

Run locally:

```bash
python3 scripts/test_validate_commits.py
python3 scripts/validate_commits.py --message - <<< 'feat: local example'
python3 scripts/validate_commits.py --version 0.1.0
```
