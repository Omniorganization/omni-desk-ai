## 0.7.11-industrial-rc6

- Ensure release source archives include `.github/workflows` so CI/CD contract tests run green from clean packages.
- Install hash-locked dependencies in Docker image builds before installing the OmniDesk wheel with `--no-deps`.
- Add first-class `--help` handling to `scripts/production_smoke_test.py` for release smoke workflows.

# Changelog

## 0.7.11-industrial-rc6

- Removed placeholder base-image digests; Docker builds now require explicit digest-pinned `PYTHON_BASE_IMAGE_DIGEST`.
- Added sandbox-runner readiness, HMAC nonce replay protection, and allowed workspace root checks.
- Extended production smoke tests to verify remote sandbox health/readiness/execution.
- Wired model budget enforcement into `ModelRouter` before provider execution.
- Updated deployment docs and CI Docker scan to require digest-pinned base images.

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

## 0.7.0-industrial-rc-history

This release fixes all known test failures and adds final industrial hardening: governed memory writes, plugin runtime permission validation, Docker plugin isolation, PR promotion gates, SQLite migrations, release SBOM workflow, initialized runtime metrics and an 80% CI coverage gate with security/core/tools grouped gates.
