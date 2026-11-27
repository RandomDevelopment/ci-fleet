# Read-only worker views

The ordinary-CI worker UI reads the protected `endpoint.json` and `jobs.json`
exports described in the [worker snapshot contract](ENDPOINT-SNAPSHOTS.md).
The [endpoint UI design](ENDPOINT-UI-DESIGN.md) defines the access and
isolation boundary. Packaging, gateway integration, and live acceptance
are tracked in [#122](https://github.com/RandomDevelopment/ci-fleet/issues/122).

## What the views show

- **Overview.** Applied lifecycle and policy, busy and current runners,
  configured and reviewed limits, reported problems, and observation times.
  The effective configuration can record an intentional drain or disablement.
  Snapshot v1 has no live admission decision or gate reason for maintenance,
  disk, network reserve, authentication, or inspection. Check results and
  spare runner slots cannot establish readiness.
- **Jobs.** Filter retained observations by repository, recorded state, and
  result. Running activity depends on fresh publisher and listener evidence
  for the matching export. Pending publisher gaps have their own evidence
  time. Exported gaps and omitted records remain visible. Clock uncertainty
  leaves terminal results available with their original evidence. Elapsed
  time identifies the local start and whether the finish came from the source
  or a local completion observation. GitHub run links use the validated owner,
  repository, and workflow run ID.
- **Health.** The five-minute health report supplies check outcomes, including
  subnet headroom, OOM evidence, controller restarts, and timer checks. It does
  not supply numeric values for those checks. Anonymous seven-day capacity
  percentiles cover the exported CPU, memory, swap, disk, and inode metrics.
  The latest included sample has its own timestamp. Capacity samples run
  every 30 seconds while runners are active and pause at idle.
- **Configuration.** Compare desired and applied commits and engine revisions,
  manager and runtime selections, the running controller, resource limits,
  and routing. The desired-engine observation records validation of the
  approved configuration. Individual review records are unavailable.
  Local image binding verification has its own time and revision.
- **Enrollment.** Installation, credential presence, successful
  authentication, scale-set match, listener polling, signed reporting, and
  owner confirmation each have separate evidence. A GitHub App owner must
  confirm visibility. The export has no durable confirmation of that step.

During a controller or Docker outage an available observer can still serve
the views. A retained value shows its original time and last-success status.
An unavailable or invalid export leaves the relevant view navigable with
unknown observations. A missing metric does not show as zero or passed.

## Repository interface

`scripts/endpoint_ui.py` takes a validated `SnapshotReader` through
`WorkerObserver(reader)`. `ObserverServer(listener, observer)` owns an
already listening IPv4 loopback socket. The caller owns provisioning and
activation under #122. The observer reads only the existing export reader
and its bundled static assets. It does not use Docker, host probes, or
private job history.

The authenticated routes support `GET` and `HEAD`.

| Route | Response |
| --- | --- |
| `/`, `/jobs`, `/health`, `/configuration`, `/enrollment` | Worker HTML views |
| `/worker.css`, `/worker.js` | Bundled styles and polling script |
| `/api/v1/endpoint` | Snapshot v1 with derived freshness |
| `/api/v1/capacity` | Capacity evidence from the same snapshot |
| `/api/v1/jobs` | Retained job history, its liveness and coverage, and up to 50 matching records |

Read APIs include `read_schema_version: 1`. Job routes accept
`repository` as a case-insensitive substring of the owner and repository,
`state` as the recorded state, `result` as the recorded result, and
`cursor` from the prior page. A cursor expires when its validated export
changes, its anchor falls out of retention, or the observer restarts. A
`409` prompts the client to reload the first page. Missing or invalid
exports return `503` from the affected API.

Every request requires the backend bearer. The server caps connections at
16 and authenticated handlers at four. It allows one request per
connection, caps headers at 16 KiB with a five-second receive deadline,
and caps responses at 1 MiB with a 30-second write deadline. Responses have
no cross-origin access and use `Cache-Control: no-store`. The browser
refreshes an active view every 15 seconds, backs off to at most two minutes
when data or transport is unavailable, and pauses while hidden. The page
explains when focus pauses refresh. It resumes when focus leaves, subject
to a five-second minimum between requests and the failure backoff. Browser
polling uses the published files at their existing producer cadences.

## Verification

Run `python3 scripts/test_endpoint_ui.py` for protected temporary export
and HTTP integration tests. The `endpoint-ui` group in
[local validation](LOCAL-VALIDATION.md) records test durations and output.
Gateway authentication, service activation, loopback port conflicts,
installation lifecycle, and outage tests on an approved worker remain part
of #122.
