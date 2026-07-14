# Agent instructions

## Purpose

This repository maps projects to CI pools, deployment environments, logical host groups, and container images. It never stores secret values.

## Required verification

Before committing configuration changes, run:

```bash
./scripts/validate.sh --strict
```

## Hard rules

- Never add real `.env` files, credentials, tokens, private keys, cookies, or passwords.
- Do not weaken `public_repositories: false` for Docker-socket runner pools.
- Production environments must require approval and must not deploy automatically.
- CI runner hosts and application deployment hosts are separate roles.
- Image promotion uses immutable digests; do not rebuild separately for production.
- Reusable workflows must be pinned to a full reviewed commit SHA.
- Update schema and validator together.
