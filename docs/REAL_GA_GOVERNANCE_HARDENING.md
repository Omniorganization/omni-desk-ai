# Real GA Governance Hardening

This document is the operational closure plan for the remaining Real GA blockers that cannot be solved by source code alone.

## Status

The repository remains a Production GA Candidate until all items below are proven with live evidence and bound to the checked commit.

## Required Real GA control plane

1. Move the repository from a personal GitHub owner to a GitHub Organization.
2. Create production teams for security, release, code owners, and operations.
3. Configure `CODEOWNERS` so restricted paths require team review.
4. Enforce branch protection on `main`:
   - pull request required
   - required status checks required
   - signed commits required
   - code owner review required
   - stale review dismissal enabled
   - admins enforced
   - linear history required
5. Generate live team governance evidence into `release/external-evidence/control-plane/github-team-governance-live.json`.
6. Bind the live evidence to the exact commit used for release.

## Required Real GA runtime evidence

Real GA also requires complete external evidence for:

- native desktop build and signed artifact verification
- Android signed build and install smoke
- iOS signed IPA and real-device install smoke
- tri-app live smoke across Web Admin, Desktop Agent, and Mobile approval/chat flows
- rollback drill
- backup and restore drill
- supply-chain verification and artifact digest binding
- signed artifact binding in `control-plane/native-signed-artifact-binding.json`

## Candidate boundary

The `candidate` release channel may run audit-only checks, but it must not be labeled as Customer Distribution GA, Real GA, or Enterprise GA. The `real-ga` release channel must fail closed if any external evidence category is missing, skipped, expired, unbound, or produced for another commit.

## Security exception boundary

Dependency-review GHSA allowlists require a governed file in `release/security-exceptions/`, with owner, scope, expiry, impact, runtime reachability, compensating controls, and removal criteria. Expired exceptions block the security workflow.

## Production config boundary

Containerized production runtime must boot with `/data/config.production.yaml`. The ambiguous `/data/config.yaml` path is reserved for development and must not be used by Dockerfile runtime entrypoints or production compose environments.
