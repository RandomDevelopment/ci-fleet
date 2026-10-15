# Local validation

Issue [#135](https://github.com/RandomDevelopment/ci-fleet/issues/135) introduces
a complete local entry point and an opt-in signed configuration-evidence path.
They run without GitHub Actions, `gh`, an Actions event, or an Actions API call.
The established required-check policy remains in place until
the parity and rollback gates below pass. The engine hosted adapter calls the
complete local entry point and retains its independent main-policy guards. This implementation does not claim
that host adoption or the standalone template migration has occurred.

## Tools and execution boundary

Use a disposable Debian 13 or Ubuntu 24.04 Linux execution machine. Engine
fixtures require root, `runuser`, `/dev/shm`, Bash, Git with complete history,
Python 3.11 or later, PyYAML, ShellCheck, OpenSSL with Ed25519 support,
coreutils, util-linux, curl and standard systemd utilities. The full engine run
also requires Docker Engine, Compose v2 and access to its local Unix socket.
The harness records tool versions. Missing tools, skipped tests and interrupted
checks fail the run. Do not use an active fleet, application or signing host.
The `--disposable-host` flag acknowledges this execution requirement; a filesystem
check also rejects standard installed fleet paths. That check cannot detect an
unusually installed service, so the operator must verify the machine's role.

Configuration runs need Bash, Git, Python, ShellCheck and OpenSSL and do not
require root or Docker. The core configuration profile covers the embedded
configuration contract. A standalone-template suite must additionally preserve
its updater, compatibility and release tests under template issue
[#32](https://github.com/RandomDevelopment/ci-fleet-config-template/issues/32).
Do not treat this profile as proof of those additional standalone tests.

Populate complete repository history and tags before running. Policy is an
independently reviewed engine commit, not a merge base chosen by the candidate.
For trusted acceptance, run the orchestrator itself from that trusted checkout.
Git and image/package downloads require network access when not already cached.
The suite never registers a runner and needs no fleet credential. Existing
installer/systemd/recovery fixtures use fake services and isolated filesystem
roots. They do not prove deployment on a real prepared host.

## Complete engine command

Replace all uppercase values with full available commit SHAs or local paths.
Choose the PR's exact head and final base. Use a new output directory outside
both checkouts. Run as root only on the disposable execution machine.

```bash
python3 /trusted/ci-fleet/scripts/local_validate.py \
  --repository /work/ci-fleet \
  --source SOURCE_SHA --base BASE_SHA \
  --policy POLICY_SHA --main MAIN_SHA \
  --pr-title 'ci: replace hosted validation with local testing' \
  --disposable-host --output /results/unique-run
```

The default profile is `full`. `--profile quick` omits container builds and
cleanup end-to-end checks and explicitly records that expected skip. A quick
success is never full evidence. The engine's lower-level `scripts/validate.sh`
remains useful for development, but it omits workflow policy and other checks.
It cannot claim complete validation by itself.

For a new release add `--tag v0.2.0 --tag-created`. Never pass `--tag-created`
for a tag update. A release validates conventional messages and committed blobs
back through the previous stable release, or all history for an initial release.
Stable tags must be reachable from the explicitly supplied main commit;
prereleases must be branch-local. The tag validator also enforces the version
bump. Keep release review and production approval separate from these tests.

The runner clones independent source and policy checkouts, excludes working-tree
edits, resets the main reference to the supplied identity, and uses a unique
private temporary directory under the operator's protected home. Permission
fixtures use a separate uniquely named directory under `/tmp` with read and traversal
permission for their deliberate UID changes. Each fixture controls access to its
own child directory. Both temporary roots are removed on exit. It strips inherited credentials and Git/Python
configuration from check environments. It loads the commit validator, secret
scanner and check plan from the pinned policy checkout. Source test programs
remain proposed code and must receive independent review.

Each stage has a log, timestamps, result and SHA-256 log digest in `result.json`.
The report records commit, tree, base, main, suite, profile, policy revision,
metadata, tool versions and expected skips. Preserve the entire output directory
outside Git. A changed source, base, title, tag or policy requires a new run.
Nonzero exit means failure or incomplete execution; no report also means
incomplete execution. Never infer success from the last visible test line.

Each stage has a 1,800-second timeout, configurable with `--timeout`. SIGINT and
SIGTERM fail the current stage, terminate its process group and remove temporary
checkouts. A hard kill or power loss cannot guarantee cleanup. Use the report's
run identity and the invocation-specific image tags to inspect leftovers, or
discard the disposable machine. Do not run global Docker prune. Container tests
remove their named inspection container and invocation-specific image tags;
the cleanup fixture tests scoped success, failure, cancellation and timeout.
Build cache can remain on the disposable daemon.

## Configuration repositories

Use the same trusted engine orchestrator with a separate source repository:

```bash
python3 /trusted/ci-fleet/scripts/local_validate.py \
  --kind configuration --repository /work/private-config \
  --source APPLIED_SHA --reviewed-head REVIEWED_HEAD_SHA \
  --base FIRST_PARENT_SHA --main APPLIED_SHA \
  --policy-repository /trusted/ci-fleet --policy REVIEWED_ENGINE_SHA \
  --output /results/config-run
```

This runs conventional-commit policy, trusted committed-history secret scanning,
syntax checks, pinned-policy rejection tests against public fixtures, strict
source validation against the base's prior configuration and rollout evidence,
reference configurations and initializer positive/negative checks. The strict
validator and its reference schema come from the independently pinned engine, so candidate validator changes cannot waive host policy. Source
policy tests are exercised as engine/template development, while initialized
configuration runs use the pinned public fixtures. It reads prior files from Git
blobs, not from mutable temporary files belonging to another run. Initialized adopters
must pass strict policy; public fictional fixtures remain non-strict in the
engine's embedded-template tests.

A merged or squash commit must obtain its own full configuration result with
its first parent as base and `--reviewed-head` naming the original reviewed head.
The trees must match. Commit policy and secret scanning cover both histories,
including a secret removed before a squash. Reviewed-head results alone do not authorize the new
commit, even when its tree matches. Every integrated commit after the installed
baseline needs its own result and review evidence. Reconcile and repeat review
when a base change changes the proposed tree.

## Coverage and hosted adapter

| Previously workflow-only check | Complete local implementation |
| --- | --- |
| PR title and proposed conventional messages | Pinned `validate_commits.py` |
| Tag immutability, SemVer, main ancestry, release range and bump | Explicit tag metadata and pinned validator |
| Secret scan of every proposed committed blob | Pinned `scan_committed_secrets.py`, including removed intermediate secrets |
| ShellCheck and documentation links | Local stages against the source checkout |
| Python, task-plan, installer, systemd, recovery and security tests | Pinned `validate.sh --quick`, with named tests in its log |
| Compose validation, image builds, APT cache reuse/image-ID parity, inert runner and cleanup outcomes | Pinned `validate.sh --containers` |
| Embedded schema, policy, multi-host and initializer checks | Pinned `validate-template.sh` |

The engine workflow calls the full local entry point and preserves the
`Build without registering a runner` check identity. Its existing trusted-main
commit and secret policy guards remain separate during staging. Hosted PR jobs
execute the proposed suite from the checked-out revision, as before; trusted
local acceptance instead pins an independently reviewed suite outside the
candidate. Protection migration remains separately gated. The embedded template convenience workflow is default-off through the repository
variable `CI_FLEET_HOSTED_VALIDATION=true`. Set `CI_FLEET_VALIDATOR_REVISION` to a
reviewed full supporting engine commit before enabling it. The standalone
template must adopt the same behavior under #32. Enabling that workflow must not add a required
check or a host requirement. Disabling the variable, removing the workflow,
Actions outages and quota blocks must leave local execution complete.

## Cutover gates

1. Retain a full exact-identity engine run on a disposable Docker host with
   Actions unavailable, plus successful complete configuration-consumer runs.
   Retain inspectable reports and logs and compare them with existing coverage.
2. Finish template #32, including its standalone-only tests and optional workflow.
   Publish a template release only through its existing reviewed release gate.
3. Stage an engine, executing manager and checkpoint manager advertising
   `configuration_local_validation`. Exercise candidate rejection, rollback and
   last-known-good recovery with Actions unavailable. Provision local approval
   policy only after compatible recovery is verified.
4. Deliberately reconcile required checks and auto-merge under engine issue
   [#114](https://github.com/RandomDevelopment/ci-fleet/issues/114). An Actions
   check cannot require an unavailable optional workflow. Preserve independent
   review and unrelated protection. Do not permanently waive a failed check.
5. Adopt the template and private configuration changes separately. Record the
   cutoff, baseline, evidence signing authority, policy and tested rollback or
   reinstall procedure. Restore the previous reviewed policy and workflow only
   through an authorized migration if cutover fails.

No billing, repository setting, credential, release, private engine pin or live
host change follows automatically from repository implementation.
