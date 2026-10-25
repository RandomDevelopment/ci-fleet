# Git-authored controller desired state

Optional `docker_storage_retention: {"exclusive_fleet_daemon": true}` admits
bounded image/default-builder cache maintenance on an isolated disposable-CI
daemon. Activate a compatible engine before adding the policy. See the
[retention and activation contract](HOST-MAINTENANCE.md#docker-storage-retention).

Schema v3 makes a reviewed private configuration repository the authority for controller routing, capacity, lifecycle, and engine revision within the fixed `RandomDevelopment/ci-fleet` public engine repository. A configuration-only change cannot redirect root execution to another repository. The target host keeps credentials and rendered runtime state outside Git.

## Responsibility boundary

| Location | Owns |
| --- | --- |
| Public ci-fleet repository | Schema, validator, renderer, installer, controller, maintenance, and fictional examples |
| Private configuration repository | Real project allowlists, runner pools, logical controller IDs, capacity budgets, controller state, resource limits, and pinned engine revisions |
| Root-owned host files | GitHub App identity, private key path, rendered environment, checkpoints, and installation state |
| Application repository | Independent CI tasks and shards; never fleet size or controller identity |

Host addresses, VM IDs, storage names, backup identifiers, SSH details, tokens, private keys, and rendered `.env` files are rejected from the Git-authored configuration.

Private policy has one explicit Docker address exception. Reviewed
`docker_network_policy.default_address_pools[].base` CIDRs define allocation
pools. The optional `docker_network_policy.default_bridge_cidr` defines the
Docker default bridge gateway/interface and prefix. These values are capacity
policy, not host identity, credentials, or routable service endpoints. The
exception does not admit other infrastructure addresses or runtime details.

## Schema v3

Each runner pool declares:

- a logical GitHub runner group;
- stable shared routing labels;
- explicitly allowed repositories;
- `public_repositories: false`;
- an infrastructure `capacity_budget`;
- `job_submission_policy: all-independent-jobs`.

Each controller has a unique object key and declares:

- its pool and logical location;
- `active`, `drained`, or `disabled` state;
- a unique scale-set name;
- `experimental`, `stable`, or `retiring` lifecycle;
- a full pinned ci-fleet engine commit;
- a zero managed minimum and reviewed maximum runner capacity;
- CPU cores and memory per ephemeral runner;
- reviewed Docker default-address pools, an optional default-bridge gateway CIDR,
  a positive per-runner network bound, and a reserved subnet count.

Active and drained controllers reserve their configured maximum against the pool budget. A drained controller has zero effective runtime capacity but keeps its reservation, so an undrain cannot silently overcommit the pool. Disabled controllers reserve no capacity.

The Docker network policy uses IPv4 CIDR `base` values and a Docker subnet
prefix `size` no longer than `/29`, which leaves enough addresses for an
ordinary Compose network. A policy may declare at most 64 pools. Validation
rejects malformed or overlapping pools, allocation prefixes broader than their
base, and active or drained policies with fewer subnets than
`max_runners * networks_per_runner + reserve_subnets + 1`. The final subnet is reserved for the
persistent controller Compose network. Disabled controllers do not reserve
runner subnet capacity, but their retained policy must still cover the reserve
and controller network. `default_bridge_cidr`, when present, is an IPv4
interface address and prefix such as `192.0.2.1/28`, not a canonical network
base. Its address must be a usable gateway, its prefix must leave room for
containers, and its subnet must not overlap any default-address pool. After
excluding the network, broadcast, and bridge gateway addresses, it must provide
at least `max_runners + reserve_subnets` container addresses. Size the
default bridge for concurrent containers attached without an explicit network.
Size `default_address_pools` separately for job and controller networks. The
existing `max_runners * networks_per_runner + reserve_subnets + 1` arithmetic
applies only to those pools.

Real Docker network values belong only in the private desired-state repository
under the narrow exception above. Public examples use RFC 5737 documentation
ranges, which strict validation rejects until the operator supplies reviewed
operational CIDRs.

`docker_network_policy` is optional for first-install staging and for upgrades
from older schema-v3 engines whose exact-key validator does not recognize it.
New controllers first install without policy, then establish active runtime
evidence as described below. Upgrade an existing image-enforced controller
through separately integrated changes.
First stage the compatible target engine's record with
`docker_network_policy_config: true` in `next-engine-rollout-evidence.json`, while
keeping the incumbent engine and active evidence. Later select that engine and
promote the matching pending record unchanged into `engine-rollout-evidence.json`.
Remove the promoted pending record and remove the sidecar when empty. After
routine reconciliation verifies the selected runtime, add the network policy in
a later commit without changing the engine or active evidence. A record staged
in the selection commit cannot satisfy this predecessor gate.

Image-free manager bootstrap instead retains incumbent active evidence and the
matching pending records through manager staging and image-policy introduction.
After runtime verification, promote evidence alone without changing fleet state.
Every staged controller needs matching predecessor pending evidence, even with
no optional status/network block and all-false capability flags.
For an established image-free fleet, integrate missing explicit incumbent runtime
evidence first in an evidence-only commit, with the entire fleet unchanged and all
existing records retained. Base it on observed activation and exact source review,
including an explicit all-false record when appropriate. Next add reviewed target
pending records in a separate evidence-only commit, retaining the fleet and prior
records. Strict validation distinguishes these additions from manager staging.
Stage manager pins only after that pending evidence is integrated, then retain both
maps through image introduction. An absent record is insufficient; the staged manager
pin cannot identify the incumbent runtime.

Transition validation requires prior integrated active capability evidence when
introducing network policy or `default_bridge_cidr`. Retained fields need matching
current evidence. The selected engine must advertise `docker_network_policy_config`,
`docker_network_policy_adapter`, and, when the bridge field is present,
`docker_default_bridge_cidr_config`. Remove unsupported fields before selecting
an older engine through its own reviewed pending-evidence sequence.

This accepted phase permits the executable policy stage to own Docker's
`default-address-pools` key and, only when configured, its `bip` key. The stage
records each prior key's presence and value in the existing root-only checkpoint,
then drains the controller and managed runners. It rejects incompatible daemon
authority such as `fixed-cidr` before the drain. The transaction writes only the
managed keys, restarts Docker, verifies the effective default-bridge subnet and
gateway when `bip` is owned, runs the bounded capacity probe, resumes the
controller, and checks health against the candidate rendered environment.

On interruption or failure, rollback must restore the managed keys and prior
rendered environment from the checkpoint, restart Docker, verify effective
network state, resume the prior controller state, and verify prior health. A
failed rollback must retain its recovery checkpoint. Rollout requires exact-head
CI and proof for the reviewed
engine and desired-state commits before any host mutation. No deployment occurs
in this PR. This scope does not create or remove networks, prune resources,
alter controller scale, change downstream-consumer labels, or authorize
application production deployment.

Managed prewarmed runners are not currently supported: `min_runners` is fixed at zero in schema, semantic validation, rendering, and preflight. This keeps idle privileged workers absent and prevents reviewed configuration from passing validation only to fail host adoption.

The authoritative fictional contract is in [`templates/config-repository`](../templates/config-repository/README.md).

## Application parallelism rule

Application workflows submit every independent task and shard. They must not use GitHub Actions `strategy.max-parallel` to represent how many fleet workers exist. GitHub queues excess jobs; private infrastructure policy decides how many run simultaneously.

An application may use a concurrency limit only for a documented external-system restriction such as a vendor rate limit or a single-writer test fixture. That exception must not be based on current worker count.

## Prepare host-local identity

Create the root-owned file once:

```bash
sudo install -d -m 0700 /etc/ci-fleet/secrets
sudo install -m 0600 host/host.env.example /etc/ci-fleet/host.env
sudo install -m 0600 /secure/source/github-app.pem /etc/ci-fleet/secrets/github-app.pem
```

Edit `/etc/ci-fleet/host.env` locally. Managed installs intentionally reject alternate host-config paths so scheduled drift checks always verify the same identity file. It contains the GitHub App client ID, installation ID, private-key path, runner TTL, and optional host-local APT cache settings. Both the host environment and PEM must be root-owned mode `0600`, and the TTL must be at least one hour. Neither file is committed, and the PEM is never printed by the installer.

[APT cache support](APT-CACHE.md) is disabled by default, requires the selected engine's `apt_cache` capability, and keeps endpoints outside Git-authored desired state.

GitHub App and runner-group creation remain the bootstrap responsibility tracked by [issue #27](https://github.com/RandomDevelopment/ci-fleet/issues/27). The installer fails closed when those prerequisites are absent.

## Install a fresh controller

The managed installer supports Debian 12 or newer. Before reading configuration or changing the host, it verifies Docker Engine and Compose v2, Git, curl, jq, the system CA bundle, direct Docker-socket access, and that the Docker filesystem is below the documented 80% warning threshold. It rejects alternate Docker endpoints and contexts, then pins every lifecycle command to the verified local Unix socket. Rollback and uninstall require only their recovery tools, not Git, tar, cmp, host-release metadata, CA bootstrap, or capacity checks.

Use a reviewed checkout of ci-fleet on the target Linux Docker machine.

For a new controller, initialize with a reviewed merged engine and its reviewed
images, then remove generated `docker_network_policy`, including any bridge
field, before the initial configuration commit. Omit optional status reporting
and storage retention. Keep `managed_images` present. A new installation starts
with an empty active evidence map; an existing fleet retains other controllers'
records and omits only this new controller's record. Validate, review and merge
against the actual prior integrated state.
Source capability declarations cannot establish activation before an instance
exists. This first-install sequence does not permit an existing installation to
discard its history or incumbent evidence.

```bash
sudo ./scripts/install-worker-controller.sh \
  --install \
  --config-repo example-org/example-fleet-config \
  --ref 1111111111111111111111111111111111111111 \
  --controller example-ci-01
```

`--ref` must be a full configuration commit SHA. The installer never follows a moving branch. For a private remote repository, configure a narrowly scoped read-only Git credential on the host before running the command. Do not embed credentials in the URL or command line. A local pinned Git checkout is also accepted.

Verify the exact same initial configuration and active runtime:

```bash
sudo ./scripts/install-worker-controller.sh --check \
  --config-repo example-org/example-fleet-config \
  --ref 1111111111111111111111111111111111111111 \
  --controller example-ci-01
```

Require `CHECK_OK`. `CHECK_STAGED` only verifies an incumbent during manager
staging. After actual runtime verification, review capability declarations and
implementation at the exact selected engine SHA and integrate this controller's
active record in a separate evidence-only commit, leaving `fleet.json` unchanged.
In a later commit, add reviewed operational network policy and optional bridge
values using the previously integrated active record and the same engine. Review,
validate and merge each commit against its real predecessor before approved
reconciliation. Source review alone is insufficient, and historical example
pins without the required capabilities cannot support a capability claim.

The installer:

1. fetches only the requested configuration commit;
2. validates the complete schema and capacity relationships;
3. selects exactly one logical controller;
4. renders `/etc/ci-fleet/ci-fleet.env` without secret values;
5. fetches and verifies the pinned public engine commit;
6. validates candidate Compose before checkpoint capture for opaque runtime sources;
7. creates a root-only controller checkpoint and prepares and verifies images under temporary tags. Portable sources then validate Compose using both verified image bindings. Post-build host checks must pass;
8. drains the current controller and waits for every managed runner to finish, including orphaned runners left after a stopped or crashed controller;
9. runs managed workload-quiescence preflight, then publishes verified canonical tags within checkpoint recovery coverage;
10. installs health, cleanup, and pinned-state drift unit definitions;
11. starts the controller only when its desired state is active and verifies runtime health;
12. atomically records redacted installation state, then enables the maintenance timers.

A successful second `--install` run reports `NO_CHANGE` and performs no unnecessary replacement. Engine upgrades advance the runtime release and maintenance installer-manager together. A deliberate runtime downgrade retains a newer compatible manager so a later reviewed upgrade can still be rendered and activated; maintenance units always come from the selected runtime. Rollback restores both.

## Adopt an existing controller

Use adoption for a manually installed controller:

```bash
sudo ./scripts/install-worker-controller.sh \
  --adopt \
  --config-repo example-org/example-fleet-config \
  --ref 1111111111111111111111111111111111111111 \
  --controller example-ci-01
```

If `/etc/ci-fleet/host.env` does not exist, adoption extracts only the approved host identity fields from the existing root-owned `/etc/ci-fleet/ci-fleet.env`. It never copies project settings or secret values into Git. The running controller is paused, existing managed runners are allowed to finish, and activation proceeds only after the host is idle. Adoption also requires trusted complete source matching the incumbent's engine revision. It preserves historical in-place source before candidate installation and uses incumbent-compatible Compose for drain and recovery. A different portable candidate cannot supply missing opaque recovery context. Missing, mismatched or unsafe source refuses before drain; prepare trusted pinned source or follow reviewed reinstall recovery.

## Check, upgrade, roll back, and remove

```bash
# Read-only comparison with one pinned configuration commit
sudo ./scripts/install-worker-controller.sh --check \
  --config-repo example-org/example-fleet-config \
  --ref 1111111111111111111111111111111111111111 \
  --controller example-ci-01

# Apply a newer reviewed configuration and/or pinned engine
sudo ./scripts/install-worker-controller.sh --upgrade \
  --config-repo example-org/example-fleet-config \
  --ref 2222222222222222222222222222222222222222 \
  --controller example-ci-01

# Restore the latest root-only controller checkpoint
sudo ./scripts/install-worker-controller.sh --rollback

# Drain and remove controller services and rendered state
sudo ./scripts/install-worker-controller.sh --uninstall
```

Uninstall removes the controller, timers, rendered environment, and active installation marker. It deliberately preserves `/etc/ci-fleet/host.env`, the secrets directory, and checkpoints so credential destruction and audit retention remain explicit operator decisions.

No mode uses global Docker prune or removes unrelated workloads.

## Drift and reviewed updates

`ci-fleet-drift.timer` checks the host every fifteen minutes against the exact configuration SHA recorded at installation. It detects host edits, missing or stale runtime and installer-manager releases, runtime-state mismatch, altered metadata, and missing maintenance timers without applying changes.

The host does not automatically follow or execute a moving branch. A new configuration becomes effective only when an operator or authorized external controller supplies its reviewed full commit SHA to `--upgrade`. Automatic dispatchers may watch a protected branch and invoke that exact command after merge, using read-only repository contents permission; their identity must remain host-side and unavailable to job runners.

## Remote reconciliation

Hosts can opt into [independent configuration review verification](CONFIGURATION-APPROVAL.md).
A root-owned policy outside the candidate repository pins the validation workflow,
required jobs, and reviewer identities. The trusted engine checks the applied
commit and reviewed tree before reconciliation or direct installer upgrades.
An enabled gate rejects unavailable evidence and engine downgrades that remove
verification. Without the separately provisioned policy/activation marker, the
existing content validation does not establish GitHub review or hosted CI results.

`ci-fleet-reconcile.timer` runs every five minutes to fetch the desired configuration from the private desired-state repository and apply it if a newer reviewed commit is available. Authentication uses the existing GitHub App identity — no PAT, no SSH, no inbound management port.

How it works:

1. The timer invokes `scripts/remote-reconcile.sh`.
2. The script generates a short-lived GitHub App installation token using the existing private key on the host (openssl + curl, no new dependencies).
3. It fetches the default-branch HEAD of the desired-state repository over authenticated HTTPS.
4. The fetched commit is resolved to an immutable SHA and compared with the installed SHA.
5. If unchanged, a drift check confirms convergence (NO_CHANGE).
6. If changed, the new configuration is validated (schema, secret scan, tree completeness) before any mutation.
7. On success, the last-known-good state is updated and the controller runs with the new configuration.
8. An explicit installer rejection before transaction start preserves the installed configuration. Failures inside a transaction use the installer's checkpoint recovery; recovery must be verified before reporting rollback success.
9. State is recorded at `/var/lib/ci-fleet/reconcile/state.json` with desired commit, applied commit, health, failure description, and controlled rejection phase/reason when available.

Bounded retries up to three attempts handle transient fetch or API failures.

### Rejected upgrades and uncertain recovery

The installer reports an explicit rejection through its private transaction-result
descriptor only when `die` stops it during initial read-only admission. Once the
command, daemon and disk checks pass, the conservative guard starts before lock
creation, adoption's host-file extraction, configuration work, shared runtime/manager
preparation, or retained-checkpoint repair. Later failures remain uncertain,
including paths that ultimately make no changes.
The unchanged Docker disk gate rejects utilization at or above 80% before drain
or activation. Reconciliation records `status=failed`,
`transaction_phase=pre_transaction`, and `failure_reason=docker_disk_capacity`,
retains the installed/applied commit, and emits `REJECTED` instead of
`ROLLBACK_UNVERIFIED`. It leaves health `unknown` because rejection does not prove
the incumbent controller is healthy. Other explicit early rejections use the
controlled `installer_precondition` reason. Raw installer stderr is not copied
into these durable rejection fields or their message.

An empty, malformed, unsupported, or contradictory result is still unknown.
Process interruption and failures after transaction start cannot claim an early
rejection. Verified rollback retains its existing health check; unverified
rollback continues to report `ROLLBACK_UNVERIFIED`. No outer recovery is attempted
from an uncertain installer outcome. The additive phase/reason fields are empty
when this rejection evidence is unavailable; empty fields do not prove that no
transaction ran.

Both the executing installer and reconciler must support this result contract.
Selecting a candidate engine does not replace the older manager already running
an upgrade. Missing results from an older installer remain unknown until the
compatible manager is adopted through the reviewed upgrade procedure.

Rejection and uncertain recovery both retain exit code 3. The systemd service
accepts that code so its five-minute timer can retry; a successful unit result
therefore does not establish successful reconciliation. Inspect the durable
`status`, desired/applied commits and rejection fields, together with the service
journal. These observations do not authorize cleanup or bypass any admission gate.

Prerequisites:

- The controller's GitHub App must have `contents: read` permission and be authorized for the desired-state repository — see [GitHub App setup](GITHUB-APP-SETUP.md).
- `host.env` must contain `CI_FLEET_GITHUB_APP_CLIENT_ID`, `CI_FLEET_GITHUB_APP_INSTALLATION_ID`, and `CI_FLEET_GITHUB_APP_PRIVATE_KEY_FILE`.

Manual invocation:

```bash
# Check-only — validate without applying
sudo /opt/ci-fleet/manager/current/scripts/remote-reconcile.sh --check-only

# Full reconcile
sudo /opt/ci-fleet/manager/current/scripts/remote-reconcile.sh

# No-op — log what would be done
sudo /opt/ci-fleet/manager/current/scripts/remote-reconcile.sh --no-op
```

## Drain and retirement

1. Merge a private configuration change setting the controller to `drained`.
2. Run `--upgrade` with that reviewed commit.
3. Verify zero managed runners and zero effective capacity.
4. Remove the controller's GitHub registration and revoke its host identity when it will not return.
5. Set it to `disabled` or remove its declaration in a later reviewed change.
6. Delete or repurpose the machine according to the installation's infrastructure policy.

Legacy project-specific hosts remain until CI, promotion, and deployment no longer reference them. Deleting or replacing a generic controller does not require application workflow changes because projects route through shared labels.

## Failure and recovery behavior

Before mutation, the installer records the prior rendered environment, installation metadata, runtime release, installer-manager release, and maintenance unit/timer state under `/var/lib/ci-fleet/checkpoints`. Each checkpoint is staged and atomically renamed with a completion marker; rollback ignores partial staging directories. Portable preparation hydrates verified runtime IDs before candidate Compose validation. Required builds use temporary tags after checkpoint creation and before drain. Both roles must pass the runtime source's managed-image identity contract, source and platform checks, and post-build host checks must pass, before drain; canonical tags are published only after a successful drain within checkpoint recovery coverage. Failed preparation or drain preserves incumbent tags and the active controller; scoped candidate image or layer artifacts may remain. A failed activation or health check drains the candidate, restores those artifacts, restarts the prior controller only when no managed runner is active, and verifies prior-release health before reporting rollback success. A host-local installer lock serializes every check and mutation. Runtime and installer-manager releases are staged on their respective target filesystems and renamed atomically so a failed copy cannot masquerade as an installed immutable release.

Installer checkpoints and machine backups serve different failure classes. A checkpoint rolls back a single failed reconciliation. Recoverability of the machine itself is governed by each host's declared failure boundary (see [Adding a host](ADDING-A-HOST.md)): a disposable controller needs no machine backup at all — recovery is rebuilding from reviewed Git-authored desired state — while a non-disposable host follows its own documented local backup policy. Rollback is a bounded convenience, not a guarantee that every catastrophic or major-version interruption leaves the controller operational. The installer repairs only canonical, digest-valid artifacts from their pinned revisions; otherwise it fails closed and reports that an operator must recover or reinstall from reviewed desired state rather than executing untrusted rollback material.

While reconciliation is executing, rollback preserves its live service definition
and timeout settings at every daemon reload. An ordered transient systemd service
restores the checkpointed reconcile unit, or removes it if absent from the
checkpoint, after reconciliation exits. Its ordering blocks the next reconciliation
until finalization completes. It uses the installer lock and refuses
to overwrite a unit changed by a later installer. Dispatch failure makes rollback
fail. Deferred execution failure retains its staged files and reports an error
in the transient service journal. This deferred step has a fifteen-minute budget;
completion after process death or reboot is not guaranteed.

Monitoring thresholds, heartbeat endpoints, and backup hooks are host-local operational facts, not fleet desired state. Keep them in the protected file documented by [Fleet health monitoring](HEALTH-MONITORING.md); the installer preserves that file across upgrades and rollback.

This contract implements the engine portions of [issue #32](https://github.com/RandomDevelopment/ci-fleet/issues/32) and integrates the installer, documentation, phone-first bootstrap, and capacity work tracked by [#21](https://github.com/RandomDevelopment/ci-fleet/issues/21), [#24](https://github.com/RandomDevelopment/ci-fleet/issues/24), [#27](https://github.com/RandomDevelopment/ci-fleet/issues/27), and [#30](https://github.com/RandomDevelopment/ci-fleet/issues/30).


## Managed image scalar semantics and predecessor evidence

The runtime source's `portable_managed_image_ids` capability makes existing
`managed_images[engine][architecture][role]` scalars original config digests.
Older source entries remain opaque Docker IDs, even under a newer manager.
The shape stays unchanged. Root preparation derives the two rendered runtime
IDs and protected proof described in [managed images](MANAGED-IMAGES.md).

Stage a nonempty `next-engine-rollout-evidence.json` in an earlier integrated
commit. Ordinary selection promotes its matching record unchanged. Image-free
manager bootstrap retains active and pending evidence through image introduction,
then promotes evidence alone after runtime verification. Both installer and
reconciler read the committed current and immediate first-parent fleet/evidence.
Missing history fails. Keep protected installed-ref and bootstrap LKG exceptions
limited to their retained identity checks.
