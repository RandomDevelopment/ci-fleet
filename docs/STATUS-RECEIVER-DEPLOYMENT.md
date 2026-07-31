# Status receiver deployment

This runbook prepares the existing authenticated status receiver for a dedicated
Linux host. It does not create a host, provision credentials, change a controller,
or deploy anything by itself.

## Target and boundaries

Use a small always-on VM or LXC separate from every runner controller: 2 vCPU,
2–4 GiB RAM, and about 20 GiB disk. Confirm the next unused infrastructure ID
from the live hypervisor inventory immediately before creation; repository state
is not inventory evidence.

The receiver host has no Docker socket, runner credentials, deployment
credentials, or inbound connection to a controller. The Python application binds
only to `127.0.0.1:8080`; an existing reverse proxy terminates HTTPS. Controllers
submit outbound HTTPS. The read API is authenticated and has no mutation route.

## Install from a reviewed commit

On the prepared receiver host, use the supported `/usr/bin/python3` version 3.9
or newer, check out the exact reviewed commit, and verify a clean tree. The
installer creates the unprivileged `ci-fleet-status` account, release and state
directories, a hardened systemd unit, and an atomic `current` link. It does not
create credentials.

```bash
ref=$(git rev-parse HEAD)
test -z "$(git status --porcelain)"
sudo ./scripts/install-status-receiver.sh --install --ref "$ref"
```

A second identical invocation returns `NO_CHANGE`. Before activation, verify:

```bash
sudo ./scripts/install-status-receiver.sh --check
sudo systemd-analyze verify \
  /etc/systemd/system/ci-fleet-status-receiver.service
```

## One-time secret provisioning boundary

Provision one independent 32–128 byte signing key per controller and one distinct
32–128 byte visible-ASCII read token. Values never belong in Git, command
arguments, chat, logs, issues, PRs, fixtures, or artifacts.

An authorized human uses an approved secret manager or controlled provisioning
workstation to place the same controller key at these host-local paths:

- receiver: `/etc/ci-fleet-status/controller-keys/<controller-id>.key`;
- controller: `/etc/ci-fleet/secrets/status-reporting.key`.

The read token exists only at `/etc/ci-fleet-status/read-api.token`. On the
receiver, every key, token, and `auth.json` is owned by `ci-fleet-status` with
mode `0600`; both containing directories are mode `0700`. On the controller, the
signing key is root-owned mode `0600`. Verify ownership, type, and mode without
printing content. Stop and remove only the newly provisioned files if any check
fails. Delete any provisioning-workstation copy after both destinations are
verified. Do not enable SSH to provision or verify the controller.

Create receiver-local `auth.json` with an editor that does not log content. It
contains only controller-to-key-path mappings and the read-token path; use the
fictional shape in [status reporting](STATUS-REPORTING.md#receiver). Never put a
secret value in that JSON file.

## HTTPS and activation

Install the location block from
`deploy/status-receiver/nginx-location.conf.example` in the existing HTTPS
reverse proxy. Supply the real public certificate and private endpoint only in
private infrastructure configuration. Do not expose port 8080.

After receiver-local credential metadata and reverse-proxy configuration pass:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ci-fleet-status-receiver.service
sudo systemctl is-active --quiet ci-fleet-status-receiver.service
python3 - <<'PY'
import json
import urllib.request

response = urllib.request.urlopen("http://127.0.0.1:8080/healthz", timeout=5)
assert json.load(response) == {"status": "ok"}
PY
```

Configure the controller's private monitoring policy with the HTTPS
`/v1/status` URL and its host-local signing-key path. Preserve its existing
identity, routing, capacity, resources, scale-to-zero behavior, and disabled SSH.
A reporting failure must remain warning-only and must not interrupt runner
management or reconciliation.

## Verification

1. Submit one scheduled report and confirm HTTP 202 without printing its body or
   authorization headers.
2. Read `/v1/controllers/<controller-id>` with the read token loaded from its
   file by the client process, not placed in an argument or environment dump.
3. Confirm an invalid signature, stale timestamp, replayed nonce, wrong
   controller identity, and oversized payload are rejected.
4. Restart the receiver and confirm `/healthz`, authenticated reads, and retained
   bounded history recover.
5. Stop the receiver for longer than one reporting interval. Confirm the
   controller records only a reporting warning and continues reconciliation and
   runner lifecycle; then restart the receiver and confirm reporting resumes.
6. Stop the controller or take it offline. Confirm external monitoring of the
   separate receiver still works and alerts on the latest report age.
7. Confirm the receiver listens only on loopback and port 8080 is unreachable
   externally. Confirm SSH remains disabled on the controller.

The receiver suppresses request logs. Keep systemd journal retention bounded by
the host's reviewed journald policy and monitor service restart count. SQLite
retention is enforced independently by age and per-controller count. The status
database is disposable; recovery is reinstalling the reviewed release,
reprovisioning credentials, and accepting fresh reports. Back it up only if an
operator separately decides that short status history is durable evidence.

## Upgrade and rollback

From a clean checkout at the newer reviewed commit:

```bash
ref=$(git rev-parse HEAD)
sudo ./scripts/install-status-receiver.sh --upgrade --ref "$ref"
sudo ./scripts/install-status-receiver.sh --check
```

The upgrade stages an immutable release, records the previous release, switches
the symlink atomically, and restarts only an already-active service. If health,
ingestion, read access, or retention verification fails:

```bash
sudo ./scripts/install-status-receiver.sh --rollback
sudo ./scripts/install-status-receiver.sh --check
```

Rollback restores the selected release's application files and systemd unit. It
preserves `auth.json`, keys, read token, database, reverse-proxy configuration,
and journal policy. Reverse-proxy or schema changes require their own reviewed
compatibility and rollback plan.

## Test coverage

`scripts/test-install-status-receiver.sh` exercises clean install, idempotent
rerun, upgrade, check, and rollback in an isolated root. Receiver tests cover
restart/key reload, incorrect secret permissions, authentication, controller
isolation, replay/freshness, request and payload bounds, strict schema/redaction,
retention, loopback binding, and read-only routes. Health and installer tests
cover warning-only reporter outages, disabled SSH reporting, and preservation of
runner lifecycle during delivery failure.
