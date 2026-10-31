# Explicit drained recovery

The normal reconcile timer still follows the configuration repository's default
branch. It does not automatically choose historical or recovery commits.
`--installed-ref` remains check-only. Neither option authorizes admission.

A supporting, independently reviewed manager can instead reconcile one immutable
configuration commit without moving the default branch:

```bash
sudo /opt/ci-fleet/manager/current/scripts/remote-reconcile.sh \
  --ref 1111111111111111111111111111111111111111
```

Replace the fictional SHA with the full lowercase 40-hex reviewed configuration
commit. This is a **mutating** operation, not a preview. Symbolic, abbreviated,
uppercase, malformed, missing and duplicate refs are rejected before credentials,
fetch or the installer lock. `--check-only`, `--installed-ref` and `--no-op` cannot
be combined with `--ref`.

## Allowed scope

- Installed metadata, the installed Git-authored baseline and the candidate must
  all identify the same drained controller. Active and disabled candidates are
  rejected; metadata alone cannot turn an active authored baseline into drained.
- The candidate must preserve the complete controller and fleet policy, including
  configured minimum/maximum capacity (not merely effective drained capacity),
  resources, pool budget, routing, network, reporting and organization policy.
  This is a generic unchanged-policy rule, not a special one-runner exception.
- Engine repository and full engine ref must match installed state and the current
  runtime marker. This mode cannot promote or downgrade the runtime engine.
- The only permitted fleet-document addition is reviewed `managed_images` for
  the unchanged current engine. Existing image identities cannot be replaced or
  removed, and unrelated engines cannot be added. Installer image publication,
  validation and health checks are not bypassed.
- An existing LKG must identify the installed drained baseline. An older active or
  otherwise different LKG blocks recovery rather than becoming a rollback target.
  Do not edit metadata to evade this rule. Resolve missing or conflicting recovery
  evidence through a separately reviewed installation/recovery procedure.

## Validation, failure and recovery

The manager fetches the requested commit and checks its exact identity, then
fetches the installed baseline from the same repository. Both remain local to the
transaction. Default-branch state is not rewritten. Existing host approval and
required-check policy remain fail-closed; a commit not accepted by that policy
cannot use this command as a bypass.

Candidate validation, strict template validation, secret scanning and installer
checks still execute under the existing shared installer lock. Even a request
for the already-installed commit takes the validation/installer/full-health path;
there is no early convergence shortcut. Success requires an `applied` transaction
receipt, installed identity/runtime readback, state-aware full local health and
LKG persistence with readback. Probe delivery is suppressed as in normal reconcile.

Rejected policy and pre-transaction failures do not apply a fallback or overwrite
an accepted LKG. Unknown or unverified installer outcomes remain failures: do not
infer safe rollback from a nonzero exit alone. Final health rejection restores
only the verified same-engine drained baseline through the existing installer
LKG path. LKG write failure is not suppressed or reported as success; explicit
recovery attempts drained-baseline restoration and returns failure. If candidate
metadata was written before readback failed, the manager atomically restores the
retained verified baseline record **before** the installer's live approval and
checkpoint checks; it never synthesizes a replacement recovery identity. A fresh
health report and matching process exit are required for each probe (0 for
healthy/maintenance, 1 for warning, 2 for unhealthy). Missing output, stale output,
execution failure or inconsistent exit/status cannot verify rollback. A rollback
whose health is not drained maintenance/warning is not reported verified.
After a confirmed LKG restoration, the manager must persist and read back a
nonterminal reconciliation record identifying the rejected desired commit and
the restored applied commit before the fresh rollback probe. This isolates the
current transaction's failure from that probe without suppressing independent
host-health checks or advancing the last-success timestamp. A missing or
unverified record cannot authorize rollback success. The final transaction still
reports failure or `rolled_back`; ordinary health continues to treat those
terminal outcomes as critical, even after a healthy restoration. Terminal
`rolled_back` publication must itself pass write/readback verification before
any rollback completion indication. If it fails, the manager attempts one
checked `failed` record retaining the attempted and restored identities. If that
fallback is also unverified, it stops explicitly for operator intervention and
emits no completion indication. Storage failure can leave a nonterminal record;
this is unverified state, not a claim that ordinary health is durably critical.
Write/readback verification is not a power-loss durability guarantee.
Inspect the protected reconcile state and installer diagnostics after every
nonzero exit. Failed recovery must remain held; admission needs separate review
and authorization. This command does not establish active-pilot evidence,
isolation proof or permission to publish otherwise unapproved images.

## Accept an already installed recovery baseline

A supporting reviewed reconciliation checkout can accept the current installed
configuration as last-known-good without another installer transaction:

```bash
sudo /path/to/reviewed-ci-fleet/scripts/remote-reconcile.sh --accept-installed
```

Use the exact independently reviewed, merged source that supports this command.
Older managers do not support it. The runtime and installed manager do not need
to be replaced for this verification. Keep the reviewed capacities, workload
protections and automation holds in place throughout acceptance.

The command holds the installer lock and fetches only the locked installation's
full configuration identity. It requires the existing approval policy, an exact
installed check and fresh state-aware full health before replacing LKG through
the normal checked metadata writer. Healthy or warning is accepted for active
controllers; drained and disabled controllers require matching maintenance or
warning health. The prior record remains in place during verification. Save its
protected metadata and recovery-source evidence outside Git before acceptance.

Drift, a mismatched fetch, missing or inconsistent health, and metadata write or
readback failure stop acceptance. This command never repairs configuration or
applies the older LKG, including on failure. It cannot be combined with another
reconciliation mode. Normal same-commit reconciliation, `--check-only`,
`--installed-ref` and `--no-op` retain their existing behavior and do not accept an
installed LKG. Inspect the terminal reconciliation record after any failure and
keep automation held until the actual result is established.

This provides a separate route when an older LKG has an incompatible capacity or
resource policy. It does not authorize restoration of that older state, changes
to a checkpoint, an engine upgrade, workload retirement or storage cleanup.
Template guidance and private adopter verification must follow the accepted
supporting source before live use.

## Rollout boundary

This feature requires a new reviewed supporting engine/manager pin and complete
isolated Linux validation, independent review and authorized staging. It is not
a drop-in flag for older pinned managers, including the old `312` manager. This
feature is only for an already-installed drained controller; adding reviewed image
identities does **not** prove bootstrapability. An image-free baseline requires a
matching protected activation checkpoint for the existing installer recovery path.
Updating public source does not change
an installed manager, runtime, checkpoint or private configuration pin. Preserve
compatible checkpoint recovery and stage manager support before using the flag;
never patch pinned guest source or fabricate LKG/approval records. Engine merge,
template publication, private adoption and live-host acceptance are separate gates.

## Local regression boundary

`python3 -I -B scripts/test_explicit_recovery.py` uses temporary repositories and
command doubles for authentication, installer, host collection and policy. The
rollback-state regressions use the production reconciliation-state reader and
health evaluator against persisted fixture records, including independent
critical host-health and state-persistence failures. The suite needs Linux
Bash, Git, Python and flock, but no fleet credentials, Docker or live host. It
proves orchestration and fail-closed routing, not actual image builds, installer
transactions, systemd behavior or host acceptance. Complete exact-source/base
validation still follows [local validation](LOCAL-VALIDATION.md) on a disposable
execution machine; a focused green suite is not complete acceptance evidence.
