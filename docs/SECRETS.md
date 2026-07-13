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

### Host-local secrets

A single-host prototype may use root-owned files outside the repository checkout, for example:

```text
/etc/ci-fleet/secrets/github-app.pem
/etc/ci-fleet/ci-fleet.env
```

Credential files should be owned by root, readable only by their intended service, and mounted only into the controller.

For a distributed fleet, use a secret manager or encrypted configuration system with independently revocable host identities.

## Docker Compose pattern

A future controller service may receive a host-local secret as a mounted file:

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
