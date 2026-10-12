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

## Reclaim empty networks inside reviewed Docker address pools

The cleanup contract in [Issue #81](https://github.com/RandomDevelopment/ci-fleet/issues/81)
requires an amendment. Ownership and expiry labels alone cannot recover address
capacity occupied by abandoned, unlabeled project Compose networks.

The existing cleanup timer may remove a zero-container network at least ten
minutes after creation when all of its
allocated subnets lie inside the controller's rendered default address pools.
Fleet labels and an expiry are not prerequisites for that pool-scoped removal.
Unknown, invalid, and future creation timestamps preserve the network.
The controller Compose networks and daemon bridge remain protected, even when
empty. Active networks and unlabeled networks outside the pools remain intact.
Removal uses individual network IDs and an exclusive maintenance gate shared
with the credential-free Docker socket proxy. The proxy mediates controller and
ordinary job reference mutations through completion, including caller
cancellation. Cleanup waits for idle work without holding the exclusive gate,
then checks existing created/stopped references before removal. Current-boot
markers preserve uncertain proxy mutations or cleanup removals across process
restarts; only a new host boot makes their old boot directories safe to clear.
The low-water gate remains enabled.

This policy assumes configured pools on an isolated fleet daemon contain
disposable job networks. An empty, intentionally reusable network inside a pool
can also be removed unless it belongs to the controller. Put persistent
infrastructure outside those job pools. Revisit this rule before allowing a
shared daemon whose persistent networks cannot satisfy that boundary. This
amendment changes neither pool capacity nor timer frequency, and it authorizes
no host deployment or cleanup during repository development.

This gate covers isolated ordinary Docker/Compose workloads using the proxy.
Host-root clients bypassing the proxy and asynchronous orchestrators cannot be
made atomic by this repository's cleanup loop. Cleanup defers on active or
unknown Swarm state and older/uncoordinated runnable containers. The independent
proxy survives controller-only restarts; proxy restarts may interrupt clients
that bind its socket file directly. Direct runners also mount the shared
directory and use `DOCKER_HOST` so their later jobs can reconnect.

## Admit disposable Docker storage retention

[Issue #111](https://github.com/RandomDevelopment/ci-fleet/issues/111) adds an
opt-in storage admission in reviewed schema-v3 controller desired state.
`docker_storage_retention.exclusive_fleet_daemon: true` declares ownership of
disposable images and the default builder cache across an isolated ordinary-CI
daemon. It permits age-bounded image removal and unused default-builder cache
reclamation under the installer and proxy maintenance locks. It does not admit
shared application daemons or authorize deletion by project/name prefix.

The cleanup timer now runs hourly. The unchanged 80% upgrade gate remains a
backstop. Storage cleanup starts at 70%, targets 65%, preserves seven days of
images/cache, retains a 10 GiB cache reserve, and limits a pass to 50 images and
two minutes of storage removal. Installed and checkpointed controller/runner
images remain protected. Protected or recent storage can still exceed capacity.
Admission therefore requires measured growth and sufficient disk capacity.

Workload diagnostics report terminal CI attempts without stopping their
containers. Future run-based removal requires independently verified task/shard
ownership and a concurrent lifecycle contract. Review this admission before
introducing another daemon owner or builder driver. Remove the admission before
sharing a daemon. The complete retention, uncertainty, bootstrap, and live proof
contract is in [host maintenance](HOST-MAINTENANCE.md#docker-storage-retention).
No host activation or live removal is authorized by repository implementation.

## Independent review of configuration candidates

The opt-in trust boundary prepared under [issue #108](https://github.com/RandomDevelopment/ci-fleet/issues/108)
keeps review/check requirements in root-owned policy separate from candidate
Git-authored configuration. It is an explicit exception to the host-file rule:
numeric repository, workflow, and reviewer trust anchors belong outside the
repository whose changes they accept. They are not application allowlists,
capacity policy, credentials, or infrastructure inventory. See
[the acceptance and recovery contract](CONFIGURATION-APPROVAL.md).

This implementation requires independent exact-head formal review, exact-applied
commit workflow/job success, and equality of the reviewed and applied trees.
Existing root-owned installation/checkpoints remain the trusted recovery
baseline; an enabled host cannot restore a manager that drops the gate.
Host-root compromise, trusted-engine compromise, and account takeover of an
allowed reviewer are outside this check's boundary. No deployment, account
permission change, host activation, or production approval is granted here.

## Project name

**Status:** accepted on 2026-08-16; retain `ci-fleet`.

`ci-fleet` names the mature shared subsystem: a portable fleet of ephemeral CI workers. Tester hosts and production deployers are deliberately separate roles, credentials, services, and trust boundaries; naming the repository after all three would suggest an integration the architecture forbids. A rename would also invalidate or migrate pinned action references, image and Compose identities, environment variables, private configuration, App and runner-group names, installed hosts, links, clones, and operator procedures before either additional role is production-ready. That churn has no current operating benefit.

The evaluated alternatives were `delivery-fleet`, `build-test-deploy-fleet`, and `software-delivery-fleet`. All three were available in the `RandomDevelopment` GitHub namespace on 2026-08-16. GitHub name search found no exact `build-test-deploy-fleet` or `software-delivery-fleet` repository; `delivery-fleet` is used by unrelated logistics projects and is ambiguous. The current name remains shorter, searchable, integration-neutral, and accurate for the repository's implemented production boundary.

Reconsider only after tester and deployer roles are implemented, independently proven, and operators demonstrably need one public umbrella identity. If that happens, write a new ADR first and stage the migration: reserve the name; inventory every public and private consumer; preserve GitHub redirects and compatibility aliases; update docs, badges, action references, module/package paths, images, labels, Compose projects, environment variables, configuration templates, Apps, runner groups, scale sets, installed hosts, scripts, and prompts; canary new references while old references remain valid; then deprecate in a documented release window. Roll back by restoring the old canonical name and aliases before removing any compatibility path. No repository, image, App, runner group, host, or external consumer is renamed by this decision.

## Authenticate the bootstrap callback through SSH

The GitHub bootstrap portal uses an operator-owned Unix socket and an
authenticated SSH local forward. A plain loopback listener would let other
target-host users read its nonce and consume its one-shot callback. The nonce
now remains in a root-private file and the socket permits only the explicitly
trusted operator UID. Registration URLs and printed socket paths contain no
authentication value. Temporary portal directories disappear when bootstrap
exits. No target-host TCP listener or firewall rule is needed.

The management device and its SSH account remain trusted. A forward on a shared
device exposes the portal to that device's other users. This choice requires an
SSH client with Unix-socket forwarding and one extra terminal session, including
when the management device is a phone. Root-console users must select the
trusted SSH account's UID. Live phone acceptance remains tracked in issue #27.
Replace this transport only after an equally authenticated portal has proved
nonce confidentiality, one-shot behavior, and cleanup without credential
transport through chat or process arguments.

## Confirm App visibility in GitHub's owner UI

GitHub's documented [`GET /app` response](https://docs.github.com/en/rest/apps/apps#get-the-authenticated-app)
has no App visibility field. Bootstrap requests `public: false` in the manifest
and requires the owner to confirm **Only on this account** before creation or
installation. It verifies the API-supported ownership, permission, and selected
repository fields and rejects an explicitly public response if GitHub reports
one. Missing visibility is never described as machine-verified private status.

An owner can broaden App distribution in GitHub outside this workflow. That
could permit installations in additional accounts and expand the effect of a
leaked private key. Live setup and periodic owner checks must retain private
distribution. Issue #27 includes live acceptance of this requirement. Replace
this manual check when GitHub exposes a supported, authoritative visibility
field that bootstrap can verify without browser credentials.
