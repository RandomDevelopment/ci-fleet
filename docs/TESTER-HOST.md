# Test-environment host

Status: repository implementation complete; prepared isolated-host acceptance remains required.

This role runs persistent or expiring deployed application test environments. It is deliberately separate from ephemeral CI workers and production deployers. It cannot register ordinary CI runners or promote production releases.

## Boundary

A tester host accepts only:

- a reviewed `ci-fleet` source commit for the tester service;
- root-owned host configuration and environment declarations;
- application images addressed by an immutable `sha256` digest;
- environment secrets stored below that environment's fixed host-local secret directory.

It rejects mutable images, public port binds, host bind mounts, external/unscoped Docker resources, custom volume drivers/options, privileged containers, added capabilities, host or shared namespaces, Docker API access, global container names, and credentials outside the environment secret boundary. Compose environment variables, env files, and configs are forbidden credential channels; use only fixed mode-`0600` Compose secrets. Every service must be read-only, drop all capabilities, and set `no-new-privileges=true`. The validated rendered Compose model is copied into protected runtime state before activation, so partial starts remain tracked and later cleanup does not depend on a mutable or deleted source definition. Test identity, networks, storage, routes, domains, data, and credentials must have no production authority. Host/network isolation is an external acceptance gate, not something this repository-only change can prove.

## Prepare host-local configuration

The installer supports only `/etc/ci-fleet-tester/tester.env`. Create it and all protected directories as root; never commit them:

```bash
sudo install -d -m 0700 \
  /etc/ci-fleet-tester \
  /etc/ci-fleet-tester/environments \
  /etc/ci-fleet-tester/definitions \
  /etc/ci-fleet-tester/secrets
sudo install -m 0600 /dev/null /etc/ci-fleet-tester/tester.env
```

Example non-secret settings:

```text
CI_FLEET_TESTER_DEFAULT_TTL_SECONDS=86400
CI_FLEET_TESTER_MAX_ENVIRONMENTS=20
CI_FLEET_TESTER_DISK_WARN_PERCENT=80
CI_FLEET_TESTER_NETWORK_PROBE_HOST=tester-probe.invalid
CI_FLEET_TESTER_HTTPS_PROBE_URL=https://tester-probe.invalid/health
CI_FLEET_TESTER_ISOLATION_ACK=test-only-no-production-authority
```

Set both probe values to a test-only host whose DNS resolution and HTTPS HEAD response exercise the intended local proxy path without carrying credentials. The acknowledgement is required but is not proof: an authorized operator must still verify that the prepared host has no production identity or network authority.

For environment `example-preview`, create root-owned mode-`0600` `/etc/ci-fleet-tester/environments/example-preview.env`:

```text
CI_FLEET_TESTER_PROJECT=example-project
CI_FLEET_TESTER_OWNER=example-owner
CI_FLEET_TESTER_COMPOSE_FILE=/etc/ci-fleet-tester/definitions/example-preview.yaml
CI_FLEET_TESTER_EXPIRES_AT=REVIEWED_FUTURE_UNIX_TIME
CI_FLEET_TESTER_ROUTE_SERVICE=web
CI_FLEET_TESTER_ROUTE_PORT=18080
```

The Compose file is root-owned mode `0644` and may contain no credential value. Each image must use `registry/path@sha256:REVIEWED_64_HEX_DIGEST`. Exactly one route is published, on loopback only, at the declared port. Compose-generated network and volume names must remain below `ci-fleet-test-<environment>_...`; explicit external names are rejected.

If credentials are required, create `/etc/ci-fleet-tester/secrets/example-preview` as root-owned mode `0700`, put only test-scope regular files there as root-owned mode `0600`, and reference them through Compose `secrets.file`. Symlinks, external secrets, production credentials, environment-variable secret transport, and files outside that exact directory are unsupported.

## Fresh install or repair

Use a clean reviewed checkout at the exact commit:

```bash
ref=$(git rev-parse 'HEAD^{commit}')
sudo ./scripts/install-tester.sh --install \
  --config /etc/ci-fleet-tester/tester.env \
  --ref "$ref"
```

The command fails before mutation unless it sees Debian 12 or newer, root, the local default Docker context/socket/root, Compose v2, required generic tools, protected paths, and Docker storage below 80%. It stages an immutable source release, validates it, switches the `current` symlink, installs health and expiration timers, and verifies both configuration and active environments. Repeating the same command is idempotent.

## Environment lifecycle

The runtime command is the one interface for create/update, inspect, reset, and removal:

```bash
sudo /opt/ci-fleet-tester/tester-runtime --converge --environment example-preview
sudo /opt/ci-fleet-tester/tester-runtime --inspect --environment example-preview
sudo /opt/ci-fleet-tester/tester-runtime --reset --environment example-preview
sudo /opt/ci-fleet-tester/tester-runtime --remove --environment example-preview
```

`--converge` validates the full resolved Compose model before `up --wait`. `--reset` removes only that exact Compose project and its volumes, then recreates it from the approved definition and digest. `--remove` uses the same scoped `compose down --volumes`; no global Docker prune is used. State reports only environment/project/owner, loopback route, expiry, source revision, timestamps, and health—not Compose environment values or secret content.

`ci-fleet-tester-health.timer` checks every five minutes. `ci-fleet-tester-cleanup.timer` checks expiration every fifteen minutes. Expired environments are removed through the same scoped path. Disposable environment data is intentionally removed on reset/expiry. Reviewed definitions and fixture sources belong outside runtime state and need ordinary configuration backups; credentials and disposable volumes are never backed up by this service.

## Upgrade, validation, and rollback

```bash
ref=$(git rev-parse 'HEAD^{commit}')
sudo ./scripts/install-tester.sh --upgrade --config /etc/ci-fleet-tester/tester.env --ref "$ref"
sudo ./scripts/install-tester.sh --check --config /etc/ci-fleet-tester/tester.env
sudo ./scripts/install-tester.sh --rollback --config /etc/ci-fleet-tester/tester.env
```

Upgrade validates the candidate before activation and restores the complete previous release if post-switch checks fail. A successful switch records only the previous complete source revision as last known good. Rollback changes the tester service release; it does not rewrite an application's immutable image digest or reset environment data.

## Removal

Remove every environment explicitly, verify no state remains, then uninstall:

```bash
sudo ./scripts/install-tester.sh --uninstall --config /etc/ci-fleet-tester/tester.env
```

Uninstall fails while any managed environment exists. It removes only tester units and immutable service releases. Host configuration, definitions, and secrets remain for explicit operator disposition; the script never guesses which credential may be deleted.

## External acceptance gate

Before this draft can merge, an authorized operator must use a prepared isolated Debian Docker host with test-only identity/network/storage/domain boundaries and provide evidence for: fresh install, unchanged second install, immutable application converge, health, scoped reset, expiration cleanup, upgrade, failed-upgrade restoration, rollback, and removal. No production credentials, database, network authority, ordinary CI runner, or live production system may participate.
