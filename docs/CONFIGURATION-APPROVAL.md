# Configuration review before host changes

The opt-in configuration approval gate checks GitHub evidence from the trusted
installed engine before applying desired state. It is separate from production
release approval and requires no private branch-protection feature or paid plan.
Main can still receive a direct push; an enabled host refuses to apply it.

## Trust boundary

A separately authorized operator provisions two root-owned regular files with
mode `0600` outside the candidate repository:

- `/etc/ci-fleet/config-review-policy.json`, following the
  [fictional example](../host/config-review-policy.json.example).
- `/etc/ci-fleet/config-review-required`, containing exactly `1` and a newline.

Their parent directories must be root-owned and must not allow group or other
writes. Symlinks, malformed files, unsupported fields, and unsafe permissions are
rejected. Either path's presence requires verification; a remaining marker makes
a missing policy fail closed. With neither path present, existing installations
retain their previous behavior. Candidate Git-authored policy cannot opt out.
Neither file is rendered into runner configuration or included in checkpoints.
Rollback and uninstall preserve both. Only a trusted root operator can remove
the gate; it does not defend against a compromised host root or trusted engine.

The policy pins the configuration repository's numeric GitHub ID, a simple
branch name such as `main`, the validation workflow's numeric ID, required job
names, and allowed reviewer numeric IDs. These are trust anchors, not application
allowlists or host inventory. Account renames do not substitute a different
reviewer identity. The verifier checks the requested repository against its
numeric ID and default branch, and checks the local Git tree against GitHub.

The existing host-side App needs read-only Contents, Pull requests, and Actions
permissions on the configuration repository. It receives no approval or write
authority. Long-lived credentials remain on the controller, outside ordinary CI.
Tokens travel to the verifier through stdin, never command arguments, policy,
or logs. Errors do not print API responses, policy values, or credentials.

## Acceptance rule

Before reconciliation, drift repair, direct install/adoption/upgrade, or a policy
adapter's resume action, the trusted verifier requires all of the following:

1. The exact candidate SHA is the result of one merged PR targeting the trusted
   repository and branch. Direct pushes and ambiguous results are rejected.
   On an existing installation, it must preserve the installed configuration
   commit's ancestry. Force-pushing a previously approved older commit cannot
   roll the host back or replace its accepted history.
2. Its Git tree equals the current reviewed PR head's tree. This supports squash
   commits without treating approval of different source content as equivalent.
   Its first parent must be an ancestor of the reviewed head, and a two-parent
   merge must name that head as its second parent. This rejects a stale reviewed
   tree that would delete later main changes. Reconcile the PR with its final
   base and repeat review before merging.
3. At least one allowed reviewer, other than the PR author, has an `APPROVED`
   formal review on that full head SHA. The reviewer's latest decision applies;
   dismissed or stale approvals do not count, and an outstanding trusted
   `CHANGES_REQUESTED` decision blocks acceptance. Comments do not grant approval.
   The review must follow any base-branch retargeting. Unavailable or incomplete
   PR base history rejects the candidate.
4. The latest push run of the pinned workflow on the applied SHA completed
   successfully for the trusted repository and branch. Each required named job
   also completed successfully in that exact run attempt. Skipped, failed,
   cancelled, queued, and missing evidence is rejected.

A Codex summary saying its review completed, a reaction, a status with a familiar
name, or a successful PR merge-ref workflow is insufficient. Provision an
independent reviewer or review service that submits a formal exact-head approval.
The gate verifies those formal decisions; it does not interpret review prose.

Requests use only GitHub's HTTPS API, refuse redirects, and have a two-minute
evidence budget, fifteen-second request timeout, four-MiB response limit, and
twenty-page limit per collection. Missing permission, network/API errors,
malformed responses, and pagination overflow reject the candidate. The running
controller and its last-known-good state remain in place when a new candidate is
rejected. The timer may retry after evidence becomes available.

## Staged adoption and recovery

Implementation review does not activate a host. First integrate and validate the
public engine, then stage and promote its reviewed immutable SHA in the private
adopter under that adopter's existing capability-rollout rules. A separately
authorized operator must verify the installed manager supports
`configuration_approval_gate`, grant only the additional read permissions,
select independent reviewer IDs and the required workflow/jobs, review the
installed recovery baseline, and provision the files above. Do not enable the
gate while an older manager is installed.

An enabled installer rejects selection of an engine without that capability.
Rollback may restore the already installed root-owned recovery baseline without
depending on GitHub availability, but rejects a checkpoint with an older manager
that would remove enforcement. Review the baseline and compatible checkpoint
before activation; historical host state is not retroactively attested by this
gate. A first installation's absent-manager checkpoint is not an approved way to
remove enforcement. Recovery to an incompatible checkpoint requires a separately
authorized root-operator procedure, not an automatic downgrade.

The verifier runs from the existing trusted engine before candidate engine code
or host convergence. Root-owned policy-action metadata carries the approved
configuration SHA/tree to resume, which checks the evidence again. A local pinned
checkout still requires the actual GitHub configuration identity and matching
commit/tree. Unchanged commits and check-only/no-op runs also verify evidence.
Revoked or unavailable evidence prevents further convergence; this gate does not
automatically stop a controller already running an accepted configuration.

Deterministic tests cover review/check rejection, tree identity, API failures,
pagination, policy permissions, direct installer modes, remote reconciliation,
resume, and rollback. Real permissions, reviewer provisioning, systemd execution,
and unattended bypass rejection require separately authorized live verification.
