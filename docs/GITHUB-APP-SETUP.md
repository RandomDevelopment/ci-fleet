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

Transfer the PEM to a fresh operator-owned temporary file on the controller
using an encrypted channel such as SSH. Compare a SHA-256 digest at both ends,
then install the verified file with `sudo install -o root -g root -m 0600`.
Remove the controller's temporary copy. Delete the workstation copy **only
after** the encrypted transfer, digest comparison, installation, ownership,
and mode checks have all succeeded. The PEM must never be committed or printed;
see [SECRETS.md](SECRETS.md).

This manual example does not cover arbitrary custom paths, symlinks, or an
external secret manager's import, rotation, or deletion lifecycle. Those cases
require provider-specific tested automation. Do not improvise them from these
Markdown examples; [issue #27](https://github.com/RandomDevelopment/ci-fleet/issues/27)
tracks that automation.

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
   above at a new root-owned `0600` host-local path. Keep the old key active.
2. Update the protected controller identity configuration to select the new PEM.
3. Activate the new key and require the installed manager's token verification,
   reconciliation, health check, and installed-state convergence check all to
   succeed with the new key.
4. Confirm the controller remains healthy and converged after a fresh check.
5. Only then revoke the old key in GitHub and remove the old controller PEM.

**Stop before old-key revocation or deletion** if new-key activation,
reconciliation, health, or convergence is incomplete or fails. Restore the old
configuration while its key remains valid. Custom paths, symlinks, and secret
managers must use the provider-specific tested automation tracked by issue #27;
do not adapt this checklist into ad hoc shell.

## Retirement

1. Drain and stop the controller through its reviewed operational procedure.
2. Revoke every key for this controller in GitHub and uninstall its GitHub App.
3. Confirm the controller can no longer authenticate.
4. Remove the controller's PEM and its local GitHub App identity state, including
   the protected client ID, installation ID, and PEM-path configuration.
5. Verify that no usable controller credential or identity state remains.

**Stop and preserve evidence** if revocation cannot be confirmed or if the
normal host-local files cannot be identified safely. Arbitrary paths, symlinks,
and secret-manager lifecycle operations require the tested automation tracked
by issue #27; operators must not improvise removal from Markdown examples.
