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

## 1. Create the app

Organization Settings → Developer settings → GitHub Apps → New GitHub App.

| Field | Value |
| --- | --- |
| Name | one per controller, e.g. `ci-fleet-<controller-id>` |
| Homepage URL | your organization or fleet repository URL |
| Webhook | disabled — the controller polls; nothing calls it |

## 2. Grant minimum permissions

Repository permissions:

| Permission | Access | Why |
| --- | --- | --- |
| Contents | Read-only | fetch desired-state configuration over HTTPS |

Organization permissions:

| Permission | Access | Why |
| --- | --- | --- |
| Self-hosted runners | Read & write | mint runner registration tokens |

Nothing else. No `write` on contents, no actions, no administration. If the
controller ever needs more, that is a reviewed design change, not a settings
tweak.

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
