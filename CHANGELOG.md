## 0.7.34+industrial-ga18-full-closure

- Closed GA17 static quality blockers by fixing ruff export/import issues and the admin route verifier type contract.
- Raised HA runtime confidence with in-memory contract tests for PostgreSQL-backed approvals, runs, jobs, outbound messages, webhooks, break-glass sessions, token budgets, model costs, learning experiments, and governed memory.
- Fixed PostgreSQL stale job recovery to requeue by job id instead of dedupe key.
- Fixed readiness semantics so single-instance SQLite deployments do not fail `/ready` solely because `multi_instance_safe` is false.
- Added strict `-W error` cleanup for OTLP probe test sockets and local `.serena` tooling artifacts before release hygiene checks.
- Covered WORM audit checkpoint signing, unsigned local checkpoints, verification, and tamper detection.

## 0.7.33+industrial-ga17-full-closure

- Removed stale current-version assertions from release governance tests.
- Added hash-locked enterprise runtime dependencies for PostgreSQL and switched Dockerfile to requirements.enterprise.lock.
- Completed HA repository factory wiring for learning experiments, memory, token budget, and model cost state.
- Added PostgreSQL JSONB-backed runtime stores for memory, token budget, model cost, and learning experiments.
- Strengthened /ready and /admin/ready to check runtime repositories, required secrets, remote sandbox configuration, and plugin/schema readiness.
- Made Helm HA app pods stateless by default and gated any PVC behind persistence.enabled.
- Added GA17 closure tests and stricter supply-chain/Kubernetes/deployment gates.

## 0.7.32+industrial-ga16-ha-closure

- GA16 HA closure: routes runtime state construction through RepositoryFactory instead of direct SQLite construction for approvals, dual approvals, break-glass sessions, webhook replay, job queue, outbound messages, and run state.
- Adds PostgreSQL JSONB-backed runtime state stores for HA/multi-instance approval, resume-token, webhook replay, job, and outbound-message flows.
- Wires break-glass sessions into AdminAuth as an auditable, time-boxed role elevation path that still requires valid base authentication.
- Adds /ready and /admin/ready readiness checks and moves Docker/Kubernetes health checks away from shallow /health.
- Closes Docker Compose PostgreSQL DSN/service/healthcheck/secret/volume requirements and strengthens Helm NetworkPolicy, PVC, placeholder URL, and probe contracts.

## 0.7.31+industrial-ga15-enterprise-closure

- Enforced critical dual approval through ApprovalStore, API routes, and Orchestrator resume consumption.
- Added Break-glass admin open/status/revoke runtime API with TTL, distinct approver, and audit trail.
- Added remote Docker plugin sandbox support through sandbox-runner stdin payloads, avoiding Docker socket dependency in the app container.
- Closed production config template gaps: capability/plugin alignment, break-glass HMAC policy, PostgreSQL DSN validation, and generated-config validation.
- Expanded Helm production chart with ServiceAccount, Service, ConfigMap, PDB, HPA, resources, probes, topology spread, preStop, and non-placeholder digests.
- Strengthened Kubernetes contract checks against placeholder digests, missing HA primitives, and broad namespaceSelector policies.
- Added GA15 regression tests for dual approval, break-glass routes, remote plugin sandbox selection, and production config validation.

## 0.7.30+industrial-ga14-production-closure

- Added GA14 production closure drill for contract-only and live storage / OTLP / audit checkpoint validation.
- Added Kubernetes / Helm production security contract checker and wired it into CI and scheduled production-closure workflow.
- Added runtime storage health checks that can exercise transactional outbox enqueue / claim / complete behavior.
- Added dependency-free OTLP collector probe using the same runtime exporter path as request tracing.
- Added time-boxed break-glass session store with distinct approver enforcement and audit log emission.
- Wired runtime security closure stores for dual approval, break-glass, and WORM audit checkpoints.
- Hardened production validation so critical risk requires dual approval and break-glass requires signed audit checkpoint material.
- Added GA14 enterprise closure documentation and regression tests covering the new production proof points.

## 0.7.27+industrial-ga11-enterprise

- Closed release image digest generation loop: release workflow now builds and pushes the OCI image, reads the registry digest, and writes that digest into release metadata without a manual digest input.
- Expanded Cosign verification to the complete release payload artifact set.
- Added OTLP/OTel collector deployment assets and traceparent propagation helpers.
- Hardened self-learning promotion with sample-size, confidence, safety, rollback, and human-approval gates.
- Added repository abstraction, PostgreSQL store skeletons, transactional outbox schema, and enterprise deployment readiness checks.

# Changelog

## 0.7.27+industrial-ga11-enterprise

- Closed GA9 release-contract drift by making image digest an explicit workflow input and verifying the same digest across release metadata, provenance, promotion, smoke and rollback gates.
- Unified remote sandbox runner production variables, required runner HMAC in production validation, and removed the ambiguous legacy allowlist variable from full Compose deployment.
- Added tamper-evident permission audit hash chaining, constant-time resume-token checks, pytest marker tiers, CODEOWNERS, branch-protection guidance and deployment readiness guardrails.
- Expanded runtime trace propagation across planner, resume, tool execution and approval paths with stable run/plan/step correlation labels.

## 0.7.25+industrial-ga9

- Final GA hardening: automatic release image digest from workflow-built OCI image.
- Added workflow semantic linting, systemd production profile, SQLite maintenance, disk guard, OpenTelemetry-style tracing helpers, alert drill, and expanded SLSA provenance.

- Added Cosign/Sigstore/SLSA supply-chain workflow gates and verification contracts.
- Added soak-test workflow and deterministic soak-test harness for lifecycle, SQLite contention, approval-race, webhook-storm, sandbox-timeout and memory-purge-race scenarios.
- Added ResourceWarning regression coverage for CLI/runtime SQLite lifecycle.
- Added optional connector coverage classification and real-environment verification checklist.

## 0.7.23+industrial-ga7

- Closed strict `-W error` CLI SQLite cleanup by making runtime context shutdown sweep escaped local SQLite connections.
- Made release workflows require a digest-pinned OCI image and verify that the same digest is written to release metadata.
- Switched GitHub workflow shell-script execution to explicit `bash scripts/...` calls and added a script executable-bit contract check.
- Preserved executable permissions for shipped shell scripts in the release source package.

## 0.7.21+industrial-ga5

- Bumped the release identity after GA4 source and workflow changes so one version maps to one immutable artifact set.
- Expanded release and promotion attestation workflows to cover every file under `dist/`, including sdists, signatures, and release manifests.
- Strengthened managed Chrome profile attestation with launcher HMAC signatures, private marker permissions, process argv checks, and profile directory permission checks.
- Added resume recovery state markers so approval/resume crashes can surface as `resuming` or `resume_failed` instead of silently stranding a waiting run.

## 0.7.20+industrial-ga4

- Fixed release/runtime version consistency across pyproject, package metadata, Dockerfile, staging workflow, and production promotion workflow.
- Added `scripts/check_version_consistency.py` and wired it into CI so artifact/image/runtime version drift fails before merge.
- Fixed governed self-upgrade state progression so artifact generation uses the state machine and governance evaluation resumes from the current state.
- Added managed Chrome profile attestation checks and a launcher script to reduce the risk of attaching browser control to a personal Chrome profile.
- Added outbound ambiguous-send status, best-effort provider reconciliation controls, and metrics for network/5xx uncertainty.
- Strengthened release metadata and artifact verification with SBOM, checksums, and signature-manifest hashes plus artifact set consistency checks.
- Added production observability starter assets and a full Docker Compose topology for app plus remote sandbox runner.

## 0.7.19+industrial-ga3

- Hardened approval/resume boundaries: run resume tokens are atomically consumed and approvals now have a one-time consumed state.
- Redacted BrowserTool tab enumeration by default and made list_tabs approval-gated; production browser control now requires a dedicated Chrome profile.
- Added provider idempotency capability matrix and propagated outbound idempotency/client request IDs through shared channel HTTP calls.
- Unified shell and sandbox-runner command allowlists through a shared command policy module.
- Aligned remote sandbox client archive limits with runner-side defaults.
- Switched webhook rate limiting to atomic SQLite UPSERT semantics.
- Tightened self-upgrade state transitions so proposals cannot jump directly into CANARY.
- Added GitHub Actions SHA pin checker, outbound reconciliation utility, SQLite backup/verify/restore scripts, and scheduled backup-restore drill workflow.

## 0.7.18+industrial-ga2

- Fixed staging and production GitHub Actions YAML parsing by quoting workflow input descriptions and normalizing multi-line attestation commands.
- Added `scripts/check_github_workflows.py` and wired it into CI so workflow YAML regressions fail before merge.
- Made production artifact attestation a non-optional promotion gate.
- Hardened `/agent/run` shared-secret semantics: configured secrets are now mandatory and unconfigured body secrets are rejected.
- Added production runtime fail-closed behavior for the argv shell backend.
- Added client-side remote sandbox archive limits for file count, total bytes, per-file bytes, symlink rejection, special-file rejection, and noisy directory exclusions.
- Added outbound idempotency keys, unique queue de-duplication, provider request-key propagation, and lookup/mark-sent helpers.
- Added regression coverage for workflow validation, outbound idempotency, sandbox archive limits, production shell backend blocking, and strict agent-run secrets.

## 0.7.12-industrial-rc7

- Fix strict CI SQLite lifecycle races by skipping active context-managed connections during global cleanup and removing runtime-wide global SQLite closes.
- Add default-off high-risk capability registry gates for shell, computer, git, pull request, browser, UI bridge, Gmail, channels, and plugins.
- Pin GitHub Actions to commit SHAs and add production promotion smoke/SLO gate workflow.
- Make `scripts/check_slo.py` independently runnable with `--help`, `--metrics-file`, `--json`, and `--fail-on-error-budget`.
- Reject macOS package metadata such as `.DS_Store` and `__MACOSX` in release hygiene checks.
- Close sandbox-runner SQLite nonce connections under strict `-W error` test runs.
- Add `--strict-sandbox` production smoke checks for archive traversal, symlink escape, allowlist rejection, nonce replay, and oversized archives.
- Add `Makefile` entries for local, strict, CI, production config, compose, and strict sandbox smoke workflows.
- Ensure release source archives include `.github/workflows` so CI/CD contract tests run green from clean packages.
- Install hash-locked dependencies in Docker image builds before installing the OmniDesk wheel with `--no-deps`.
- Add first-class `--help` handling to `scripts/production_smoke_test.py` for release smoke workflows.
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
