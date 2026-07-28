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

## 3. Generate the private key

On the app page: Private keys → Generate a private key. GitHub downloads one
PEM. Store it only on the controller host, root-owned `0600`. It is never
committed, printed, or copied elsewhere — see [SECRETS.md](SECRETS.md).

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

## 6. Verify

On the controller host:

```bash
sudo /opt/ci-fleet/manager/current/scripts/github-app-token.sh \
  --env-file /etc/ci-fleet/host.env
```

Prints nothing secret; exit 0 means JWT signing and token exchange work.
Then a check-only reconcile validates the full fetch path without applying
anything:

```bash
sudo /opt/ci-fleet/manager/current/scripts/remote-reconcile.sh --check-only
```

`CHECK_OK` means the app can read the desired-state repository. A 403 or
"Repository not found" means the installation lacks the repository or the
`contents: read` grant — see Troubleshooting.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| `Repository not found` on fetch | repository not in the installation's selected list |
| 403 on content API after granting permission | permission change saved on the app but not yet accepted on the installation — reopen the installation page and approve the pending permission request |
| 401 on token exchange | wrong client ID, installation ID, or PEM path in `host.env` |

## Rotation and removal

- New controller: new app. Do not share one app across controllers.
- Rotate: generate a new key, update the PEM path, delete the old key.
- Retire: uninstall the app from the organization. The host keeps no usable
  credential.
