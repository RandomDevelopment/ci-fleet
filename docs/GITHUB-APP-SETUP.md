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

Lifecycle commands use canonical absolute paths so path comparisons and exact
deletion cannot change meaning. Before rotating, revoking, or directly retiring
an existing non-canonical path
(for example one containing `..`, repeated separators, or a symlinked parent),
resolve it with `sudo readlink -f -- "$ACTIVE_PEM"`, update `host.env` to that
canonical result, then reconcile and verify healthy convergence before
continuing. Record the canonical result as `ACTIVE_PEM`. New destinations must
also equal `realpath -m -- "$PEM_DEST"`.

The SSH workflow below is only for a host-local destination. For a
secret-manager-backed destination, do not write into the materialized mount.
Instead, use that manager's authenticated import/version operation to create a
new inactive version from `$PEM`, materialize it at a distinct canonical
`PEM_DEST`, and run the following verification. Activate that version only in
step 2 of the rotation procedure. If verification fails, retain the download,
remove only the new inactive version through the manager, and leave the active
version and path untouched:

```bash
# PEM_DEST is canonical and intentionally expanded client-side.
# shellcheck disable=SC2029
if
  [[ $PEM_DEST =~ ^/[A-Za-z0-9._/-]+$ ]] &&
  ssh "$CONTROLLER" \
    "test \"\$(realpath -m -- \"$PEM_DEST\")\" = \"$PEM_DEST\"" &&
  local_sha=$(sha256sum -- "$PEM") &&
  local_sha=${local_sha%% *} &&
  [[ "$local_sha" =~ ^[0-9a-f]{64}$ ]] &&
  remote_sha=$(ssh "$CONTROLLER" "sha256sum -- \"$PEM_DEST\"") &&
  remote_sha=${remote_sha%% *} &&
  [[ "$remote_sha" =~ ^[0-9a-f]{64}$ ]] &&
  test "$local_sha" = "$remote_sha" &&
  ssh "$CONTROLLER" \
    "test \"\$(stat -c '%U:%G' -- \"$PEM_DEST\")\" = 'root:root'" &&
  ssh "$CONTROLLER" \
    "test \"\$(stat -c '%a' -- \"$PEM_DEST\")\" = '600'"
then
  rm -f -- "$PEM" || exit 1
else
  printf 'manager import verification failed; retained download: %s\n' \
    "$PEM" >&2
  exit 1
fi
```

For a host-local destination, run the transfer and verification sequence below.
The path
validation makes it safe to quote `PEM_DEST` in the remote shell command, the
rotation guard rejects the active path, and an atomic hard-link creation refuses
any existing destination node before the key can appear at that path:

```bash
valid_pem_path() {
  [[ $1 =~ ^/[A-Za-z0-9._/-]+$ ]]
}
valid_pem_path "$PEM_DEST" || exit 1
if [[ -n "$ACTIVE_PEM" && "$PEM_DEST" == "$ACTIVE_PEM" ]]; then
  printf 'refusing to overwrite active PEM: %s\n' "$ACTIVE_PEM" >&2
  exit 1
fi
PEM_DIR=${PEM_DEST%/*}
[[ -n "$PEM_DIR" ]] || PEM_DIR=/
TRANSFER_ID=$(< /proc/sys/kernel/random/uuid) || exit 1
PEM_MARKER="$PEM_DIR/.ci-fleet-transfer-$TRANSFER_ID"
[[ $PEM_MARKER =~ ^/[A-Za-z0-9._/-]+$ ]] || exit 1
[[ -z "$ACTIVE_PEM" || "$PEM_MARKER" != "$ACTIVE_PEM" ]] || exit 1

# PEM_DIR and PEM_DEST are validated above and intentionally expanded locally.
# shellcheck disable=SC2029
if
ssh "$CONTROLLER" \
  "test \"\$(realpath -m -- \"$PEM_DEST\")\" = \"$PEM_DEST\"" &&
ssh "$CONTROLLER" "
  { test -d \"$PEM_DIR\" || install -d -m 0700 \"$PEM_DIR\"; } &&
  umask 077 &&
  tmp=\$(mktemp -- \"$PEM_DIR/.github-app-key.XXXXXX\") &&
  trap 'status=\$?;
    if [ \"\$status\" -ne 0 ] &&
       [ -e \"$PEM_MARKER\" ] && [ \"$PEM_MARKER\" -ef \"\$tmp\" ]; then
      if [ -e \"$PEM_DEST\" ] && [ \"$PEM_DEST\" -ef \"$PEM_MARKER\" ]; then
        rm -f -- \"$PEM_DEST\";
      fi;
      rm -f -- \"$PEM_MARKER\";
    fi;
    rm -f -- \"\$tmp\";
    exit \"\$status\"' 0 &&
  cat >\"\$tmp\" &&
  chown root:root \"\$tmp\" &&
  chmod 0600 \"\$tmp\" &&
  ln -T -- \"\$tmp\" \"$PEM_MARKER\" &&
  ln -T -- \"\$tmp\" \"$PEM_DEST\" &&
  rm -f -- \"\$tmp\" &&
  trap - 0
" <"$PEM" &&
  local_sha=$(sha256sum -- "$PEM") &&
  local_sha=${local_sha%% *} &&
  [[ "$local_sha" =~ ^[0-9a-f]{64}$ ]] &&
  remote_sha=$(ssh "$CONTROLLER" "sha256sum -- \"$PEM_DEST\"") &&
  remote_sha=${remote_sha%% *} &&
  [[ "$remote_sha" =~ ^[0-9a-f]{64}$ ]] &&
  test "$local_sha" = "$remote_sha" &&
  ssh "$CONTROLLER" \
    "test \"\$(stat -c '%U:%G' -- \"$PEM_DEST\")\" = 'root:root'" &&
  ssh "$CONTROLLER" \
    "test \"\$(stat -c '%a' -- \"$PEM_DEST\")\" = '600'" &&
  ssh "$CONTROLLER" \
    "test \"$PEM_MARKER\" -ef \"$PEM_DEST\""
then
  if ! ssh "$CONTROLLER" \
    "if test -e \"$PEM_MARKER\"; then
       test \"$PEM_MARKER\" -ef \"$PEM_DEST\" && rm -f -- \"$PEM_MARKER\";
     fi"; then
    printf 'verified transfer; retry idempotent marker cleanup before activation: %s\n' \
      "$PEM_MARKER" >&2
    exit 1
  fi
  if rm -f -- "$PEM"; then
    unset PEM local_sha remote_sha TRANSFER_ID PEM_MARKER
  else
    printf 'verified transfer, but could not delete downloaded PEM: %s\n' \
      "$PEM" >&2
    exit 1
  fi
else
  if ! ssh "$CONTROLLER" \
    "if test -e \"$PEM_MARKER\" && test \"$PEM_MARKER\" -ef \"$PEM_DEST\"; then
       rm -f -- \"$PEM_DEST\" \"$PEM_MARKER\";
     elif test -e \"$PEM_MARKER\"; then exit 1; fi"; then
    printf 'remote ownership cleanup failed; inspect inactive destination: %s\n' \
      "$PEM_DEST" >&2
  fi
  printf 'transfer verification failed; retained downloaded PEM: %s\n' "$PEM" >&2
  unset local_sha remote_sha TRANSFER_ID PEM_MARKER
  exit 1
fi
```

Use an equivalent privileged SSH workflow if direct root login is disabled.
If transfer, checksum, owner, or mode verification fails, the sequence stops.
It removes `PEM_DEST` only when the per-transfer hard-link marker proves that
this invocation created that inode, including after an ambiguous SSH result;
pre-existing destinations are preserved. The active controller PEM remains
untouched and the downloaded replacement is retained for diagnosis or a safe
retry. If marker cleanup itself returns an ambiguous SSH result, rerun only its
idempotent cleanup command before activation; do not rerun the transfer. Do not
revoke the old GitHub key.
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

`RECONCILE CONVERGED` means the app can read and validate the installed desired-state
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

   If token generation fails, immediately restore
   `CI_FLEET_GITHUB_APP_PRIVATE_KEY_FILE` in `host.env` to the exact recorded
   `ACTIVE_PEM`, verify token generation with the old key, and run normal
   reconciliation. Stop; retain both PEMs and do not revoke the old GitHub key.

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
   not `healthy` for an `active` controller or `maintenance` for a controller
   whose reviewed desired state is `drained` or `disabled`, or the final check
   does not report `RECONCILE CONVERGED`. Warning and unhealthy results always fail.
   Restore
   `CI_FLEET_GITHUB_APP_PRIVATE_KEY_FILE` to the exact value of `ACTIVE_PEM`
   and reconcile again; do not revoke the old key or remove either PEM until
   rollback is healthy and converged.
5. Before revoking the old key, verify that the newest complete rollback
   checkpoint references `PEM_DEST`, not `ACTIVE_PEM`:

   ```bash
   valid_pem_path() {
     [[ $1 =~ ^/[A-Za-z0-9._/-]+$ ]] &&
       [[ $(realpath -m -- "$1") == "$1" ]]
   }
   PEM_DEST=$(sudo grep -E '^CI_FLEET_GITHUB_APP_PRIVATE_KEY_FILE=' \
     /etc/ci-fleet/host.env | cut -d= -f2-)
   valid_pem_path "$PEM_DEST" || exit 1
   LATEST_CHECKPOINT=$(sudo find /var/lib/ci-fleet/checkpoints \
     -mindepth 2 -maxdepth 2 -type f -name .complete \
     ! -path '/var/lib/ci-fleet/checkpoints/.checkpoint.staging.*/*' \
     -printf '%T@ %h\n' | sort -nr | awk 'NR == 1 {print $2}')
   [[ -n "$LATEST_CHECKPOINT" ]] || exit 1
   sudo grep -Fx -- \
     "CI_FLEET_GITHUB_APP_PRIVATE_KEY_FILE=$PEM_DEST" \
     "$LATEST_CHECKPOINT/ci-fleet.env" >/dev/null || exit 1
   ```

   The rotation itself checkpoints the pre-rotation environment. If this gate
   fails, retain the old GitHub key and old PEM until a subsequent reviewed
   controller mutation creates and validates a checkpoint based on the new
   path. Do not revoke a key still required by the latest rollback checkpoint.

## Old-key revocation

Only after every activation check above succeeds:

1. In the current shell, read the new active destination from `host.env` and
   reassign `ACTIVE_PEM` to the exact old path recorded before activation.
   Put it in exactly one array: `OLD_LOCAL_PEMS` for host-local storage or
   `OLD_MANAGED_PEMS` for secret-manager-backed storage. Remove manager-backed
   material through that manager and verify it is absent before running this
   block. The block resolves host-local symlinks, validates every exact path,
   and removes only host-local material; do not use a wildcard:

   ```bash
   valid_pem_path() {
     [[ $1 =~ ^/[A-Za-z0-9._/-]+$ ]] &&
       [[ $(realpath -m -- "$1") == "$1" ]]
   }
   PEM_DEST=$(sudo grep -E '^CI_FLEET_GITHUB_APP_PRIVATE_KEY_FILE=' \
     /etc/ci-fleet/host.env | cut -d= -f2-)
   ACTIVE_PEM="/etc/ci-fleet/secrets/OLD-GITHUB-APP-KEY.pem"
   OLD_LOCAL_PEMS=("$ACTIVE_PEM")
   OLD_MANAGED_PEMS=()
   active_classifications=0
   for pem in "${OLD_LOCAL_PEMS[@]}" "${OLD_MANAGED_PEMS[@]}"; do
     [[ $pem =~ ^/[A-Za-z0-9._/-]+$ ]] || exit 1
     if [[ "$pem" == "$ACTIVE_PEM" ]]; then
       active_classifications=$((active_classifications + 1))
     fi
   done
   ((active_classifications == 1)) || exit 1
   PEM_DEST_BACKING=$(sudo readlink -f -- "$PEM_DEST") || exit 1
   valid_pem_path "$PEM_DEST_BACKING" || exit 1
   RESOLVED_OLD_LOCAL_PEMS=()
   for pem in "${OLD_LOCAL_PEMS[@]}"; do
     backing=$(sudo readlink -f -- "$pem") || exit 1
     valid_pem_path "$backing" || exit 1
     [[ "$backing" != "$PEM_DEST_BACKING" ]] || exit 1
     RESOLVED_OLD_LOCAL_PEMS+=("$backing")
     [[ "$pem" == "$backing" ]] || RESOLVED_OLD_LOCAL_PEMS+=("$pem")
   done
   OLD_LOCAL_PEMS=("${RESOLVED_OLD_LOCAL_PEMS[@]}")
   valid_pem_path "$PEM_DEST" || exit 1
   for pem in "${OLD_LOCAL_PEMS[@]}" "${OLD_MANAGED_PEMS[@]}"; do
     [[ $pem =~ ^/[A-Za-z0-9._/-]+$ ]] || exit 1
     pem_backing=$(sudo realpath -m -- "$pem") || exit 1
     [[ "$pem_backing" != "$PEM_DEST_BACKING" ]] || exit 1
   done
   for pem in "${OLD_MANAGED_PEMS[@]}"; do
     sudo test ! -e "$pem" || exit 1
   done
   for pem in "${OLD_LOCAL_PEMS[@]}"; do
     sudo rm -f -- "$pem" || exit 1
   done
   unset active_classifications backing pem_backing PEM_DEST_BACKING \
     RESOLVED_OLD_LOCAL_PEMS \
     OLD_LOCAL_PEMS OLD_MANAGED_PEMS
   ```

2. On the GitHub App settings page, under **Private keys**, delete/revoke the
   old key. Do not revoke it unless step 1 completed.
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
   exists and classify every retained rotation path explicitly. Revoke or
   unmount each secret-manager-backed path through that manager first and
   verify that it is absent; never pass a manager-owned path to `rm`.
   `LOCAL_PEMS` must contain only host-local files. The uninstaller deliberately
   preserves `/etc/ci-fleet/host.env` and `/etc/ci-fleet/secrets`:

   ```bash
   safe_pem_path() {
     [[ $1 =~ ^/[A-Za-z0-9._/-]+$ ]]
   }
   PEM_DEST=$(sudo grep -E '^CI_FLEET_GITHUB_APP_PRIVATE_KEY_FILE=' \
     /etc/ci-fleet/host.env | cut -d= -f2-)
   safe_pem_path "$PEM_DEST" || exit 1
   [[ $(sudo realpath -m -- "$PEM_DEST") == "$PEM_DEST" ]] || exit 1
   # Put PEM_DEST and every old path in exactly one array; never use a wildcard.
   LOCAL_PEMS=("/etc/ci-fleet/secrets/HOST-LOCAL-KEY.pem")
   MANAGED_PEMS=("/run/secret-manager/MANAGER-BACKED-KEY")
   # Retire both a host-local symlink and its backing key file.
   for pem in "${LOCAL_PEMS[@]}"; do
     if sudo test -L "$pem"; then
       backing=$(sudo readlink -f -- "$pem") || exit 1
       safe_pem_path "$backing" || exit 1
       LOCAL_PEMS+=("$backing")
     fi
   done
   PEM_INVENTORY=/etc/ci-fleet/retired-pem-paths
   configured_classifications=0
   for pem in "${LOCAL_PEMS[@]}" "${MANAGED_PEMS[@]}"; do
     safe_pem_path "$pem" || exit 1
     [[ $(sudo realpath -m -- "$pem") != \
        $(sudo realpath -m -- "$PEM_INVENTORY") ]] || exit 1
     if [[ "$pem" == "$PEM_DEST" ]]; then
       configured_classifications=$((configured_classifications + 1))
     fi
   done
   ((configured_classifications == 1)) || exit 1
   for pem in "${MANAGED_PEMS[@]}"; do
     sudo test ! -e "$pem" || exit 1
   done
   if ((${#LOCAL_PEMS[@]})); then
     printf '%s\n' "${LOCAL_PEMS[@]}" | \
       sudo install -m 0600 /dev/stdin "$PEM_INVENTORY"
   else
     sudo install -m 0600 /dev/null "$PEM_INVENTORY"
   fi || exit 1

   remove_retired_pems() {
     for pem in "${LOCAL_PEMS[@]}"; do
       sudo rm -f -- "$pem" || return 1
     done
   }
   if sudo /opt/ci-fleet/manager/current/scripts/install-worker-controller.sh \
        --uninstall &&
      remove_retired_pems &&
      sudo rm -f -- /etc/ci-fleet/host.env &&
      sudo rm -f -- "$PEM_INVENTORY"; then
     unset PEM_DEST PEM_INVENTORY LOCAL_PEMS MANAGED_PEMS
   else
     printf 'retirement cleanup failed; retained %s and host.env\n' \
       "$PEM_INVENTORY" >&2
     exit 1
   fi
   ```

   Replace or repeat the examples for every exact rotation path used by this
   app. If classification, manager cleanup, extraction, or validation fails,
   stop before uninstalling
   or removing `host.env`. On later cleanup failure, rebuild `LOCAL_PEMS`
   from the retained root-owned inventory before retrying exact-path removal;
   the inventory is deleted only after every PEM and `host.env` are removed.
5. Remove remaining management-workstation, temporary, and backup copies
   according to their retention and secure-erasure policies. If
   the retired storage cannot guarantee file-level erasure (for example SSD,
   snapshot, or copy-on-write media), destroy the encrypted volume or its
   encryption key before disposal.
