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

For an initial installation, the destination may use the conventional active
path because no controller key exists yet. Set `PEM_DEST` explicitly when a
different installation path is wanted:

```bash
PEM="$HOME/Downloads/YOUR-APP.private-key.pem"
CONTROLLER=root@CONTROLLER_HOST
ACTIVE_PEM=""
PEM_DEST=${PEM_DEST:-"/etc/ci-fleet/secrets/github-app.pem"}
```

For rotation, replace the last two assignments with the exact active path from
`CI_FLEET_GITHUB_APP_PRIVATE_KEY_FILE` and a new key-specific filename. Never
use the active path as the rotation destination:

```bash
ACTIVE_PEM="/etc/ci-fleet/secrets/github-app.pem"
PEM_DEST="/etc/ci-fleet/secrets/github-app-ROTATION-ID.pem"
```

Then run the same transfer and verification sequence for either case. The path
validation makes it safe to quote `PEM_DEST` in the remote shell command, the
rotation guard rejects the active path, and shell noclobber prevents replacing
any existing file:

```bash
[[ "$PEM_DEST" =~ ^/etc/ci-fleet/secrets/[A-Za-z0-9._-]+\.pem$ ]] || exit 1
if [[ -n "$ACTIVE_PEM" && "$PEM_DEST" == "$ACTIVE_PEM" ]]; then
  printf 'refusing to overwrite active PEM: %s\n' "$ACTIVE_PEM" >&2
  exit 1
fi

if
ssh "$CONTROLLER" \
  'install -d -m 0700 /etc/ci-fleet/secrets &&
   umask 077 && set -C && cat > "'"$PEM_DEST"'" &&
   chown root:root "'"$PEM_DEST"'" &&
   chmod 0600 "'"$PEM_DEST"'"' <"$PEM" &&
  local_sha=$(sha256sum -- "$PEM" | cut -d' ' -f1) &&
  remote_sha=$(ssh "$CONTROLLER" \
    "sha256sum -- \"$PEM_DEST\" | cut -d' ' -f1") &&
  test "$local_sha" = "$remote_sha" &&
  ssh "$CONTROLLER" \
    "test \"\$(stat -c '%U:%G' -- \"$PEM_DEST\")\" = 'root:root'" &&
  ssh "$CONTROLLER" \
    "test \"\$(stat -c '%a' -- \"$PEM_DEST\")\" = '600'"
then
  rm -f -- "$PEM"
  unset PEM local_sha remote_sha
else
  printf 'transfer verification failed; retained downloaded PEM: %s\n' "$PEM" >&2
  unset local_sha remote_sha
  exit 1
fi
```

Use an equivalent privileged SSH workflow if direct root login is disabled.
If transfer, checksum, owner, or mode verification fails, the sequence stops,
leaves the active controller PEM untouched, and retains the downloaded
replacement for diagnosis or a safe retry. Do not revoke the old GitHub key.
After success, remove any other copy from the browser download location, trash,
sync, and temporary storage according to the workstation's secure-erasure
policy; plain `rm` may not erase data from snapshots, SSDs, or copy-on-write
storage. The only retained copy may be the controller file or an approved
secret manager — see [SECRETS.md](SECRETS.md).

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

1. Generate a new key and use the rotation assignments and secure-transfer
   procedure in section 3. `PEM_DEST` must differ from `ACTIVE_PEM`; the
   transfer must not overwrite either path. Continue only after every transfer
   and verification command succeeds and the downloaded workstation copy is
   removed.
2. Before changing `host.env`, record the exact value of `ACTIVE_PEM` for
   rollback and old-file removal. Then update
   `CI_FLEET_GITHUB_APP_PRIVATE_KEY_FILE` in `/etc/ci-fleet/host.env` to the
   exact value of `PEM_DEST`.
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
   not healthy, or the final check does not report `CHECK_OK`. Restore
   `CI_FLEET_GITHUB_APP_PRIVATE_KEY_FILE` to the exact value of `ACTIVE_PEM`
   and reconcile again; do not revoke the old key or remove either PEM until
   rollback is healthy and converged.

## Old-key revocation

Only after every activation check above succeeds:

1. On the GitHub App settings page, under **Private keys**, delete/revoke the
   old key.
2. In the current shell, read the new active destination from `host.env` and
   reassign `ACTIVE_PEM` to the exact old path recorded before activation.
   Validate both paths and their inequality before removing the old PEM; do not
   use a broad wildcard in a shared secrets directory:

   ```bash
   PEM_DEST=$(sudo grep -E '^CI_FLEET_GITHUB_APP_PRIVATE_KEY_FILE=' \
     /etc/ci-fleet/host.env | cut -d= -f2-)
   ACTIVE_PEM="/etc/ci-fleet/secrets/OLD-GITHUB-APP-KEY.pem"
   for pem in "$PEM_DEST" "$ACTIVE_PEM"; do
     [[ "$pem" =~ ^/etc/ci-fleet/secrets/[A-Za-z0-9._-]+\.pem$ ]] || exit 1
   done
   [[ "$ACTIVE_PEM" != "$PEM_DEST" ]] || exit 1
   sudo rm -f -- "$ACTIVE_PEM"
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
4. Before uninstalling, read the configured destination while `host.env` still
   exists and enumerate every retained rotation path explicitly. The
   uninstaller deliberately preserves `/etc/ci-fleet/host.env` and
   `/etc/ci-fleet/secrets`:

   ```bash
   PEM_DEST=$(sudo grep -E '^CI_FLEET_GITHUB_APP_PRIVATE_KEY_FILE=' \
     /etc/ci-fleet/host.env | cut -d= -f2-)
   RETIRED_PEMS=("$PEM_DEST")
   # Repeat for every retained old rotation path; never use a wildcard.
   RETIRED_PEMS+=("/etc/ci-fleet/secrets/OLD-ROTATION-ID.pem")
   for pem in "${RETIRED_PEMS[@]}"; do
     [[ "$pem" =~ ^/etc/ci-fleet/secrets/[A-Za-z0-9._-]+\.pem$ ]] || exit 1
   done

   sudo /opt/ci-fleet/manager/current/scripts/install-worker-controller.sh \
     --uninstall
   for pem in "${RETIRED_PEMS[@]}"; do
     sudo rm -f -- "$pem"
   done
   sudo rm -f -- /etc/ci-fleet/host.env
   ```

   Replace or repeat the example old path for every exact rotation path used by
   this app. If path extraction or validation fails, stop before uninstalling
   or removing `host.env`.
5. Remove remaining management-workstation, temporary, secret-manager, and
   backup copies according to their retention and secure-erasure policies. If
   the retired storage cannot guarantee file-level erasure (for example SSD,
   snapshot, or copy-on-write media), destroy the encrypted volume or its
   encryption key before disposal.
