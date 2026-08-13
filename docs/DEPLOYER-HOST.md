# Deployer host installation and operation

This runbook installs the generic ci-fleet deployer runtime on a dedicated Linux host. It does not deploy an application by itself. Application repositories retain their own deployment logic through the narrow adapter contract below.

A deployer is a separate trust boundary. It must never register as an ordinary GitHub Actions runner, accept pull-request jobs, share a Docker daemon with CI workers, or run on the ordinary controller host. Staging and production require different hosts or equally isolated security boundaries, identities, credential scopes, state, approval evidence, and target policy.

## Capability and security model

The runtime accepts only a full source commit, an image reference ending in `@sha256:<64 lowercase hex>`, exact matching approval evidence, explicit environment and target identities, a protected credential reference, and checkpoint evidence. It never follows `latest`, a branch, a moving tag, or an implicit production default.

GitHub-native Environment protection is optional, not assumed. In particular, do not assume a private repository on GitHub Free has protected Environments, Environment secrets, branch protection, or rulesets. Use one of these fail-closed approval providers:

- `manual-exact-head`: a protected host-local evidence file records an approval outside the secret store;
- `external-exact-head`: an external policy system writes the same bounded evidence contract;
- `github-environment`: accepted only with separate host-local capability evidence proving exact-head Environment protection was verified for this installation.

A secret store proves only that a credential exists. It is not approval evidence. Every provider still binds the environment, target, source commit, artifact digest, approving identity, policy identity, approval ID, and UTC time.

Production remains separately gated during the controlled-migration phase. `ENVIRONMENT=production` is rejected unless `PRODUCTION_AUTHORIZATION_EVIDENCE_PATH` names an additional protected exact-target, exact-head, exact-artifact authorization record. Ordinary approval or credential access does not satisfy this gate, and the field is rejected outside production.

The systemd services run as root because access to the Docker socket is root-equivalent and protected credential references may be root-only. `NoNewPrivileges`, a read-only host filesystem, explicit writable paths, private temporary storage, and no supplementary service account reduce accidental reach, but they do not turn Docker access into a low-privilege boundary. Put nothing else on this host.

## Supported host and prerequisites

The installer supports Debian 12/13 and Ubuntu 22.04/24.04 with systemd. It checks Docker Engine, synchronized time, disk capacity, DNS, HTTPS reachability, and Compose v2 when `REQUIRE_COMPOSE=1`. The configured network host is supplied to curl over standard input so a private endpoint is not placed in the process argument list.

It rejects:

- ordinary ci-fleet controller state or runner units;
- any unrelated running or stopped Docker container;
- any unrelated custom Docker network or volume;
- unsafe owners, modes, symlinks, traversal, malformed configuration, or ambiguous identities.

`--check` is read-only: it creates no user, directory, lock, release, service, timer, image, container, registration, or GitHub state. Mutating modes require root.

## Filesystem contract

| Path | Owner/mode | Purpose | Uninstall |
| --- | --- | --- | --- |
| `/etc/ci-fleet-deployer/` | root `0700` | host-local policy boundary | retained |
| `/etc/ci-fleet-deployer/deployer.conf` | root `0600` | bounded non-secret policy and references | retained |
| `/etc/ci-fleet-deployer/adapters/` | root `0700` | application-owned adapter | retained |
| `/etc/ci-fleet-deployer/credentials/` | root `0700` | credential files | retained |
| `/etc/ci-fleet-deployer/evidence/` | root `0700` | approval/capability/checkpoint evidence | retained |
| `/opt/ci-fleet-deployer/releases/<commit>/` | root `0755` | immutable core runtime release | retained |
| `/opt/ci-fleet-deployer/current` | root symlink | atomically selected core release | removed |
| `/var/lib/ci-fleet-deployer/` | root `0700` | active policy, request, drain, transaction, LKG state | retained except transient drain/active state |
| `/var/lock/ci-fleet-deployer/` | root `0700` | flock serialization boundary | retained |
| `/var/log/ci-fleet-deployer/` | root `0700` | secret-free audit log | retained |
| `/etc/systemd/system/ci-fleet-deployer*` | root `0644` | deploy, health, cleanup, and drain units/timers | removed |

Releases, last-known-good state, audit records, configuration, credentials, and evidence are retained deliberately. Their retention or destruction is a separate operator decision.

## Application adapter contract

The adapter is an application-owned root-only executable under `/etc/ci-fleet-deployer/adapters`. Its SHA-256 is pinned in `deployer.conf`. Core invokes exactly one positional operation:

```text
adapter validate
adapter health
adapter cleanup
adapter deploy
adapter rollback
```

`validate` must be non-mutating and must prove that the candidate policy is usable before core changes the active release. `health` returns zero only when the deployer and application-owned contract are healthy. `cleanup` may remove only resources carrying the application's exact deployer ownership identity; it must never run global prune or touch unrelated resources. `deploy` reads the active policy and request paths from the documented environment variables and owns application-specific staging, rollout, health, and rollback. `rollback` restores application state compatible with the recorded last-known-good core policy.

Operations have no interactive input. Zero means success; nonzero means failure. Direct installer validation and health calls are limited to two minutes; rollback is limited to 45 minutes. The adapter must avoid child processes that outlive those bounds, redact logs, and never print credential contents, authorization headers, cookies, private endpoints, or secret-manager responses. Core validates immutable identifiers and approval evidence; it cannot validate application-specific correctness.

Rollback must be atomic from the adapter's perspective: nonzero restores the pre-call application state; zero means rollback health is already verified. Rollback is exposed only through the transactional installer, never as a direct runtime operation. For rollback only, core exports `CI_FLEET_DEPLOYER_ROLLBACK_COMMIT`; the adapter atomically creates that root-owned mode-`0600` file as its final successful step. Core stages and selects the last-known-good core before invoking the adapter, restores the newer core on an uncommitted failure, and consumes committed last-known-good state only after core/application alignment. A committed rollback interrupted after the adapter returns is finalized from the retained transaction on the next serialized installer operation.

## Prepare host-local files

All commands in this section run on the dedicated deployer host and change host-local state. They do not contact GitHub or deploy an application.

```bash
sudo install -d -o root -g root -m 0700 \
  /etc/ci-fleet-deployer \
  /etc/ci-fleet-deployer/adapters \
  /etc/ci-fleet-deployer/credentials \
  /etc/ci-fleet-deployer/evidence
sudo install -o root -g root -m 0700 ./application-adapter \
  /etc/ci-fleet-deployer/adapters/application-adapter
sudo install -o root -g root -m 0600 /dev/null \
  /etc/ci-fleet-deployer/credentials/application.credential
sudoedit /etc/ci-fleet-deployer/credentials/application.credential
```

`sudoedit` writes the credential without displaying it. Do not use a secret value in a command argument, shell history, environment variable, fixture, or log. An approved external secret-manager reference may replace the regular file; the adapter retrieves the value without core seeing it.

Create checkpoint evidence only after an operator or approved external backup adapter has produced a recoverable checkpoint. Core cannot see or operate a hypervisor and does not claim to verify one. This example records evidence, not a secret:

```bash
sudo install -o root -g root -m 0600 /dev/null \
  /etc/ci-fleet-deployer/evidence/checkpoint.conf
sudoedit /etc/ci-fleet-deployer/evidence/checkpoint.conf
```

Fictional checkpoint evidence:

```text
SCHEMA_VERSION=1
ENVIRONMENT=staging
TARGET_ID=example-staging
CHECKPOINT_ID=checkpoint-20260808-1
RECORDED_AT=2026-08-08T19:55:00Z
```

Create independent exact-head approval evidence:

```bash
sudo install -o root -g root -m 0600 /dev/null \
  /etc/ci-fleet-deployer/evidence/approval.conf
sudoedit /etc/ci-fleet-deployer/evidence/approval.conf
```

Fictional manual/external evidence:

```text
SCHEMA_VERSION=1
ENVIRONMENT=staging
TARGET_ID=example-staging
SOURCE_COMMIT=1111111111111111111111111111111111111111
ARTIFACT_IMAGE=registry.example.invalid/example/app@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
APPROVAL_IDENTITY=example-reviewer
POLICY_IDENTITY=example-staging-policy-v1
APPROVAL_ID=approval-20260808-1
APPROVED_AT=2026-08-08T20:00:00Z
```

For `github-environment`, also create mode-`0600` capability evidence. This file must be produced by an authorized capability check; copying the fictional text is not proof:

```text
SCHEMA_VERSION=1
ENVIRONMENT=staging
TARGET_ID=example-staging
ENVIRONMENT_PROTECTION=verified
EXACT_HEAD=1111111111111111111111111111111111111111
CAPABILITY_ID=example-capability-check
CHECKED_AT=2026-08-08T20:00:00Z
```

The `ENVIRONMENT` and `TARGET_ID` bind the evidence to one installation; capability evidence produced for a different installation is rejected even when the source commit matches.

For production, a separate authorized process must also create evidence such as:

```text
SCHEMA_VERSION=1
ENVIRONMENT=production
TARGET_ID=example-production
SOURCE_COMMIT=1111111111111111111111111111111111111111
ARTIFACT_IMAGE=registry.example.invalid/example/app@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
AUTHORIZED_BY=example-production-authorizer
GATE_ID=production-gate-20260808-1
AUTHORIZED_AT=2026-08-08T20:00:00Z
```

Production deployment is currently disabled: `docs/DESIGN-DECISIONS.md` keeps production deployment paths separately gated, and the runtime rejects every production request until an accepted decision enables them. The evidence above documents the intended record shape only; it does not lift the gate.

Create the bounded configuration. Values cannot contain shell expressions; the installer parses `KEY=VALUE` without sourcing it. Unknown, duplicate, empty, malformed, or missing keys fail closed.

```bash
core_ref=$(git rev-parse HEAD)
adapter_sha=$(sudo sha256sum /etc/ci-fleet-deployer/adapters/application-adapter | cut -d' ' -f1)
sudo install -o root -g root -m 0600 /dev/null \
  /etc/ci-fleet-deployer/deployer.conf
{
  printf 'SCHEMA_VERSION=1\n'
  printf 'CORE_REF=%s\n' "$core_ref"
  printf 'ENVIRONMENT=staging\n'
  printf 'TARGET_ID=example-staging\n'
  printf 'DEPLOYER_IDENTITY=staging-deployer-01\n'
  printf 'ADAPTER_PATH=/etc/ci-fleet-deployer/adapters/application-adapter\n'
  printf 'ADAPTER_SHA256=%s\n' "$adapter_sha"
  printf 'CREDENTIAL_PROVIDER=file\n'
  printf 'CREDENTIAL_REF=/etc/ci-fleet-deployer/credentials/application.credential\n'
  printf 'CREDENTIAL_SCOPE=staging\n'
  printf 'APPROVAL_PROVIDER=manual-exact-head\n'
  printf 'APPROVAL_EVIDENCE_PATH=/etc/ci-fleet-deployer/evidence/approval.conf\n'
  printf 'CHECKPOINT_EVIDENCE_PATH=/etc/ci-fleet-deployer/evidence/checkpoint.conf\n'
  printf 'SOURCE_COMMIT=1111111111111111111111111111111111111111\n'
  printf 'ARTIFACT_IMAGE=registry.example.invalid/example/app@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n'
  printf 'NETWORK_HOST=registry.example.invalid\n'
  printf 'MIN_DISK_GIB=10\n'
  printf 'REQUIRE_COMPOSE=1\n'
} | sudo tee /etc/ci-fleet-deployer/deployer.conf >/dev/null
sudo chmod 0600 /etc/ci-fleet-deployer/deployer.conf
```

For an external secret manager, use `CREDENTIAL_PROVIDER=external` and a non-secret reference shaped like `external:example-vault:staging/deployer`. For GitHub Environment approval, use `APPROVAL_PROVIDER=github-environment` and add `APPROVAL_CAPABILITY_EVIDENCE_PATH=/etc/ci-fleet-deployer/evidence/github-capability.conf`. Production additionally requires `PRODUCTION_AUTHORIZATION_EVIDENCE_PATH=/etc/ci-fleet-deployer/evidence/production-authorization.conf`.

## Install, check, repair, upgrade, drain, and rollback

These commands run from a clean checkout at the exact `CORE_REF` on the dedicated deployer host.

Fresh install changes the host:

```bash
sudo ./scripts/install-deployer.sh \
  --install --config /etc/ci-fleet-deployer/deployer.conf
```

Read-only validation:

```bash
sudo ./scripts/install-deployer.sh \
  --check --config /etc/ci-fleet-deployer/deployer.conf
```

Repair owned drift without selecting a different environment or target; changes the host only when drift exists:

```bash
sudo ./scripts/install-deployer.sh \
  --repair --config /etc/ci-fleet-deployer/deployer.conf
```

Upgrade after updating the exact core/source/artifact/approval/checkpoint fields; changes the host transactionally:

```bash
sudo ./scripts/install-deployer.sh \
  --upgrade --config /etc/ci-fleet-deployer/deployer.conf
```

The adapter candidate passes `validate` before activation. Core stages a complete commit-pinned release, atomically switches it, writes active policy/state with mode `0600`, and derives last-known-good from the atomically published deployed policy/state snapshot rather than an undeployed candidate. A failed candidate does not replace the current release or remove drain state.

Drain before reboot or maintenance; changes drain state and refuses while a deployment is active:

```bash
sudo ./scripts/install-deployer.sh \
  --drain --config /etc/ci-fleet-deployer/deployer.conf
sudo systemctl start ci-fleet-deployer-drain.service
```

After maintenance, explicitly resume through the same serialized installer boundary. This removes the managed drain marker only after full installed-state convergence, role isolation, and active-policy health pass; it is idempotent:

```bash
sudo ./scripts/install-deployer.sh \
  --resume --config /etc/ci-fleet-deployer/deployer.conf
```

Rollback changes the active core/application state but deliberately does not overwrite or depend on the operator-owned candidate evidence or registry preflight; it uses the retained last-known-good policy and local adapter. It refuses while a deployment is active:

```bash
sudo ./scripts/install-deployer.sh \
  --rollback --config /etc/ci-fleet-deployer/deployer.conf
```

The report's next action is `restore-host-policy-evidence-then-check`. Restore the prior reviewed `deployer.conf`, exact-head approval evidence, and checkpoint evidence through the same protected operator process that created them; do not copy secrets through the shell. Then verify read-only:

```bash
sudo ./scripts/install-deployer.sh \
  --check --config /etc/ci-fleet-deployer/deployer.conf
```

A cross-environment or cross-target in-place upgrade is rejected. Build a separately isolated host with its own identity and credential scope instead.

## Submit an approved deployment

Place one root-owned mode-`0600` request at `/var/lib/ci-fleet-deployer/request.conf`. It uses the same exact-head approval fields shown above. No secret values belong in the request. Before invoking the adapter, runtime revalidates host-role isolation and all protected approval, capability, checkpoint, and production evidence, then permanently consumes the semantic request identity; failed attempts require fresh approval. After success it records the deployed policy as the application-compatible rollback point and atomically moves the request to `last-request.conf`.

Then run on the deployer host; this changes the application target through its adapter:

```bash
sudo systemctl start ci-fleet-deployer.service
sudo systemctl status --no-pager ci-fleet-deployer.service
```

The runtime and read-only checks serialize on the same flock. Deploy writes a mode-`0600` active-operation marker and removes it on completion. While the adapter runs, `systemd-inhibit` blocks shutdown and sleep; the deploy service has an explicit 45-minute start/stop bound. Upgrade, rollback, drain, and uninstall refuse while that marker belongs to a live or bounded recent process. A root-owned stale marker older than the fixed one-hour recovery bound is removed only by a serialized mutating installer run. Unsafe or malformed stale state fails closed.

Health runs every five minutes and cleanup daily. Cleanup refuses while drained and is delegated to the application adapter because only application-owned code knows its exact resources. Core itself issues no Docker delete or prune command.

## Verification and reports

Verify unit definitions and state without reading protected content:

```bash
sudo systemd-analyze verify /etc/systemd/system/ci-fleet-deployer*.service \
  /etc/systemd/system/ci-fleet-deployer*.timer
sudo systemctl is-enabled ci-fleet-deployer-health.timer ci-fleet-deployer-cleanup.timer
sudo systemctl is-active ci-fleet-deployer-health.timer ci-fleet-deployer-cleanup.timer
sudo stat -c '%U:%G %a %n' \
  /etc/ci-fleet-deployer \
  /var/lib/ci-fleet-deployer \
  /var/lock/ci-fleet-deployer \
  /var/log/ci-fleet-deployer
```

Every installer exit emits one stable secret-free report. Example:

```text
REPORT action=install result=CHANGED environment=staging target=example-staging version=2222222222222222222222222222222222222222 digest=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa health=healthy changed=yes rollback_available=no next=run-check
```

`result` is one of `CHANGED`, `NO_CHANGE`, `BLOCKED`, or `FAILED`. `BLOCKED` means an operator prerequisite or safety gate must be resolved. `FAILED` means attempted candidate work failed. The report includes identifiers, never credential contents.

## Failure recovery

1. Do not delete the lock, state, or current release blindly.
2. Confirm whether `/var/lib/ci-fleet-deployer/active-operation` names a live process.
3. If live, wait or follow the application adapter's documented bounded recovery. Never interrupt an unknown deployment to force an upgrade.
4. If the installer reports safe stale state, rerun the same mutating command; it removes only a protected stale marker and current-core staging directory while holding the lock.
5. If candidate validation or activation failed, run `--check`; the previous active release remains selected.
6. If application health failed after an authorized deployment, run `--rollback`, then `--check`.
7. If machine recovery is required, use the checkpoint named in the protected evidence or rebuild the isolated host from reviewed inputs. Core does not operate the hypervisor.

## Uninstall

Uninstall changes the host, drains first by refusing any active deployment, disables timers, removes only ci-fleet deployer units and the current activation pointer, and is idempotent:

```bash
sudo ./scripts/install-deployer.sh \
  --uninstall --config /etc/ci-fleet-deployer/deployer.conf
```

It retains configuration, credential references and files, evidence, immutable releases, audit log, and last-known-good state. Review retention policy and destroy those items separately only after access revocation and audit requirements are satisfied. It never unregisters a runner, modifies GitHub, deletes an application repository, or removes unrelated Docker resources.

## Repository-only validation

The deterministic test uses an explicit alternate-root test mode and mocked Docker/systemd/network commands. It never changes the test machine's Docker daemon, systemd, GitHub state, or a deployment target:

```bash
scripts/test-install-deployer.sh
```

Prepared-host proof is still required before production use: exercise the reviewed adapter, Docker/systemd behavior, network policy, checkpoint recovery, immutable artifact verification, staging and production isolation, active-deployment drain, health, rollback, and scoped cleanup on a separately authorized isolated deployment host.
