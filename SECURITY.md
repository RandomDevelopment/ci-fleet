# Security Policy

## Reporting a vulnerability

Do not open a public issue containing credentials, exploit details, private network information, or an active vulnerability.

Use GitHub private vulnerability reporting when available. If private reporting is unavailable, contact services@randomdevelopment.biz with the repository name and a concise description.

## Secrets

This public repository must never contain:

- GitHub personal access tokens;
- runner registration tokens;
- GitHub App private keys;
- real `.env` files;
- cloud or infrastructure credentials;
- internal host inventories;
- production credentials;
- private network topology;
- unredacted diagnostic reports.

Example configuration must use obviously nonfunctional placeholders.

## Trust model

Self-hosted CI jobs with access to a Docker daemon must be treated as host-privileged. Containers sharing a Docker daemon are not independent security boundaries.

The intended fleet is for explicitly authorized repositories. Normal validation, releases, deployments, and jobs requiring internal network access must be routable to separate runner groups.

## Credential design

Long-lived GitHub App credentials belong in the fleet controller or an external secret manager. Ephemeral job runners should receive only short-lived, just-in-time runner configuration and the job-scoped secrets explicitly required by a workflow.
