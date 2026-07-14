# Security policy

This repository stores deployment relationships and policy, not credentials.

Never commit passwords, tokens, private keys, GitHub App keys, SSH keys, TLS keys, production `.env` files, database connection strings containing credentials, cookies, or cloud credentials. A private repository is not a secret manager.

Configuration may list required secret **names**, such as `DATABASE_URL`, because their values live in GitHub Environments, root-owned host files, or an external secret manager.

If a secret is committed, revoke or rotate it immediately before removing it from Git history. Treat deletion from the latest commit as insufficient.
