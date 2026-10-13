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

Production remains separately gated during the controlled-migration phase. Installing a policy with `ENVIRONMENT=production` requires `PRODUCTION_AUTHORIZATION_EVIDENCE_PATH` to name an additional protected exact-target, exact-head, exact-artifact authorization record. The runtime still rejects production deployment and cleanup, and the installer rejects production rollback under the current accepted scope. Authorization evidence does not enable those operations. The field is rejected outside production.

Core systemd services remain trusted root orchestrators. Application adapters run in transient DynamicUser services with no capabilities, private network and devices, and a read-only filesystem. They receive a constrained Docker broker socket, never the backend socket. The broker requires a dedicated rootless daemon and checks the socket peer's host identity. The daemon account must have no sudo authority, privileged socket ACL, or access to protected recovery files. Core also rejects workloads, custom networks and volumes on a separate default host daemon. Operators must prove those prerequisites on the prepared host. Moving an existing rootful application into this rootless contract requires separately authorized application migration; the installer does not move it automatically.

Core writes and fsyncs policy, request, installed/LKG and deployed snapshot recovery copies beneath `/var/lib/ci-fleet-deployer/.adapter-recovery` before dispatch. The adapter and rootless daemon cannot read or write this root-owned mode-0700 store. Core also persists an outstanding adapter guard. It retires the completed journal after the corresponding guard is retired. Failed or uncertain mutations retain their protected records for operator recovery. SIGKILL, power loss, failed application mutations, or uncertain Docker responses retain the guard and block deployment, maintenance and drain pending operator recovery. Service termination alone cannot retire the guard; the protected broker journal must also prove completion. Read-only health and validation can retire a completed failed probe. Legacy guards without that journal require operator reconciliation. This implementation still requires adversarial prepared-host acceptance. See [the containment decision](DESIGN-DECISIONS.md#deployer-adapter-authority-and-recovery).

## Supported host and prerequisites

The installer supports Debian 12/13 and Ubuntu 22.04/24.04 with systemd. It checks a dedicated rootless Docker Engine at the canonical local `DOCKER_SOCKET`, synchronized time, disk capacity, DNS, HTTPS reachability, and Compose v2 when `REQUIRE_COMPOSE=1`. The configured network host is supplied to curl over standard input so a private endpoint is not placed in the process argument list.

It rejects:

- ordinary ci-fleet controller state or runner units, including the capacity sampler service and timer;
- any unrelated running or stopped Docker container;
- any unrelated custom Docker network or volume;
- unsafe owners, modes, symlinks, traversal, malformed configuration, or ambiguous identities.

Role admission checks installed unit paths, systemd unit-file inventory and all loaded units, including vendor, runtime and transient units. Controller, tester, status-receiver and ordinary runner units block installation, upgrade, repair, check, resume, drain, rollback and managed uninstall, plus runtime operations. Mutating modes repeat role admission under the shared role lock before adapter or journal recovery. An unavailable unit inventory fails admission. Emergency drain still works during a Docker outage, but cannot bypass mixed-role systemd state. Controller and status-receiver admission also reject deployer units from these inventories and block admission if a query fails.

`--check` makes no persistent configuration or application change. It takes the existing operation lock and runs health in a bounded transient service, with a temporary protected adapter guard that successful completion removes. An interrupted check can retain that guard and require serialized recovery. It creates a transient DynamicUser service and protected recovery records, but no persistent user, release, service/timer, image, container, registration, or GitHub state. Mutating modes require root.

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
| `/var/lib/ci-fleet-deployer/.adapter-recovery/` | root `0700` | durable dispatch and recovery records | retained |
| `/var/lib/ci-fleet-deployer-container-credentials/` | root `0711`, entries root/daemon group `0710`, files `0440` | stable read-only application bind sources | retained |
| `/var/lock/ci-fleet-deployer/` | root `0700` | flock serialization boundary | retained |
| `/var/log/ci-fleet-deployer/` | root `0700` | secret-free audit log | retained |
| `/etc/systemd/system/ci-fleet-deployer*` | root `0644` | deploy, health, cleanup, and drain units/timers | removed |

Releases, last-known-good state, audit records, configuration, credentials, and evidence are retained deliberately. Their retention or destruction is a separate operator decision.

## Application adapter contract

The adapter source is an application-owned root-only executable under `/etc/ci-fleet-deployer/adapters`. Its SHA-256 is pinned in `deployer.conf`. Core invokes exactly one positional operation:

```text
adapter validate
adapter health
adapter cleanup
adapter deploy
adapter rollback
```

`validate` must be non-mutating and must prove that the candidate policy is usable before core changes the active release. `health` returns zero only when the deployer and application-owned contract are healthy. `cleanup` may remove only resources carrying the application's exact deployer ownership identity; it must never run global prune or touch unrelated resources. `deploy` reads the active policy and request paths from the documented environment variables and owns application-specific staging, rollout, health, and rollback. `rollback` restores application state compatible with the recorded last-known-good core policy.

Every Docker resource must carry four labels from the active policy: `io.randomdevelopment.ci-fleet.role=deployer`, `io.randomdevelopment.ci-fleet.identity=<DEPLOYER_IDENTITY>`, `io.randomdevelopment.ci-fleet.environment=<ENVIRONMENT>` and `io.randomdevelopment.ci-fleet.target=<TARGET_ID>`. Names begin with `ci-fleet-deploy-<DEPLOYER_IDENTITY>-`. Host-role isolation rejects any unlabelled or differently labelled workload as unrelated, so unlabelled adapter resources break subsequent health, cleanup, deployment, and installer operations.

Operations have no interactive input. Zero means success; nonzero means failure. Direct installer validation and health calls are limited to two minutes. Scheduled health allows four minutes for prerequisite checks, the two-minute adapter and verified teardown; rollback is limited to 45 minutes. The adapter must avoid child processes that outlive those bounds, redact logs, and never print credential contents, authorization headers, cookies, private endpoints, or secret-manager responses. Core validates immutable identifiers and approval evidence; it cannot validate application-specific correctness.

The Docker API permits only the approved digest-pinned artifact, scoped internal bridge networks, local named volumes without driver options, and containers with positive limits capped at 2 GiB memory, two CPUs and 256 PIDs, private cgroups/namespaces, `--cap-drop ALL`, `--read-only` and `--security-opt no-new-privileges`. Host ports must bind loopback above port 1023. Build, exec, archive access, global pruning, arbitrary binds and alternate network attachments are rejected. Every image-declared volume destination must use a pre-created scoped labelled volume, so Docker cannot create anonymous volumes during container creation. The broker drops adapter-supplied registry authentication headers. Pulls use anonymous registry access; private artifacts must be preloaded by a separately authorized operator process. Adapters must use this contract rather than unrestricted Compose or host administration. Validation and health can only read; cleanup can only inspect and delete scoped resources.

Core supplies `CI_FLEET_DEPLOYER_CONFIG` and optional `CI_FLEET_DEPLOYER_REQUEST` as private credential copies. `CREDENTIAL_REF` in that config names the adapter-readable application credential. To bind it into a container, use `CI_FLEET_DEPLOYER_CONTAINER_CREDENTIAL` as the source and a read-only destination below `/run/secrets/`. Core materializes a durable copy under `/var/lib/ci-fleet-deployer-container-credentials`, keyed by target, daemon identity and credential contents. The directory and file remain root-owned; only the dedicated daemon group can traverse the entry and read its mode-0440 credential. Containers with a non-root image user can add numeric group zero in the rootless namespace. These sources survive adapter completion and reboot. Rotation creates a new immutable source and preserves earlier sources for existing containers and rollback. Before consuming approval, deployment validates credential references from both the deployed snapshot and retained last-known-good policy. Retain their protected credential files while either policy needs them. Uninstall retains them. An operator may retire a source only after proving no container or retained recovery point references it. External providers must materialize the approved credential into the protected host-local file before invocation; direct external-provider access is unsupported by the private-network sandbox.

Rollback must be atomic from the adapter's perspective: nonzero restores the pre-call application state; zero means rollback health is already verified. For rollback only, core exports `CI_FLEET_DEPLOYER_ROLLBACK_COMMIT`. The adapter atomically writes this private runtime file as its final successful step. The executor publishes the marker through the broker into the root-owned installer journal. The adapter never receives that journal path or write authority. Core stages the last-known-good core before dispatch and finalizes a committed rollback from the retained transaction after interruption.

## Prepare host-local files

All commands in this section run on the dedicated deployer host and change host-local state. They do not contact GitHub or deploy an application.

```bash
sudo install -d -o root -g root -m 0700 \
  /etc/ci-fleet-deployer \
  /etc/ci-fleet-deployer/adapters \
  /etc/ci-fleet-deployer/credentials \
  /etc/ci-fleet-deployer/evidence
adapter_sha=$(sha256sum ./application-adapter | cut -d' ' -f1)
sudo install -o root -g root -m 0700 ./application-adapter \
  "/etc/ci-fleet-deployer/adapters/application-adapter.$adapter_sha"
sudo install -o root -g root -m 0600 /dev/null \
  /etc/ci-fleet-deployer/credentials/application.credential
sudoedit /etc/ci-fleet-deployer/credentials/application.credential
```

`sudoedit` writes the credential without displaying it. Do not use a secret value in a command argument, shell history, environment variable, fixture, or log. An external secret manager may provision this protected regular file before dispatch. Core accepts only the approved file provider for contained adapter execution.

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
adapter_sha=$(sha256sum ./application-adapter | cut -d' ' -f1)
adapter_path=/etc/ci-fleet-deployer/adapters/application-adapter.$adapter_sha
sudo install -o root -g root -m 0600 /dev/null \
  /etc/ci-fleet-deployer/deployer.conf
{
  printf 'SCHEMA_VERSION=1\n'
  printf 'CORE_REF=%s\n' "$core_ref"
  printf 'ENVIRONMENT=staging\n'
  printf 'TARGET_ID=example-staging\n'
  printf 'DEPLOYER_IDENTITY=staging-deployer-01\n'
  printf 'DOCKER_SOCKET=/run/ci-fleet-deployer-engine/docker.sock\n'
  printf 'ADAPTER_PATH=%s\n' "$adapter_path"
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

For an external secret manager, provision the protected credential file before invocation, keep `CREDENTIAL_PROVIDER=file`, and set `CREDENTIAL_REF` to its canonical path. For GitHub Environment approval, use `APPROVAL_PROVIDER=github-environment` and add `APPROVAL_CAPABILITY_EVIDENCE_PATH=/etc/ci-fleet-deployer/evidence/github-capability.conf`. Production additionally requires `PRODUCTION_AUTHORIZATION_EVIDENCE_PATH=/etc/ci-fleet-deployer/evidence/production-authorization.conf`.

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

Upgrade after updating the exact core/source/artifact/approval/checkpoint fields; changes the host transactionally. Adapter bytes are immutable too: install changed bytes at a new digest-suffixed path, update `ADAPTER_PATH` and `ADAPTER_SHA256` together, and retain the old path while any deployed or last-known-good policy names it. Never overwrite the adapter at its existing path; pre-convergence verification intentionally requires those old bytes so rollback remains executable.

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

Place one root-owned mode-`0600` request at `/var/lib/ci-fleet-deployer/request.conf`. It uses the same exact-head approval fields shown above. No secret values belong in the request. Before invoking the adapter, runtime revalidates host-role isolation and all protected approval, capability, checkpoint, and production evidence, then permanently consumes the stable `APPROVAL_ID`; changing the approval time, approver, policy, or other request fields cannot reuse it. Failed attempts require a new approval ID. The durable replay ledger lives at `/var/log/ci-fleet-deployer/consumed-requests`, outside application state cleanup. New markers use `approval-` followed by the SHA-256 of the approval ID. Runtime retains stable markers from either ledger location. Legacy markers that hash the entire request cannot establish which approval IDs were consumed, so runtime preserves them and refuses deployment pending operator reconciliation from trusted evidence or checkpoint recovery. Never delete a replay ledger to bypass that refusal. Recovery restores every consumed marker, including earlier attempts. After success it restores protected active core state, records the deployed policy as the application-compatible rollback point, and durably moves the request to `last-request.conf` before writing the success audit record.

Then run on the deployer host; this changes the application target through its adapter:

```bash
sudo systemctl start ci-fleet-deployer.service
sudo systemctl status --no-pager ci-fleet-deployer.service
```

The runtime and read-only checks serialize on the same flock. Deploy writes a mode-`0600` active-operation marker and removes it on completion. The protected adapter guard is retired only after successful durable core/application publication. A failed contained deployment retains the guard for operator reconciliation. While the adapter runs, `systemd-inhibit` blocks shutdown and sleep; the deploy service has an explicit 45-minute start/stop bound. Upgrade, rollback, drain, and uninstall refuse while that marker belongs to a live or bounded recent process. A root-owned stale marker older than the fixed one-hour recovery bound is removed only by a serialized mutating installer run. Unsafe or malformed stale state fails closed.

Health runs every five minutes and cleanup daily. Their root services keep managed state and logs read-only except the protected adapter recovery store. They use protected temporary policy copies. Cleanup refuses while drained and is delegated to the application adapter because only application-owned code knows its exact resources. Core itself issues no Docker delete or prune command.

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

New installer journals live in `/opt/ci-fleet-deployer/.installer-transactions`, outside adapter-accessible paths. They retain the incumbent core, deployed snapshot, pending/completed requests, drain marker, consumed-approval ledger, and audit history. Candidate health preserves that incumbent snapshot even when health succeeds. Recovery also recognizes older journals under `/var/lib/ci-fleet-deployer`. A legacy journal without an operation kind and with a rollback marker requires operator recovery. Restore the checkpoint or establish the journal's actual operation from trusted evidence before classifying it. Core cannot infer that operation from the marker alone. Mixed-role host state blocks adapter and journal recovery before either can mutate managed state. Retain the journal and resolve the host-role conflict through the reviewed operator recovery procedure. Runtime refuses work until an interrupted installer journal is recovered. Only a rollback transaction can honor an application rollback commit marker.

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

## Prepared-host acceptance

PR #69 remains a draft and issue #22 remains open until separately authorized prepared-host acceptance is complete. Required CI and the alternate-root tests establish repository behavior. They do not establish application adapter correctness, credential scope, recoverable host checkpoints, or deployed application health.

The [historical synthetic proof](https://github.com/RandomDevelopment/ci-fleet/pull/69#issuecomment-5311189387) exercised real Debian 13, Docker, and systemd with credential-free `demo-a` and `demo-b` targets at commit `2ad48c75c1412a146086b7a18fb3027f423515f1`. It does not establish acceptance for the current PR head or an application-owned staging adapter and identity.

Before any host work, an operator must separately authorize an isolated test or staging host, its target, the reviewed adapter, credential provisioning, checkpoint creation and restoration, and the permitted deployment and failure exercises. Keep host inventory and protected evidence outside this public repository.

Before host activation, verify the selected constrained API, DynamicUser adapter and durable protected recovery design against both containment findings. Prove the dedicated rootless daemon user and group have no unrelated members or authority, and lack sudo, privileged socket access and recovery-store access. Passing repository tests do not establish these host properties.

Record the following evidence against the exact reviewed ci-fleet commit and adapter digest:

- Verify the supported OS, Docker and Compose versions, synchronized clock, disk and network prerequisites, and absence of ordinary runners, controller/receiver state, unrelated workloads, and shared production access.
- Verify root ownership and permissions for configuration, adapter, credentials, and evidence. Prove that the identity and credential scope belong only to the authorized target and cannot reach production or ordinary CI jobs.
- Produce exact-source and exact-artifact approval evidence through the chosen approval provider. Verify artifact provenance and digest through the application adapter. Prove missing, mismatched, and replayed approval cannot invoke deployment.
- Create a recoverable host checkpoint and prove its restoration through the operator's recovery procedure. A syntactically valid checkpoint evidence file alone is insufficient.
- Prove credential bind sources remain readable after adapter completion, container stop/start and host reboot. Verify rotated sources preserve existing containers and rollback, and reject source tampering.
- Run the documented one-command install, then an unchanged rerun and `--check`. Retain the secret-free reports, installed source identity, timer state, and service health. Exercise the adapter through real systemd units, including scheduled health and scoped cleanup.
- Exercise upgrade, repair, active-deployment refusal, drain, resume, bounded failure and interruption recovery, and rollback to verified application/core state. Prove audit history and consumed approvals survive recovery. Verify cleanup after success, failure, cancellation, and timeout preserves unrelated resources.
- Exercise repeated uninstall and verify that units and activation are removed while operator-owned configuration, credentials, releases, rollback state, and audit records remain as documented.

Retain detailed evidence privately. Post only the reviewed commit and adapter digest, pass/fail results, redacted reports, and an operator attestation in the PR. Keep the PR draft if any item lacks evidence. Production deployment, cleanup, and rollback require a separate accepted policy decision and authorization.

The next acceptance step is review of the stronger containment implementation, followed by separate operator authorization of the isolated test or staging host and application adapter, followed by host-local preparation and the documented one-command install and unchanged-rerun proof. Do not begin that step from repository CI or ordinary fleet runners.

Drain requires an existing managed deployer installation and rejects filesystem or effective systemd state for another host role before creating a drain marker. Runtime drain does not inventory Docker resources, so a daemon outage or resource drift cannot prevent its maintenance guard. Resume and deployment still require full workload admission. A committed application rollback restores and verifies its pinned core pointer and systemd units before retiring the recovery journal. The runtime verifies the inherited operation lock when it changes to the active revision.

Scoped cleanup may delete an old approved artifact by its full digest reference through `DELETE /images/<reference>`. Core records authorized pulls in a protected target ledger. It refuses deletion of the current, deployed or last-known-good artifact, forbids force and parent pruning, and relies on Docker to refuse images still referenced by any container. Successful deletion retires the ledger entry. Unrelated images and global prune remain unavailable.
