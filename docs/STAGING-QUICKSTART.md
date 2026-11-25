# First staging deployment

Deploy two versions of a fictional `example-app` to `example-staging` and verify
that rollback restores the first version. Use `ENVIRONMENT=staging` for this
preview. The app is a stateless HTTP service whose health response identifies
the running release. Bring your own reviewed adapter and test data for an
actual rollout.

Use this guide only after an operator has separately authorized the isolated
host, target, credential and artifact provisioning, checkpoint, deployments
and rollback exercise. Stop at any missing authorization or prerequisite.
Production deployment, cleanup and rollback remain disabled. The
[deployer host runbook](DEPLOYER-HOST.md) defines the full contract.

## 1. Identify the releases

In the application repository and registry, identify two tested release
commits and their published images. Reviewing those records is read-only.
Building or publishing an image belongs to the application's separate release
path. Ordinary CI validates the source with read-only permissions and has no
deployment credentials. See the [application deployment path](ARCHITECTURE.md#application-deployment-path).

For each release, record the full 40-character application `SOURCE_COMMIT`
and the registry `ARTIFACT_IMAGE` ending in `@sha256:<64 lowercase hex>`.
Use the first image as the baseline and the second as the candidate. The image
is the deployable input. Build it upstream from the app checkout, loose files
or Dockerfile. The deployer does not accept those inputs or arbitrary Compose
stacks. The broker pulls anonymously. For a private image, stop unless a
separately authorized process can preload the exact digest into the dedicated
rootless daemon. Keep registry credentials out of the adapter and request.

## 2. Prepare the isolated host

Run the host steps on dedicated Debian 12/13 or Ubuntu 22.04/24.04 with systemd.
Host preparation changes the machine. Have the authorized host operator provide
a dedicated rootless Docker Engine and its canonical local `DOCKER_SOCKET`.
The daemon account must have no sudo authority or access to privileged sockets or
protected deployer files. Check role isolation, required tools, synchronized
time, disk space, DNS and HTTPS per the [host prerequisites](DEPLOYER-HOST.md#supported-host-and-prerequisites).
Stop if the host has another fleet role or unrelated Docker containers, custom
networks or volumes.

On the deployer host, obtain the public ci-fleet checkout and pin it to the
full commit reviewed for this installation. Cloning and checking out change
only the checkout. The last two commands are read-only.

```bash
git clone https://github.com/RandomDevelopment/ci-fleet.git
cd ci-fleet
git checkout --detach REVIEWED_CI_FLEET_COMMIT
git rev-parse HEAD
git status --short
```

Use the verified HEAD as `CORE_REF`. Stop if it differs from the reviewed
commit or the checkout is dirty.

On the host, prepare the protected files using the
[host-local examples](DEPLOYER-HOST.md#prepare-host-local-files). These steps
write host-local state.

- Install the reviewed application adapter as a root-owned mode-`0700`
  executable. Set `ADAPTER_PATH` to its digest-suffixed path and
  `ADAPTER_SHA256` to the hash of those bytes. For this example, the adapter
  uses the constrained Docker broker API. Set `REQUIRE_COMPOSE=0`.
  `REQUIRE_COMPOSE=1` checks for the Compose v2 plugin on the host. It grants
  no extra adapter permissions. See the [adapter contract](DEPLOYER-HOST.md#application-adapter-contract).
- Set the target, deployer identity, core and adapter pins, socket and baseline
  source/image pair in root-owned mode-`0600`
  `/etc/ci-fleet-deployer/deployer.conf`. Use `CREDENTIAL_PROVIDER=file`,
  `CREDENTIAL_SCOPE=staging` and a `CREDENTIAL_REF` under the protected
  `/etc/ci-fleet-deployer/credentials/` directory. The authorized provider
  supplies the root-owned mode-`0600` credential file. Keep its contents out
  of policy, requests and logs.
- Have the authorized operator create a recoverable checkpoint before
  activation, then record its ID and UTC time in `CHECKPOINT_EVIDENCE_PATH`
  for this environment and target. The installer validates the record.
  Verify checkpoint recoverability through the operator's recovery procedure.
- Use `APPROVAL_PROVIDER=manual-exact-head` for this example. Have the
  approver record the exact environment, target, source commit and full image
  reference in `APPROVAL_EVIDENCE_PATH` with the approving identity, policy
  identity, unique approval ID and UTC time. Keep both evidence files
  root-owned with mode `0600`. Stop if evidence is absent or mismatched.

## 3. Install and verify the deployer

Run as an authorized operator from the pinned checkout on the deployer host.
Install writes the core release, active policy and systemd units, and enables
the health and cleanup timers. The adapter's `validate` and `health` must
succeed before the first app deployment. The check starts a transient health
service and makes no persistent application or configuration change on success.

```bash
sudo ./scripts/install-deployer.sh --install --config /etc/ci-fleet-deployer/deployer.conf
sudo ./scripts/install-deployer.sh --check --config /etc/ci-fleet-deployer/deployer.conf
```

Expect `result=CHANGED` for a fresh install. The check should report
`result=NO_CHANGE` and `health=healthy`.
Verify the timers are enabled and active with the read-only
[unit and state checks](DEPLOYER-HOST.md#verification-and-reports).
Stop on `BLOCKED`, `FAILED` or an unhealthy result.

## 4. Submit and verify the baseline

On the deployer host, use the protected approval record as the request. It
must match the active policy. If `/var/lib/ci-fleet-deployer/request.conf` is
already pending, stop and reconcile it before writing another one. The first
command writes a root-owned mode-`0600` request using the runbook's example
approval path. Use the path in your active policy if it differs. The second
command consumes the approval and deploys through the adapter. A GitHub merge
or passing CI run does not start the service.

```bash
sudo install -o root -g root -m 0600 \
  /etc/ci-fleet-deployer/evidence/approval.conf \
  /var/lib/ci-fleet-deployer/request.conf
sudo systemctl start ci-fleet-deployer.service
```

Verify on the host without changing app state. The request should move to
`last-request.conf` and `/var/log/ci-fleet-deployer/audit.log` should record
`result=success` for the expected environment, target, commit, digest,
approval and checkpoint. Check that the application health probe identifies
the baseline release. For `example-app`, read its loopback health endpoint
defined by the adapter and confirm the release in the response. A successful
service exit alone cannot establish application health. Stop on failed or
uncertain results and use the [recovery procedure](DEPLOYER-HOST.md#failure-recovery).
Preserve the audit and replay ledger. A consumed approval ID cannot be reused.

## 5. Deploy the candidate and verify rollback

On the same host, retain the baseline policy, evidence, adapter, credential
reference and image for recovery. Prepare the candidate's protected policy
and fresh approval ID as in step 2. Confirm the checkpoint still covers the
operation. Keep the same core pin, environment, target and deployer identity
for this exercise. The following command changes the active deployer policy
and retains the successfully deployed baseline as the rollback point.

```bash
sudo ./scripts/install-deployer.sh --upgrade --config /etc/ci-fleet-deployer/deployer.conf
```

Proceed only after a healthy upgrade reports `rollback_available=yes`.
Repeat step 4 with the candidate's matching request and verify its own audit
entry and health response. Stop for recovery if either verification fails.

From the pinned checkout, run the following command on the deployer host as
part of the approved exercise. It restores the retained core and application
state through the adapter.

```bash
sudo ./scripts/install-deployer.sh --rollback --config /etc/ci-fleet-deployer/deployer.conf
```

Expect `result=CHANGED`, `health=healthy` and a successful rollback audit for
the baseline digest. Confirm the application's health response identifies the
baseline again. Restore the baseline's operator-owned policy and evidence as
directed by `next=restore-host-policy-evidence-then-check`. That restoration
writes host-local files. Rerun the step 3 check and the read-only unit checks.
If rollback is unavailable, blocked, failed or uncertain, stop and follow the
[rollback and recovery runbook](DEPLOYER-HOST.md#install-check-repair-upgrade-drain-and-rollback).
Restoring a machine checkpoint is a separate operator action.

Retain the core and adapter pins, both source commits and image digests, the
target, checkpoint and approval IDs, installer reports, deployment and
rollback audit results, and application health results. Keep protected
records on the authorized side of the host boundary and share only redacted
evidence.
