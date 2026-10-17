# Run local validation and provision trusted evidence

Use this walkthrough with the [validation contract](LOCAL-VALIDATION.md) and
[configuration approval rules](CONFIGURATION-APPROVAL.md#signed-local-validation).
It covers engine testing, configuration testing, and an operator-controlled
signing and acceptance procedure. Normal application Actions workflows continue
to schedule self-hosted ci-fleet jobs.

The examples use fictional repositories, identities and paths. Replace every
uppercase placeholder before execution. Keep reports, keys, host details and
real policy files outside Git. Test execution, signing, host activation and
required-check migration are separate operations.

## 1. Prepare the execution machine

Use a fresh disposable Debian 13 or Ubuntu 24.04 machine. It must have no fleet
role, application workload, controller credential, signing key or unrelated
Docker resource. A separate checkout or Unix account does not isolate Docker
access from host root. Keep console access and discard the machine after testing.

Both profiles reject standard signing-authority paths, including
`/etc/ci-fleet-signing`, `/var/lib/ci-fleet-signing` and `/srv/ci-fleet-signing`,
before candidate execution. Partial bootstrap, leftover files and dangling
symlinks also block execution, even with `--disposable-host`.

On that machine, install the [listed prerequisites](LOCAL-VALIDATION.md#tools-and-execution-boundary).
For example, install the non-Docker tools from the distribution's reviewed
package repositories:

```bash
sudo apt-get update
sudo apt-get install --no-install-recommends \
  bash git python3 python3-yaml shellcheck openssl coreutils util-linux \
  curl jq ca-certificates systemd
```

Install Docker Engine, Compose v2 and the Docker Buildx plugin through your
reviewed Linux host procedure, then check `docker version`, `docker compose version`
and `docker buildx version`. Do not install Docker
or run privileged fixtures on an existing fleet or application host. Downloads
need network access unless Git objects, packages and images are already present.
Verify outbound DNS and HTTPS from Docker build containers under the worker's
firewall rules. Working host DNS alone is insufficient. If private-network
isolation blocks the host's LAN resolver, configure an approved reachable DNS
resolver for the disposable Docker daemon while preserving that isolation.

Run the following engine preparation and validation commands in a root shell on
the disposable machine. The harness needs a protected home directory, `/dev/shm`
and local Docker access. Require systemd 255 or newer as PID 1 with cgroup v2.
The system manager owns each stage and its descendants, deadline and cleanup.
There is no custom supervisor or unprivileged fallback backend.
Do not forward an SSH agent or install production Git
credentials; use an operator-controlled read-only fetch method where needed.

```bash
set -euo pipefail
install -d -m 0700 /srv/ci-fleet-validation /srv/ci-fleet-results
engine_repo=/srv/ci-fleet-validation/engine
git clone https://github.com/RandomDevelopment/ci-fleet.git "$engine_repo"
git -C "$engine_repo" fetch --tags origin
engine_policy_sha=FULL_REVIEWED_VALIDATOR_SHA
test "$(git -C "$engine_repo" rev-parse "$engine_policy_sha^{commit}")" = "$engine_policy_sha"
git -C "$engine_repo" checkout --detach "$engine_policy_sha"
python3 -I "$engine_repo/scripts/local_validation_executor.py" --prepare --disposable-host
```

Choose the validator revision independently from the proposed source. For
development acceptance of the PR introducing this suite, an independently
reviewed candidate engine can be the explicitly selected test policy. That does
not make it a merged engine revision or authorize a production pin. Adopters must
still meet their merged-engine and template compatibility rules.

## 2. Test an exact engine PR

Fetch the PR and record its current main base. This example uses PR 137; replace
the number when testing another PR. The fetch uses Git, not an Actions run.

```bash
engine_pr=137
git -C "$engine_repo" fetch origin \
  "refs/pull/$engine_pr/head:refs/validation/reviewed-engine" \
  '+refs/heads/main:refs/remotes/origin/main'
engine_source_sha=$(git -C "$engine_repo" rev-parse refs/validation/reviewed-engine)
engine_main_sha=$(git -C "$engine_repo" rev-parse origin/main)
engine_base_sha=$engine_main_sha
git -C "$engine_repo" merge-base --is-ancestor "$engine_base_sha" "$engine_source_sha"
engine_run_parent=$(mktemp -d /srv/ci-fleet-results/engine.XXXXXX)
engine_result=$engine_run_parent/result

python3 "$engine_repo/scripts/local_validate.py" \
  --repository "$engine_repo" --source "$engine_source_sha" \
  --base "$engine_base_sha" --main "$engine_main_sha" \
  --policy "$engine_policy_sha" \
  --pr-title 'feat(ci): add trusted local validation without Actions' \
  --disposable-host --output "$engine_result"
```

Use the actual reviewed PR title. If the ancestry check fails, reconcile the PR
with main and repeat review before testing; do not silently substitute an older
base to hide the difference. The validator checkout remains at the selected
policy revision while the harness creates a separate candidate checkout.
Keep result paths outside every Git worktree and repository. The harness checks
resolved paths before creating output or running candidates, including when a
repository argument names a subdirectory or a result path uses a symlink.

Inspect `result.json`, every named stage log, recorded identities, tool versions
and exit status. Success requires `profile` equal to `full`, `status` equal to
`passed`, no expected skips and all stages passed. Export the entire result directory once to private durable storage, verify its
recorded hashes there, and reference that bundle for review and signing.
Partial exports and lost-worker runs cannot authorize acceptance. A quick run, missing report, interrupted run or unexpected
skip does not establish full acceptance.

The harness refreshes and verifies source and policy checkouts for every stage.
Those checkouts fetch only declared commit identities and ancestors. Release
runs also freeze the needed stable tag objects from declared source/main in
`result.json`; fresh source stages preserve those exact annotated or lightweight
tags. Other branches and tags remain unavailable, including their unrelated
objects. The policy checkout receives no release tags. Keep the original
operator checkout's complete history and release tags available for selection.
Treat a checkout-mutation or forced-cleanup failure as failed validation and
investigate it before rerunning. Mutation diagnostics identify the first changed
relative paths and change kinds without exposing file contents. Mutable test
fixtures must create commits and replacement refs in their own repositories;
never waive source Git-metadata changes. These checks do not isolate hostile root code
or detect edits restored within a stage. Independently review the source and
supervise the disposable machine; never run candidate tests on the signer.

For Actions-independent acceptance, launch the full run from the operator's
shell on this machine without an Actions job, event or runner. Record the actual
condition demonstrated, such as Actions disabled for the test repository or an
Actions scheduling outage, along with the exact identities and retained results.
Clearing environment variables alone is not proof of an external API outage.
This test still needs registry/package access. GitHub review remains a separate
requirement; do not disable the repository's existing required checks here.

## 3. Test an integrated configuration commit

Use a separate clean execution machine with the configuration prerequisites and
the same independently reviewed engine checkout. Prepare it with the account
setup command above and run the trusted launcher as root. It creates fresh
unprivileged source/scratch copies and keeps policy and completion metadata
protected. Configuration stages need no Docker access. Do not use the signer or
an active fleet host as the execution worker.

The core profile covers the embedded configuration contract. The standalone
template and private adopters have additional updater, compatibility, release
and delivery checks. Use their complete local entry point when adopting this
contract; do not substitute the embedded profile for those additional suites.
The following command illustrates the core profile for an initialized adopter.

Fetch complete configuration history and tags using your existing authorized
read-only Git access. Set `config_repo` to that checkout. Select the exact merged
configuration commit and the PR that produced it, rather than assuming the
latest main commit belongs to the PR being approved:

```bash
set -euo pipefail
config_repo=/work/fleet-config
config_pr=42
config_applied_sha=FULL_MERGED_CONFIGURATION_SHA
git -C "$config_repo" fetch --tags origin
git -C "$config_repo" fetch origin \
  "refs/pull/$config_pr/head:refs/validation/reviewed-config"
config_reviewed_sha=$(git -C "$config_repo" rev-parse refs/validation/reviewed-config)
config_base_sha=$(git -C "$config_repo" rev-parse "$config_applied_sha^1")
test "$(git -C "$config_repo" rev-parse "$config_applied_sha^{commit}")" = "$config_applied_sha"
test "$(git -C "$config_repo" rev-parse "$config_applied_sha^{tree}")" = \
     "$(git -C "$config_repo" rev-parse "$config_reviewed_sha^{tree}")"
config_run_parent=$(mktemp -d "$HOME/config-validation.XXXXXX")
config_result=$config_run_parent/result

python3 "$engine_repo/scripts/local_validate.py" \
  --kind configuration --repository "$config_repo" \
  --source "$config_applied_sha" --reviewed-head "$config_reviewed_sha" \
  --base "$config_base_sha" --main "$config_applied_sha" \
  --policy-repository "$engine_repo" --policy "$engine_policy_sha" \
  --output "$config_result"
```

Here `engine_repo` and `engine_policy_sha` identify the trusted checkout on this
execution machine. For pre-merge development testing use the PR head as source
and its reviewed base; those results cannot authorize the later merged commit.
Squash/merge acceptance needs a new full run for the applied commit, with the
original reviewed head retained so removed intermediate secrets are also scanned.

## 4. Bootstrap the separate signing authority

Perform this section on a protected signing machine that never runs candidate
tests and has no Docker execution role. The authority must supervise the run or
independently verify it through an authenticated operator channel. Receiving a
report and checking its hashes does not establish trustworthy execution.

Use a root Bash operator shell on the signer, with Git, Python and OpenSSL
installed from the reviewed OS repositories. Prepare a separate trusted engine
checkout, then create an Ed25519 key outside every checkout. The shell refuses
to overwrite an existing authority key:

```bash
set -euo pipefail
engine_repo=/srv/ci-fleet-signing/engine
engine_policy_sha=FULL_REVIEWED_VALIDATOR_SHA
install -d -m 0700 /srv/ci-fleet-signing
git clone https://github.com/RandomDevelopment/ci-fleet.git "$engine_repo"
git -C "$engine_repo" fetch --tags origin
git -C "$engine_repo" checkout --detach "$engine_policy_sha"
test "$(git -C "$engine_repo" rev-parse HEAD)" = "$engine_policy_sha"
sudo install -d -m 0700 /etc/ci-fleet-signing /var/lib/ci-fleet-signing/verified
sudo sh -c 'umask 077; set -C; openssl genpkey -algorithm ED25519 > /etc/ci-fleet-signing/local-validation.key'
sudo openssl pkey -in /etc/ci-fleet-signing/local-validation.key \
  -pubout -out /etc/ci-fleet-signing/local-validation.pub
```

Stop if the key already exists; reuse or rotate it only through an explicit
operator decision. Restrict backups and restoration to the signing authority.
The public key may be copied to the host policy; the private key must never go
to the execution machine, controller, job, artifact, repository or PR.

Prepare a protected policy from
[`config-review-policy-local.json.example`](../host/config-review-policy-local.json.example).
Use authenticated GitHub repository and user metadata to obtain numeric IDs,
for example `GET /repos/OWNER/REPOSITORY` and `GET /users/REVIEWER`. Select an
independent allowed reviewer; the author cannot approve their own configuration.
Replace the fictional IDs, validator SHA and public test key. Keep the example's
`workflow_id` and `required_jobs` absent for local-only acceptance.

The following example creates the signer copy, not an active controller policy:

```bash
sudo install -m 0600 "$engine_repo/host/config-review-policy-local.json.example" \
  /etc/ci-fleet-signing/config-review-policy.json
sudo python3 -I - /etc/ci-fleet-signing/config-review-policy.json \
  NUMERIC_REPOSITORY_ID NUMERIC_REVIEWER_ID "$engine_policy_sha" \
  /etc/ci-fleet-signing/local-validation.pub <<'PY'
import json, sys
from pathlib import Path
path = Path(sys.argv[1])
policy = json.loads(path.read_text())
policy['configuration_repository_id'] = int(sys.argv[2])
policy['reviewer_ids'] = [int(sys.argv[3])]
policy['local_validation']['validator_revision'] = sys.argv[4]
policy['local_validation']['public_key'] = Path(sys.argv[5]).read_text()
path.write_text(json.dumps(policy, indent=2) + '\n')
PY
sudo sh -c 'umask 077; printf "1\n" > /etc/ci-fleet-signing/config-review-required'
```

`engine_repo` on this machine is a trusted checkout at `engine_policy_sha`, not
a copy of candidate-produced executable files. Match the suite and required
checks to the reviewed complete validator used by the adopter. The standalone
template's complete suite must not be mislabeled as the smaller core suite.
Review the policy and marker before proceeding. The signer and controller must
use the same policy values; policy changes invalidate earlier signatures.

## 5. Verify provenance and issue evidence

Through the approved authenticated operator channel, supervise the execution or
independently establish its exact source, base, reviewed head, validator, suite,
tools and outcome. For example, from the signer's operator account, use existing
SSH access to fetch results into private staging. Verify the execution machine's
host key through the approved channel and replace the fictional address and
source path; do not disable host-key checking or forward an agent:

```bash
received_parent=$(mktemp -d "$HOME/ci-fleet-received.XXXXXX")
scp -r validation-operator@execution.example.invalid:SOURCE_RESULT_DIRECTORY \
  "$received_parent/result"
```

After independent verification, install the original report and every stage log
in a fresh protected directory. This copy alone does not establish provenance:

```bash
verified_result=/var/lib/ci-fleet-signing/verified/UNIQUE_RUN
sudo test ! -e "$verified_result"
sudo install -d -m 0700 "$verified_result"
sudo install -m 0600 "$received_parent/result/result.json" "$verified_result/result.json"
for stage_log in "$received_parent/result"/*.log; do
  sudo install -m 0600 "$stage_log" "$verified_result/"
done
```

Keep the report and logs together. Stop if the destination already exists, and
use a new run identity. Candidate uploads alone are insufficient.

On the signing machine, set `verified_result` to that independently verified
directory, and set the exact applied commit, original head and PR number from
the reviewed configuration. Then sign:

```bash
verified_result=/var/lib/ci-fleet-signing/verified/UNIQUE_RUN
config_applied_sha=FULL_MERGED_CONFIGURATION_SHA
config_reviewed_sha=FULL_REVIEWED_CONFIGURATION_SHA
config_pr=42
signed_evidence=/var/lib/ci-fleet-signing/verified/$config_applied_sha.json
sudo python3 "$engine_repo/scripts/attest_local_validation.py" \
  --policy /etc/ci-fleet-signing/config-review-policy.json \
  --result "$verified_result" --key /etc/ci-fleet-signing/local-validation.key \
  --reviewed-head "$config_reviewed_sha" --pull-request "$config_pr" \
  --output "$signed_evidence"
```

The output must not already exist. Inspect failures before retrying; do not
truncate logs, edit reports, change timestamps or relabel quick results to make
them acceptable. Reports, each accepted log and the encoded envelope have a
1 MiB limit. Reduce noisy suite output deliberately and rerun if necessary.
Keep larger diagnostics separately. Never publish private reports as PR evidence.

## 6. Provision and check host acceptance

This is a separately reviewed host change after compatible executing, candidate
and checkpoint managers, recovery access and rejection/rollback tests are proven.
Do not enable policy v2 on an older manager or bootstrap an installed-state
baseline by inventing a state file. Existing policies and normal project CI stay
in place until the deliberate cutover is approved.

Before changing an existing controller policy, retain its protected policy,
marker and trusted recovery information through the operator recovery procedure.
Quiesce automatic reconciliation and hold the documented installer/maintenance
locks for the rollout window. Use the adopter's runbook for its actual units and
restore procedure; this public guide contains no host inventory or timer names.

Transfer the reviewed public policy, signed envelope and independently verified
logs through the authorized operator channel. On the controller, use the existing
protected evidence directory named by policy and install the files as root:

```bash
set -euo pipefail
evidence_root=/var/lib/ci-fleet/local-validation
config_repo=/secure/operator-transfer/config-repository
config_applied_sha=FULL_MERGED_CONFIGURATION_SHA
verified_result=/secure/operator-transfer/verified-run
signed_evidence=/secure/operator-transfer/$config_applied_sha.json
sudo install -d -m 0700 "$evidence_root" "$evidence_root/$config_applied_sha.logs"
sudo install -m 0600 "$signed_evidence" "$evidence_root/$config_applied_sha.json"
for stage_log in "$verified_result"/*.log; do
  sudo install -m 0600 "$stage_log" "$evidence_root/$config_applied_sha.logs/"
done
```

Install the reviewed policy and `config-review-required` marker under
`/etc/ci-fleet` only during the approved activation procedure. Their modes must be
`0600`, with root-owned parents that are not group- or other-writable. The policy
must match the signer copy, including the evidence directory. Install all required
evidence before allowing reconciliation to resume. Forward verification checks
every integrated configuration since the installed baseline, so retain valid
evidence for each required commit, not just the newest one.

Use the installed trusted engine's verifier for a read-only acceptance check.
In a root operator shell, set `trusted_engine` to that installed engine and
`config_repo` to the local configuration checkout. Obtain the short-lived App
token from the existing protected host identity and pass it only through stdin:

```bash
set +x
set -o pipefail
trusted_engine=/opt/ci-fleet/manager/current
config_repository=example-org/fleet-config
config_tree=$(git -C "$config_repo" rev-parse "$config_applied_sha^{tree}")
"$trusted_engine/scripts/github-app-token.sh" --env-file /etc/ci-fleet/host.env |
  python3 "$trusted_engine/scripts/verify_config_approval.py" \
    --policy /etc/ci-fleet/config-review-policy.json \
    --repository "$config_repository" --commit "$config_applied_sha" \
    --tree "$config_tree" --installed-state /var/lib/ci-fleet/install-state.json
```

Keep shell tracing disabled. An `APPROVED` response is verifier evidence, not
proof of host convergence. Resume only through the reviewed adopter procedure,
then record desired/applied engine and configuration identities, health, and a
later reconciliation cycle. Do not change production deployment approval.

## 7. Exercise rejection, recovery and migration

On the disposable acceptance system, test missing, tampered, expired and
wrong-identity evidence, an incompatible manager/checkpoint, and Actions
unavailability. Preserve the rejection output and proof that candidate state was
not applied. Repeat the protected rollback and last-known-good recovery procedures
with Actions unavailable; do not fabricate or edit recovery records. A successful
mock fixture does not replace this operational acceptance.

For authority-key rotation, update the reviewed policy on signer and controller,
then rerun and reissue evidence for the new policy digest before convergence.
For rollback, preserve the active policy and marker and select only a compatible
reviewed checkpoint or trusted recorded LKG. A policy downgrade or removal is a
separate root-operator decision. Do not remove the marker merely to bypass a
failed check. Retain recovery access until a later healthy reconciliation cycle.

Common failures and next steps:

- Missing tools or an installed-role rejection: fix the disposable execution
  environment; do not disable the guard or accept a quick subset.
- Commit/tree/base mismatch: reconcile and obtain fresh review, then rerun the
  exact integrated commit. Earlier identical-tree results do not authorize it.
- Changed policy, expired evidence or an uncertain clock: correct the cause and
  independently rerun/reissue evidence. Never modify signed content.
- Unsafe file mode, symlink, missing log or changed digest: restore the verified
  originals through the operator channel and check every parent directory.
- API/review rejection: check independent exact-head review, repository identity,
  PR association and read-only App permissions. Local-only policy still needs
  GitHub review/Git APIs, though it needs no Actions API result.
- Interrupted cleanup: inspect only the recorded run's resources or discard
  the disposable machine. Never use unrestricted global Docker prune.

Finally, migrate optional workflows and required checks together under
[#114](https://github.com/RandomDevelopment/ci-fleet/issues/114). Native protection
cannot verify a local success file. Review the enforcement/publishing identity
and rollback before removing the existing hosted requirement. Complete the
standalone-template release and private-adopter work separately. Application
Actions workflows and self-hosted task routing remain unchanged.
