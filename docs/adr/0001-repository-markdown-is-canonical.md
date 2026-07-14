# ADR 0001: Repository Markdown is canonical

- Status: Accepted
- Date: 2026-07-14
- Tracks: #24, #26

## Context

Operators, contributors, automation, and downstream projects need procedures that match the exact version of the software they use. Maintaining mandatory instructions in both the repository and a GitHub Wiki would permit them to drift.

## Decision

Markdown committed to this repository is the sole source of truth for architecture, security policy, installation, upgrades, recovery, testing, and deployment procedures.

Documentation changes use the same pull-request review process as implementation changes. Instructions can therefore be pinned to a release or commit, read offline, validated by CI, and consumed by people or automation.

A GitHub Wiki must not contain independently maintained normative procedures. It may remain disabled or later contain clearly non-authoritative community material or an automatically generated mirror. A generated documentation site may improve navigation, but its source remains repository Markdown.

## Consequences

- Required procedures live under `docs/` or alongside the code they describe.
- Every mandatory workflow change updates its documentation in the same pull request.
- Link, example, and policy validation can be automated.
- Issue #26 can close when this ADR is merged.
