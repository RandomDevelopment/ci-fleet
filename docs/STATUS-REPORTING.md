# Authenticated controller status reporting

The status channel extends the local health collector with a redacted, versioned report for routine remote observation. It is backend infrastructure for a future read-only console. It does not expose a controller listener or permit host actions.

## Flow

1. The existing root-owned health timer collects local state. It reads exact runner counts from a small status file inside the controller container with `docker exec`; no additional service receives the Docker socket.
2. The reporter serializes `schemas/status-report-v1.json`, signs the request with that controller's independent key, and sends `POST /v1/status` over HTTPS.
3. A receiver bound to loopback validates identity, signature, freshness, nonce, size, schema, and ordering before storing the report.
4. A TLS reverse proxy exposes the receiver. The receiver itself refuses a non-loopback bind.
5. A later console can use the authenticated read-only API. Mutation endpoints do not exist.

The five-minute health timer is the normal submission schedule. The receiver rejects submissions less than 30 seconds apart and bodies larger than 32 KiB.

## Version 1 contract

The machine-readable contract is `schemas/status-report-v1.json`. It reports:

- controller ID, controller build/engine version, host boot time, and SSH state;
- desired and applied configuration commits;
- reconciliation state and last successful reconciliation time;
- drift state;
- controller process state and restart count;
- reconciliation, drift, health, and cleanup timer states;
- current, busy, and configured-maximum runner counts;
- CPU use, logical CPU count, memory, swap, root/Docker disk and inode use, and 1/5/15-minute load;
- Docker availability, OOM evidence, and aggregate configured/used/free/legacy subnet counts;
- one controlled error code/message, report generation time, and schema version.

All times are Unix seconds. Commit values are empty when unavailable. Receiver validation rejects unknown fields and unsupported schema versions rather than guessing at compatibility.

`error.message` is derived only from a controlled error code (`_` becomes a space). Raw exception text is never transmitted.
Docker pool prefixes and network addresses are intentionally absent from the
status contract; they remain in private desired state and host-local inspection.

## Authentication

Each controller receives a unique random HMAC key. A key must contain 32-128 bytes and be owned by root with mode `0600` on the controller. The receiver keeps a separate copy owned by its service account with mode `0600`. Key files are read byte-for-byte, including leading or trailing whitespace; copy the same raw bytes to both sides.

Every report includes:

- `X-CI-Fleet-Controller`;
- `X-CI-Fleet-Timestamp`;
- a 128-bit random `X-CI-Fleet-Nonce`;
- an `Authorization` header using the `CI-Fleet-HMAC-SHA256` scheme.

The signature covers method, fixed path, controller ID, timestamp, nonce, and SHA-256 body digest. It is valid only within five minutes. The receiver selects the key from the claimed controller ID, requires the signed ID to equal the payload ID, and records nonces until their authentication window expires. A controller therefore cannot sign as another controller unless that controller's independent key is compromised.

Rotate a controller key by replacing the receiver-side copy, restarting the receiver so it reloads the `0600` auth configuration, and then replacing the controller-side copy within one reporting interval. Keys are not GitHub credentials and must not be committed to desired state.

## Receiver

Example service-account-owned configuration (`0600`):

```json
{
  "controllers": {
    "example-ci-01": "secrets/example-ci-01.key"
  },
  "read_token_file": "secrets/read-api.token"
}
```

Start the stdlib receiver on loopback behind an HTTPS reverse proxy:

```bash
python3 scripts/status_receiver.py \
  --auth-config /etc/ci-fleet-status/auth.json \
  --database /var/lib/ci-fleet-status/status.db \
  --bind 127.0.0.1 \
  --port 8080
```

The database is mode `0600`. Defaults retain at most 288 reports per controller and no report older than seven days; whichever bound is reached first wins. At five-minute intervals, the count bound is approximately one day. Nonces are retained only for the request-authentication window.

Read endpoints require `Authorization: Bearer <read token>`:

- `GET /v1/controllers` returns the latest report for each controller;
- `GET /v1/controllers/<controller-id>` returns latest plus bounded history.

`POST /v1/status` is the only write endpoint. There are no endpoints for configuration, shell execution, logs, Docker actions, runner actions, or arbitrary host operations.

Controller-side configuration in `/etc/ci-fleet/monitoring.env` is:

```text
CI_FLEET_HEALTH_STATUS_URL=https://status.example.invalid/v1/status
CI_FLEET_HEALTH_STATUS_KEY_FILE=/etc/ci-fleet/secrets/status-reporting.key
```

The URL must be HTTPS with the exact `/v1/status` path and no embedded credentials, query, or fragment.
Schema-v3 desired state may require reporting with only the fixed host-local
configuration reference `/etc/ci-fleet/monitoring.env`; endpoint and key values
remain outside Git. A required but missing or unsafe host-local configuration is
reported as a redacted delivery warning and does not interrupt runner lifecycle.

## Threat model

Protected assets are controller identity, status integrity, status confidentiality, controller credentials, private desired state, and runner/reconciliation availability.

Covered threats:

- network modification or forgery: HTTPS plus body-bound HMAC;
- replay and stale overwrite: timestamp window, nonce uniqueness, and monotonically increasing report time;
- controller impersonation: independent keys and header/payload identity equality;
- storage abuse: strict schema, 32 KiB request cap, rate limit, count retention, and time retention;
- secret leakage: allowlisted fields and controlled error strings only;
- receiver exposure: loopback-only application bind, authenticated reads, and external HTTPS termination;
- monitoring dependency failure: delivery failure is a local warning and never blocks runner handling or reconciliation.

Residual risks:

- compromise of one controller exposes that controller's reporting key and permits forged reports for that identity until rotation;
- ordinary runner jobs are already host-root-equivalent through the Docker socket and can read any host-local reporting key; this channel prevents cross-controller impersonation but does not attest a controller against malicious code already running on that controller. Isolate job Docker onto a separate trust boundary before treating reports as adversarial to job code;
- compromise of the receiver exposes retained status and all receiver-side reporting keys;
- HMAC keys are symmetric; use a managed asymmetric identity service later if receiver compromise becomes part of the impersonation threat model;
- the read bearer token is suitable for the backend foundation, not browser distribution. A future console should terminate user authentication before this API.

## Deliberately excluded

Reports never contain:

- GitHub App keys, installation tokens, reporting keys, read tokens, or other credentials;
- environment names or values;
- private desired-state contents or repository contents;
- command lines, process arguments, arbitrary logs, job output, or exception text;
- project source, runner registration material, network addresses, or provider inventory;
- Docker socket access or any host-control capability.

The local full health result remains available for recovery, but only the status schema's allowlisted summary leaves the controller.

For dedicated-host packaging, one-time secret boundaries, activation,
verification, upgrade, and rollback, see
[Status receiver deployment](STATUS-RECEIVER-DEPLOYMENT.md).
