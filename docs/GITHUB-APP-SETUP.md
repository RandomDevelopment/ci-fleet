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

Run the reviewed checkout on the target Linux Docker host. The callback defaults
to loopback. For phone-first use, explicitly choose one private address that the
phone can reach; the script rejects public callback addresses.

```bash
sudo ./scripts/bootstrap-github.sh \
  --organization example-org \
  --instance example-ci-01 \
  --runner-group example-ci-experimental \
  --allow-repository example-org/example-repo
```

For phone access, append `--bind PRIVATE_IP --callback-host PRIVATE_IP` using the
same explicitly selected host-local private address; do not publish that address
in Git, logs, or support messages.

All names above are fictional. The script prints one non-secret local
`REGISTRATION_URL`. Open it, press the single registration button, install the
new App for **only** the requested private repositories, and return to the
terminal. The target host receives and exchanges the temporary code itself.
Neither the code nor any credential is copied through a phone, clipboard, chat,
email, issue, or second computer.

The bootstrap:

- requests only `contents: read`, metadata read, and organization self-hosted
  runner write permission;
- creates a private, independently revocable App identity for the host;
- verifies callback state and expires the callback after 30–1800 seconds;
- writes the PEM directly to `/etc/ci-fleet/secrets/github-app.pem` and the
  client/installation IDs to `/etc/ci-fleet/host.env`, root-owned mode `0600`;
- rejects public, archived, wrong-organization, broader App installation, and
  default/broad runner-group access;
- creates a missing selected-repository runner group, but never changes an
  existing group whose identity or access differs;
- destroys the conversion code, conversion response, JWTs, installation token,
  callback state, and temporary curl configurations on every exit.

Inspect a request without local writes or GitHub calls:

```bash
sudo ./scripts/bootstrap-github.sh --dry-run \
  --organization example-org --instance example-ci-01 \
  --runner-group example-ci-experimental \
  --allow-repository example-org/example-repo
```

After success, rerun the same command with `--check` to verify App ownership,
permissions, exact selected private repositories, exact runner-group access,
and host-local credential modes without changing the App or runner group.

Pass `--install --config-repo OWNER/REPO --config-ref REVIEWED_COMMIT` on the
initial live command for direct handoff to the idempotent host installer. Without
it, the redacted final report prints the exact installer shape. Externally
provisioned credentials remain supported; the generic installer never requires
this bootstrap on every run.

The controller exchanges a short-lived JWT signed with the PEM for an
installation token at runtime (`scripts/github-app-token.sh`). No token is
stored.

## Verify

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

The bootstrap never replaces or broadens an existing App or runner group. If a
new object was approved accidentally, stop before running the installer. An
organization owner must compare the App slug/ID and group name in the redacted
bootstrap report with GitHub's settings, verify the new group has no runners,
then remove only those exact newly created objects. Preserve and investigate any
pre-existing or mismatched object. App/group deletion and owner approval are live
GitHub-setting mutations and therefore require separate authorization; this
repository command does not automate them.

## Rotation

Use this ordered safety checklist for the normal host-local root-owned PEM
workflow. It is a set of gates, not a copy-and-paste shell program.

1. Before any key bytes arrive, use a controlled management workstation and
   pre-create the one approved replacement path,
   `/etc/ci-fleet/secrets/github-app.next.pem`. Pre-create it as root-owned
   `0600` inside the root-owned `0700` directory. Generate a new GitHub key,
   transfer and verify it there, and keep the old key and
   `/etc/ci-fleet/secrets/github-app.pem` active for rollback. Delete the
   workstation copy immediately after verified transfer and before token,
   reconciliation, health, or convergence checks.
2. Update the protected controller identity configuration to select the new PEM.
3. Require new-key activation, reconciliation, health, or convergence checks
   all to succeed with the new key, including the installed manager's token
   verification and installed-state convergence check.
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
