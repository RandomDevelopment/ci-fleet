# ADR 0005: One-command target-host setup

- Status: Accepted
- Date: 2026-07-14
- Tracks: #21, #27

## Context

The first host was completed with an automation agent coordinating several manual steps. That was useful for the prototype, but it is not the intended operator experience. A new VM or bare-metal Docker host must be deployable later from a terminal and a phone without depending on an agent or separate workstation.

## Decision

The supported operator entry point is one repository-owned command executed on the target host:

```bash
sudo ./setup.sh
```

The command is an idempotent orchestrator. It may call smaller reviewed scripts internally, including GitHub bootstrap and host installation, but operators must not need to reconstruct that sequence from prose.

The setup flow must:

- support fresh install, check, repair, upgrade, and explicit removal;
- accept reviewed configuration without putting secrets in command arguments;
- bootstrap or validate the GitHub App and runner group;
- serve any temporary callback from the target host;
- provide phone-friendly, non-secret approval links and optionally QR codes;
- exchange temporary codes and store credentials only on the target host;
- install and validate the controller, maintenance, cleanup, and health checks;
- be safe to rerun after success, interruption, or partial failure;
- emit a concise redacted report and exact remediation on failure.

Externally provisioned credentials remain supported. An automation agent may run the same command, but it is never an architectural requirement.

## Security boundaries

The setup command must preserve component separation, require an explicit private-repository allowlist for privileged runners, avoid global Docker cleanup, leave unrelated workloads untouched, and never print credentials or temporary codes.

## Consequences

Issues #21 and #27 implement parts of one public setup contract rather than separate operator journeys. Documentation should lead with `setup.sh`; lower-level scripts are advanced and recovery interfaces.
