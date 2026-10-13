# Secrets Model

## Rule

Do not commit plaintext secrets to this repository or to a separate private Git repository. Git history is not a secret manager.

## Secret classes

### Controller credentials

Long-lived credentials, such as a GitHub App private key, belong only in the fleet controller or an external secret manager.

The controller should use that credential to generate short-lived just-in-time runner configuration. Job runner containers must not receive the long-lived key.

### Workflow secrets

Project test or integration secrets belong in GitHub repository or environment secrets owned by the calling project. Reusable workflows should accept only explicitly declared secrets.

Normal validation should use no secret when possible. Deployment credentials must not be available to the normal shared validation pool.

### Host-local identity and secrets

A single-host prototype may use root-owned files outside the repository checkout, for example:

```text
/etc/ci-fleet/secrets/github-app.pem
/etc/ci-fleet/host.env
/etc/ci-fleet/ci-fleet.env
```

`host.env` contains the App client ID, installation ID, private-key path, and runner TTL. `ci-fleet.env` is rendered from reviewed desired state plus those approved host fields. Neither contains the PEM or application secrets, but both remain root-owned mode `0600` because they describe the controller identity and private installation.

Credential provisioning files should be owned by root and readable only through their intended service's approved delivery mechanism. GitHub App private keys are mounted only into the controller. The installer never accepts a private key, token, or Git credential in a command-line argument.

For a distributed fleet, use a secret manager or encrypted configuration system with independently revocable host identities.

### Planned endpoint observer access

The [endpoint observation design](ENDPOINT-UI-DESIGN.md#access-on-port-80) introduces a separate backend read bearer only when its packaging and access gates are implemented and separately authorized. No observer credential or listener is supported by this design change alone.

Provision 32 random bytes as 43 unpadded base64url ASCII characters without a trailing newline, in root-owned mode-`0600` files outside Git. The privileged installer reads the protected access references in `/etc/ci-fleet/observer/access.json`. Systemd `LoadCredential` supplies the observer's copy through its isolated credential directory; the observer does not read the private `/etc/ci-fleet` tree. Provision the gateway copy independently through its approved secret mechanism.

The gateway strips browser-supplied authentication headers and injects the backend bearer. Every observer request requires it, including loopback requests. Keep it separate from controller, reporting, deployment and receiver credentials. Browsers and ordinary jobs receive no copy; credentials, authorization headers and query strings never enter logs.

Rotation revokes the old observer value before activating the new gateway copy. A temporary observation outage is acceptable. Committed disablement, uninstall or unsupported downgrade removes observer-owned metadata but retains operator-owned access policy and provisioning files for explicit disposition. Removing the service does not by itself revoke the bearer. Retire or revoke both provisioning copies through the authorized credential process before retiring their operator policy.

## Docker Compose pattern

The controller service receives its host-local private key as a mounted file:

```yaml
services:
  controller:
    secrets:
      - github_app_private_key

secrets:
  github_app_private_key:
    file: /etc/ci-fleet/secrets/github-app.pem
```

The runner service must not receive this secret.

## Public examples

Committed examples may document:

- variable names;
- expected file locations;
- obviously nonfunctional organization names;
- safe default capacity;
- permission requirements.

Committed examples must not include:

- real tokens or keys;
- real environment files;
- internal addresses;
- production endpoints;
- private host inventories;
- encoded secrets.

Base64 is encoding, not encryption.
