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
PEM to the management workstation. Treat that download as a temporary copy:
transfer it over an authenticated, encrypted channel directly to the final
root-owned path on the controller. Do not stage it in a shared directory or
send it through chat, email, or a repository.

For example, from the management workstation (replace both placeholders):

```bash
PEM="$HOME/Downloads/YOUR-APP.private-key.pem"
CONTROLLER=root@CONTROLLER_HOST
ssh "$CONTROLLER" \
  'install -d -m 0700 /etc/ci-fleet/secrets &&
   umask 077 && cat > /etc/ci-fleet/secrets/github-app.pem &&
   chown root:root /etc/ci-fleet/secrets/github-app.pem &&
   chmod 0600 /etc/ci-fleet/secrets/github-app.pem' <"$PEM"
local_sha=$(sha256sum -- "$PEM" | cut -d' ' -f1)
remote_sha=$(ssh "$CONTROLLER" \
  "sha256sum /etc/ci-fleet/secrets/github-app.pem | cut -d' ' -f1")
test "$local_sha" = "$remote_sha"
ssh "$CONTROLLER" \
  "test \"\$(stat -c '%U:%G %a' /etc/ci-fleet/secrets/github-app.pem)\" = 'root:root 600'"
rm -f -- "$PEM"
unset PEM local_sha remote_sha
```

Use an equivalent privileged SSH workflow if direct root login is disabled.
Do not delete the workstation copy until both checksum and ownership/mode
checks pass. Then remove it from the browser download location, trash, sync,
and temporary storage according to the workstation's secure-erasure policy;
plain `rm` may not erase data from snapshots, SSDs, or copy-on-write storage.
The only retained copy may be the controller file or an approved secret
manager — see [SECRETS.md](SECRETS.md).

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
CI_FLEET_GITHUB_APP_PRIVATE_KEY_FILE=<absolute path to the PEM>
```

The controller exchanges a short-lived JWT signed with the PEM for an
installation token at runtime (`scripts/github-app-token.sh`). No token is
stored.

## 6. Pre-install verification from the reviewed checkout

Before the first managed installation, `/opt/ci-fleet/manager/current` and the
installed-state file do not exist. On the controller host, run the token helper
from the exact reviewed engine checkout that will be installed:

```bash
sudo /PATH/TO/REVIEWED/ci-fleet/scripts/github-app-token.sh \
  --env-file /etc/ci-fleet/host.env >/dev/null
```

The helper writes the installation token to stdout, so the redirection is
mandatory. Exit 0 means JWT signing and token exchange work; it does not prove
that an installed controller can reconcile. Continue with the managed install
workflow before using an installed-manager command.

## 7. Post-install remote-reconciliation verification

Only after a successful managed install, validate the complete fetch and
installed-state path without applying anything:

```bash
sudo /opt/ci-fleet/manager/current/scripts/remote-reconcile.sh \
  --check-only --installed-ref
```

`CHECK_OK` means the app can read and validate the installed desired-state
commit and the controller is converged. The reconcile script consumes its
token internally; it does not print the token. A 403 or "Repository not found"
means the installation lacks the repository or the `contents: read` grant —
see Troubleshooting.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| `Repository not found` on fetch | repository not in the installation's selected list |
| 403 on content API after granting permission | permission change saved on the app but not yet accepted on the installation — reopen the installation page and approve the pending permission request |
| 401 on token exchange | wrong client ID, installation ID, or PEM path in `host.env` |

## Key rotation: activate and verify before revocation

New controller: new app. Do not share one app across controllers.

1. Generate a new key and use the secure-transfer procedure in section 3 to
   place it at a **new** root-owned `0600` path. Verify the transfer and remove
   the downloaded workstation copy.
2. Update `CI_FLEET_GITHUB_APP_PRIVATE_KEY_FILE` in
   `/etc/ci-fleet/host.env` to the new path.
3. Verify that the new key can mint a token, always suppressing token stdout:

   ```bash
   sudo /opt/ci-fleet/manager/current/scripts/github-app-token.sh \
     --env-file /etc/ci-fleet/host.env >/dev/null
   ```

4. Run a normal reconciliation, not `--check-only`. The host-configuration
   drift forces the installer upgrade path, recreates the controller with the
   new PEM mount, and runs its post-activation health check:

   ```bash
   sudo /opt/ci-fleet/manager/current/scripts/remote-reconcile.sh
   sudo /opt/ci-fleet/current/scripts/healthcheck.sh
   sudo /opt/ci-fleet/manager/current/scripts/remote-reconcile.sh \
     --check-only --installed-ref
   ```

   Stop if reconciliation does not report `RECONCILE_OK`, the health check is
   not healthy, or the final check does not report `CHECK_OK`. Restore the old
   `host.env` path and reconcile again; do not revoke the old key.

## Old-key revocation

Only after every activation check above succeeds:

1. On the GitHub App settings page, under **Private keys**, delete/revoke the
   old key.
2. Remove the old PEM from the controller by its exact path; do not use a broad
   wildcard in a shared secrets directory:

   ```bash
   sudo rm -f -- /etc/ci-fleet/secrets/OLD-GITHUB-APP-KEY.pem
   ```

3. Remove any old workstation or temporary copies under the applicable secure
   erasure policy. Keep the new key and its configured path unchanged.

## Controller retirement and PEM removal

Retirement is not complete when only the App installation is removed:

1. Drain the controller through reviewed desired state and verify zero managed
   runners and zero effective capacity.
2. Uninstall the App from the organization to invalidate installation access.
3. On the GitHub App settings page, delete/revoke **every** private key for this
   controller's app; delete the dedicated app itself if it will not be reused.
4. Uninstall the controller. The uninstaller deliberately preserves
   `/etc/ci-fleet/host.env` and `/etc/ci-fleet/secrets`, so remove the retained
   credentials explicitly:

   ```bash
   sudo /opt/ci-fleet/manager/current/scripts/install-worker-controller.sh \
     --uninstall
   sudo rm -f -- /etc/ci-fleet/secrets/github-app.pem
   sudo rm -f -- /etc/ci-fleet/host.env
   ```

   Repeat the PEM removal for every exact rotation path used by this app.
5. Remove remaining management-workstation, temporary, secret-manager, and
   backup copies according to their retention and secure-erasure policies. If
   the retired storage cannot guarantee file-level erasure (for example SSD,
   snapshot, or copy-on-write media), destroy the encrypted volume or its
   encryption key before disposal.
