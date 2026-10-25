# Controller GitHub App setup

The controller authenticates to GitHub as a GitHub App — never a PAT or other
long-lived user credential. It uses its app identity for two things only:
registering self-hosted runners and, optionally, fetching its reviewed
desired-state configuration from a private repository over outbound HTTPS.

This guide creates that app from scratch. It is generic: substitute your own
organization, app name, and configuration repository.

## Why an app instead of a token

- Installation tokens expire in one hour; a leaked PAT does not.
- Permissions are scoped per repository and per capability.
- Runner registration and configuration reads are auditable under the app's
  identity, not a personal account.
- Revoking access is removing an installation, not rotating a user's keys.

## Bootstrap on the target host

Run the reviewed checkout on the target Linux Docker host. The callback listens
on a private Unix socket accessible only to the operator UID. Reach it through
an authenticated SSH local forward bound to loopback on the management device.
The callback nonce stays in a private file and never enters process arguments.

```bash
sudo ./scripts/bootstrap-github.sh \
  --organization example-org \
  --instance example-ci-01 \
  --config-repository example-org/config \
  --runner-group example-ci-experimental \
  --allow-repository example-org/example-repo=123456 \
  --config-repo /PATH/TO/PINNED/CHECKOUT \
  --config-ref REVIEWED_COMMIT
```

All names above are fictional. The script prints a non-secret `CALLBACK_SOCKET`,
an SSH `TUNNEL` command shape, and a local `REGISTRATION_URL`. Establish the
forward, open the URL, press the single registration button, install the
new App for **only** the private configuration repository, and return to the
terminal. The target host receives and exchanges the temporary code itself.
Before creating or installing the App, the owner must confirm GitHub's
**Only on this account** installation setting. The manifest requests a private
App, but GitHub's documented App API does not report visibility. Bootstrap
verifies ownership, permissions, and installation scope; it cannot replace this
owner visibility check.
Neither the code nor any credential is copied through a phone, clipboard, chat,
email, issue, or second computer.

Use a second terminal on the same management device to establish the printed
SSH local forward after bootstrap starts. Substitute the target host and use
the same remote account that invoked `sudo`. Keep the SSH session open and open
`http://127.0.0.1:8765/` on that device. Close the session after bootstrap. The
Unix socket is mode `0600` inside an operator-owned mode-`0700` directory. The
operator UID defaults to `SUDO_UID`, or the invoking UID without sudo. A root
console operator can explicitly authorize the trusted SSH account with
`--operator-uid UID`. Other target-host users cannot fetch the nonce or submit
a callback. Do not expose the forward on a shared management device. No TCP
listener or firewall change is created on the target host.

Each project argument includes its numeric repository ID, obtained and reviewed
by the organization owner. The App itself receives access only to the separate
configuration repository; the project name/ID pairs scope runner-group routing
without granting the controller App project source access.

GitHub App names are globally unique. If the default
`ci-fleet-ORGANIZATION-INSTANCE` is unavailable, add `--app-name UNIQUE_NAME`
with a reviewed lowercase name of at most 34 characters. Bootstrap binds the
returned slug to that name and persists it. Reruns reuse the persisted slug;
an explicitly different name is rejected.

The bootstrap:

- requests `contents: read`, metadata read, and organization self-hosted
  runner write permission. With a valid protected configuration-approval policy
  and activation marker, it also requests and verifies `actions: read` and
  `pull_requests: read`. It rejects unrelated or broader permissions;
- creates a private, independently revocable App identity for the host;
- verifies callback state and expires the callback after 30–1800 seconds;
- publishes the PEM at `/etc/ci-fleet/secrets/github-app.pem` and the
  client/installation IDs at `/etc/ci-fleet/host.env` through atomic file
  replacement, root-owned mode `0600`;
- rejects public, archived, wrong-organization, broader App installation, and
  default, enterprise-inherited, or broad runner-group access;
- creates a missing selected-repository runner group with no repository access.
  Only an acknowledged creation receives the requested access. An acknowledged
  group created by this invocation is automatically deleted if inspection,
  routing verification, or cancellation fails before verification completes.
  Existing groups whose identity or access differs remain unchanged;
- retains the protected creation marker and acknowledged group ID until routing
  verification or confirmed deletion completes. SIGKILL, host failure, or an
  uncertain rollback leaves owner reconciliation required before any retry;
- records registration uncertainty in root-owned mode-`0600`
  `bootstrap-registration.pending` before exposing the portal. A callback
  atomically publishes its code as protected `bootstrap-registration-code`.
  A rerun with the same identity resumes a captured code without registering
  another App. Without a captured code, retries stop for owner reconciliation,
  including after cancellation, timeout, or a dropped tunnel;
- destroys JWTs, installation tokens, callback nonces, and temporary curl
  configurations on exit. The captured code remains protected until conversion
  is attempted. After a successful conversion, a
  protected mode-`0600` `bootstrap-recovery.json` or
  `bootstrap-recovery.pending` is deliberately retained until the PEM and
  identity record are installed. Bootstrap syncs the completed response to disk
  and syncs its recovery pathname before installing credentials. Rerun the
  identical live command to recover.
  A durable pending marker exists before conversion starts. A lost response,
  including zero response bytes, blocks registration retries until owner
  recovery or abandonment. Recovery never overwrites a different active PEM
  or App identity.
  If bootstrap is abandoned, an organization owner must first revoke/delete the
  exact newly created App, then explicitly remove those recovery files; ordinary
  cleanup never discards potentially unique credentials.

Inspect the argument format without local writes or GitHub calls. Dry-run does
not validate a configuration checkout; live mode validates the pinned commit
before mutation.
Complete the [first-install configuration prerequisites](../templates/config-repository/README.md#first-install-capability-evidence),
including reviewed selected-source capability evidence, before live bootstrap.

```bash
sudo ./scripts/bootstrap-github.sh --dry-run \
  --organization example-org --instance example-ci-01 \
  --config-repository example-org/config \
  --runner-group example-ci-experimental \
  --allow-repository example-org/example-repo=123456
```

After success, rerun the same command with `--check` to verify App ownership,
App permissions, effective installation `contents: read` and organization
runner-write permission, exact selected private repositories, runner-group access,
and host-local credential modes without changing the App or runner group.
An enabled approval gate also verifies the selected configuration repository
against the protected numeric repository ID and default branch.
Bootstrap preserves an existing valid `CI_FLEET_RUNNER_TTL` and uses the host
contract default of `6h` when the optional setting is absent.

Every live command requires `--config-repo /PATH/TO/PINNED/CHECKOUT` and
`--config-ref REVIEWED_COMMIT`. Add `--install` for direct handoff to the
idempotent host installer. Bootstrap requires the commit in that local checkout and verifies that its
selected controller pool exactly matches the requested organization, runner
group, and project allowlist before any GitHub mutation.
When the protected approval gate is enabled, bootstrap also verifies the pinned
configuration commit's review and CI evidence against the installed trusted
baseline before discovering or provisioning a runner group.
Remote `OWNER/REPO` handoff is rejected because a fresh host has no independently
verified Git credential before App creation. Without `--install`, the redacted
final report prints the installer shape. Externally provisioned credentials remain supported;
the generic installer never requires this bootstrap on every run.

## Manual provisioning

Use this path for externally provisioned credentials.

## 1. Create the app

Organization Settings → Developer settings → GitHub Apps → New GitHub App.

| Field | Value |
| --- | --- |
| Name | one per controller, e.g. `ci-fleet-<controller-id>` |
| Homepage URL | your organization or fleet repository URL |
| Webhook | disabled — the controller polls; nothing calls it |
| Installation availability | Only on this account |

## 2. Grant minimum permissions

Repository permissions:

| Permission | Access | Why |
| --- | --- | --- |
| Contents | Read-only | fetch desired-state configuration over HTTPS |

Organization permissions:

| Permission | Access | Why |
| --- | --- | --- |
| Self-hosted runners | Read & write | mint runner registration tokens |

The ordinary controller needs no other repository permissions. The optional
[configuration approval gate](CONFIGURATION-APPROVAL.md) additionally requires
read-only Pull requests and Actions on the configuration repository to verify
reviews and exact-commit workflow/job results. It never needs contents write,
approval write, or administration. Granting those read permissions and enabling
the gate remain separately authorized operator actions.

## 3. Generate and transfer the private key

On the app page: Private keys → Generate a private key. GitHub downloads one
PEM. The normal manual workflow in this guide stores it on the controller's
local filesystem at `/etc/ci-fleet/secrets/github-app.pem`, owned by root with
mode `0600`.

GitHub's browser download is necessarily present briefly on a controlled
management workstation. Choose a fresh temporary directory outside synchronized,
indexed, and backed-up locations, restrict the downloaded file to the operator
immediately, and transfer it at once. This is transient handling, not approved
long-term credential storage; never claim that the key was absent from the
workstation.

Before any key bytes arrive, create the controller directory as root-owned mode
`0700` and pre-create the destination as a root-owned regular file with mode
`0600`. Initial setup uses only the active path:

```bash
sudo install -d -o root -g root -m 0700 /etc/ci-fleet/secrets
sudo install -o root -g root -m 0600 /dev/null \
  /etc/ci-fleet/secrets/github-app.pem
```

Verify the directory and destination ownership, type, and mode without reading
the content. Then use an authenticated encrypted channel to stream into that
already secured file; the transfer must not replace it with a default-mode node.
Keep key bytes out of tracing, logs, stdout, process arguments, Git, issues, and
PRs. Compare a SHA-256 digest at both ends without printing file content, then
verify the destination again.

Delete the workstation copy immediately after authenticated transfer and those
transfer checks succeed, before token, reconciliation, health, or convergence
checks. Stop if transfer verification or local deletion fails. The PEM must
never be committed or printed; see [SECRETS.md](SECRETS.md).

This manual workflow permits exactly two host-local files: the active path above
and `/etc/ci-fleet/secrets/github-app.next.pem` while rotating. It does not cover
other custom paths, symlinks, or an external secret manager's import, rotation,
or deletion lifecycle. Those cases require provider-specific tested automation.
Do not improvise them from these Markdown examples;
[issue #27](https://github.com/RandomDevelopment/ci-fleet/issues/27) tracks that
automation.

## 4. Install the app

App page → Install App → choose the organization → **Only select
repositories**: pick only the private desired-state configuration repository.
Installing on all repositories defeats the permission scoping.

Record from the installation page URL and app page:

- **Client ID** (app page, `Iv1...` / `Iv23...`)
- **Installation ID** (the number at the end of the installation URL)

## 5. Wire the host

`/etc/ci-fleet/host.env` (root-owned `0600`, never committed):

```bash
CI_FLEET_GITHUB_APP_CLIENT_ID=<client id>
CI_FLEET_GITHUB_APP_INSTALLATION_ID=<installation id>
CI_FLEET_GITHUB_APP_PRIVATE_KEY_FILE=/etc/ci-fleet/secrets/github-app.pem
```

The controller exchanges a short-lived JWT signed with the PEM for an
installation token at runtime (`scripts/github-app-token.sh`). No token is
stored.

## 6. Verify

The token helper writes a token to stdout. Every verification invocation must
redirect stdout to `/dev/null`; exit status alone is the result.

Before installation, use only a reviewed checkout at the intended immutable
commit. Confirm `git rev-parse HEAD` is that commit, ensure the checkout is
clean, and run the checkout's helper:

```bash
sudo ./scripts/github-app-token.sh \
  --env-file /etc/ci-fleet/host.env >/dev/null
```

**Stop** if the checkout or commit is not the reviewed source, the checkout is
dirty, or the command fails. Do not substitute a downloaded standalone script.

After installation, use the installed manager rather than a working tree:

```bash
sudo /opt/ci-fleet/manager/current/scripts/github-app-token.sh \
  --env-file /etc/ci-fleet/host.env >/dev/null
sudo /opt/ci-fleet/manager/current/scripts/remote-reconcile.sh --check-only
```

`CHECK_OK` means the app can read the desired-state repository. A 403 or
"Repository not found" means the installation lacks the repository or the
`contents: read` grant — see Troubleshooting.

## Troubleshooting

- `Repository not found` on fetch: the repository is not in the installation's
  selected list.
- 403 on the content API after granting permission: the installation has not
  accepted the app's pending permission change. Reopen the installation page
  and approve it.
- 401 on token exchange: `host.env` has the wrong client ID, installation ID,
  or PEM path.

## Accidental creation rollback

An unresolved `bootstrap-registration.pending` means GitHub may have created
the App before a callback reached the host. An owner must reconcile the exact
organization and App name in that protected marker. Resume a durably captured
code with the identical live command while GitHub still accepts it. Otherwise,
confirm no App was created or revoke/delete only the exact newly registered App
before approving removal of the marker and any retained code. An unrelated App
that already holds the same name must remain untouched.

If the runner-group creation response is lost or malformed, bootstrap performs
read-only discovery and retains root-owned mode-`0600`
`/etc/ci-fleet/bootstrap-group.pending`. The new group has no repository access.
The marker also survives SIGKILL or a failed rollback after acknowledged
creation, and records the acknowledged group ID until verification or confirmed
deletion completes. Reconcile that exact ID when present.
Automatic deletion must return HTTP 204. A redirect, unexpected status, or
transport failure retains the marker for owner reconciliation.
Every later live/check invocation stops before making a GitHub request until an
organization owner reconciles the recorded organization/group with GitHub.
An absent group immediately after a timeout does not prove creation failed.
Confirm the eventual outcome and ownership, remove only an accidentally created
empty group with separate authorization, then remove the exact pending file.
Do not delete an observed group based on its name alone.

The bootstrap never replaces or broadens an existing App or runner group. A new
runner group with an acknowledged creation ID is automatically deleted only
when the same invocation cannot
verify its identity/access or is cancelled before verification; it never deletes
a pre-existing group. If another new object was approved accidentally, stop
before running the installer. An
organization owner must compare the App slug/ID and group name in the redacted
bootstrap report with GitHub's settings, verify the new group has no runners,
then remove only those exact newly created objects. Preserve and investigate any
pre-existing or mismatched object. App deletion and deletion of any group outside
the bounded automatic rollback above are live GitHub-setting mutations and
therefore require separate authorization; this command does not automate them.

## Rotation

Use this ordered safety checklist for the normal host-local root-owned PEM
workflow. It is a set of gates, not a copy-and-paste shell program.

1. Generate a new GitHub key and transfer, verify, and install it as described
   above at the one approved replacement path,
   `/etc/ci-fleet/secrets/github-app.next.pem`. Pre-create it as root-owned
   `0600` inside the root-owned `0700` directory before transfer. Keep the old
   key and `/etc/ci-fleet/secrets/github-app.pem` active for rollback, and delete
   the workstation copy before continuing.
2. Update the protected controller identity configuration to select the new PEM.
3. Activate the new key and require the installed manager's token verification,
   reconciliation, health check, and installed-state convergence check all to
   succeed with the new key.
4. Confirm the controller remains healthy and converged after a fresh check.
5. Only then revoke the old key in GitHub. Remove its exact old controller PEM
   only after revocation is confirmed. Retain the now-active replacement PEM;
   never use a wildcard in the shared secrets directory.

**Stop before old-key revocation or deletion** if new-key activation,
reconciliation, health, or convergence is incomplete or fails. Restore the old
configuration while its key and old PEM remain valid. The approved replacement
file is not cleanup residue after successful activation; it is the selected
active key. Further rotation, canonical-path normalization, custom paths,
symlinks, and secret managers require the tested automation tracked by
[issue #27](https://github.com/RandomDevelopment/ci-fleet/issues/27); do not
adapt this checklist into ad hoc shell.

## Retirement

1. In the reviewed private schema-v3 desired state, drain the controller and
   converge that state; verify zero managed runners and zero effective capacity.
2. Revoke every key for this controller in GitHub and uninstall its GitHub App.
3. Confirm the controller can no longer authenticate.
4. Remove the controller's PEM and its local GitHub App identity state, including
   the protected client ID, installation ID, and PEM-path configuration.
5. Through review, permanently disable or remove the controller declaration,
   update lifecycle and capacity inventory as appropriate, and converge the
   reviewed private desired state. Keep private identifiers out of this public
   repository.
6. Verify that no usable controller credential or identity state remains and
   that the authoritative desired state no longer declares active capacity.

**Stop and preserve evidence** if revocation cannot be confirmed or if the
normal host-local files cannot be identified safely. Arbitrary paths, symlinks,
and secret-manager lifecycle operations require the tested automation tracked
by issue #27; operators must not improvise removal from Markdown examples.
