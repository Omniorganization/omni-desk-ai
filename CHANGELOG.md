# Changelog

## 0.7.1-preproduction-hardening

- Reclassified runtime metadata away from `industrial.final` toward pre-production hardening.
- Fixed Docker CLI entrypoint ordering and moved container runtime installation to wheel artifacts.
- Added clean release hygiene via `.dockerignore` and `scripts/build_release.sh` metadata outputs.
- Added runtime shutdown hooks for SQLite-backed memory lifecycle.
- Added SQLite webhook job queue and background worker so webhook endpoints enqueue work instead of blocking provider requests.
- Added shared channel HTTP client with retry/backoff, 429/5xx handling and provider request-id normalization.
- Added model-router fallback, retry and circuit-breaker behavior.
- Hardened Gmail OAuth token persistence with private file permissions.

## 0.5.0-beta.1

- Added industrialization hardening: safe health checks, admin status, request IDs, metrics, privacy redaction, Docker sandbox option, PR-only self-upgrade guard, CI/security workflows, deployment and operations docs.
- Maintained Python 3.9 compatibility and full local test pass.


---

## Industrial Runtime Hardening

This version adds role-aware AdminAuth, IP allowlist, admin audit, one-time resume token consumption, Docker-capable shell backend, subprocess plugin output limits, self-upgrade state-machine evidence, and runtime memory governance.


---

## 0.7.0-industrial-final

This release fixes all known test failures and adds final industrial hardening: governed memory writes, plugin runtime permission validation, Docker plugin isolation, PR promotion gates, SQLite migrations, release SBOM workflow, initialized runtime metrics and a 65% CI coverage gate.
