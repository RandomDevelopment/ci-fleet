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
  branches. Use `git revert` for corrections and edit its generated subject
  to a conventional `revert: <description>` before committing.

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

## Configuration template follow-up

For every feature rollout, assess the impact on the standalone
[configuration template](https://github.com/RandomDevelopment/ci-fleet-config-template).
Check schema, desired state, initializer, validation, capability evidence,
examples, setup, and operator guidance. A host feature can require template
adoption guidance even when it adds no configuration field. The engine's
embedded examples and the standalone template must each be assessed.

When template support or guidance is affected, the responsible contributor or
agent must automatically create or reuse a matching open template issue,
without waiting for a separate user request. Record it by the engine feature's
merge or release, whichever happens first. File it during PR preparation when
the impact is known. Mark the feature pending until merged, then update the
issue with the full merged engine commit when available. A pending issue should
identify the full reviewed PR head and must not describe it as merged.

Run the following from a development checkout with `gh` authenticated to the
appropriate repositories. Listing and inspection are read-only; issue creation,
editing, and comments change GitHub tracking. Replace uppercase placeholders
and fictional commit values before publishing.

1. Search open and closed issues using the feature's name, capability, and source
   issue/PR. Repeat with related terms and inspect matching bodies before filing:

   ```bash
   gh issue list --repo RandomDevelopment/ci-fleet-config-template \
     --state all --search 'FEATURE_KEYWORD' --limit 100
   gh issue view TEMPLATE_ISSUE_NUMBER \
     --repo RandomDevelopment/ci-fleet-config-template
   ```

   Reuse a matching open issue and preserve its existing requirements. A closed
   match may already document completed support; verify that support before
   deciding whether a new gap exists. An empty search alone does not establish
   that there is no matching issue.

2. Prepare the issue body in a temporary file outside the repository. Include
   all of the following, using public links and fictional examples only:

   ```bash
   template_issue_body=$(mktemp)
   cat > "$template_issue_body" <<'EOF'
   Source: ENGINE_ISSUE_URL and ENGINE_PR_URL
   Status: pending engine merge
   Reviewed engine PR head: 1111111111111111111111111111111111111111
   Merged engine commit: pending

   Gap: describe the missing behavior or operator guidance.
   Affected template files: list the actual paths to change.
   Acceptance checks: name validation, tests, examples, and documentation checks.
   Capability staging: identify required capabilities and evidence, or explain
   why none are needed. Separate manager staging from runtime activation.
   Migration: describe compatibility, adoption order, and rollback requirements,
   or explain why no migration is needed. Do not activate hosts through this issue.
   Documentation impact: list affected guides and examples, or explain why none.
   EOF
   ```

3. If no matching open issue covers the gap, create one and retain its URL:

   ```bash
   gh issue create --repo RandomDevelopment/ci-fleet-config-template \
     --title 'feat: support FEATURE_NAME' --body-file "$template_issue_body" &&
   rm -- "$template_issue_body"
   ```

   For an existing open issue, edit its body to include missing information
   while preserving prior scope and acceptance criteria. After the engine
   merges, update the status and full merged commit with `gh issue edit
   TEMPLATE_ISSUE_NUMBER --repo RandomDevelopment/ci-fleet-config-template
   --body-file PATH_TO_COMPLETE_UPDATED_BODY`. Keep the reviewed PR head as
   distinct evidence if the merge commit differs. Do not close the template
   issue merely because the engine merged.

If lookup, creation, or update fails, report the missing tracking as outstanding
rollout work. Do not claim template synchronization without a created or reused
issue, and do not claim implementation or adoption from issue tracking alone.

### PR and completion checklist

- [ ] Assess documentation impact and update affected engine documentation.
- [ ] Link the matching template issue, or explain why no template change is needed.
- [ ] Include the source revision, acceptance checks, staging/migration needs,
  and documentation impact in the follow-up; update pending status after merge.
- [ ] Report checks, unresolved tracking, and separate rollout evidence in the
  completion report.

Template compatibility checks validate one reviewed engine baseline. Passing
them does not prove parity with the latest engine. Merged engine availability,
a published template release, adopter engine promotion, and live-host acceptance
are separate milestones. Creating a follow-up authorizes none of the latter
actions. This contributor workflow needs no new GitHub Actions automation or
credentials; ordinary CI keeps read-only permissions. See the
[repository boundaries](PUBLIC-PRIVATE-CONFIGURATION.md#engine-and-template-rollouts).

## Release gate

A version is released (tagged on `main`) only when:

- the tagged commit passes the complete [local suite](LOCAL-VALIDATION.md), and
  all established required hosted checks until their deliberate migration;
- the change set is reviewed and the SemVer bump matches the Conventional
  Commits classification;
- an operator has confirmed the live pilot evidence for any engine rollout
  evidence schema change.

The selected version component must increase. A MAJOR bump resets MINOR and
PATCH to zero; a MINOR bump resets PATCH to zero. Operators select the initial
nonzero version without classifying legacy commit history. A new release must
include the latest released commit in its history.

Organization members publish releases after the checks and review above.
Tag-push validation detects violations after publication; it does not prevent
an authorized writer from creating a tag. Review workflow changes along with
other PR code. Ordinary CI remains read-only.

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

The complete validation command and evidence contract are documented in
[local validation](LOCAL-VALIDATION.md). Local execution needs no Actions event,
`gh`, hosted allowance or Actions API. The current required check remains active
while parity and rollback are pending. Issue #114 must reconcile auto-merge with
the selected evidence policy before a required check is removed. A locally
passing subset never permits a release or bypasses review.
