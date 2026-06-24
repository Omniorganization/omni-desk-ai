## 1.12.6+root-monorepo-production-ga-candidate

- Replaced bounded PostgreSQL JSON-state model-cost aggregation with a dedicated SQL ledger table and direct SQL summary queries.
- Added a shared side-effect idempotency store for resume, approvals, self-upgrade evaluation, and break-glass open/revoke routes.
- Normalized and cached non-dict `/agent/run` orchestrator results so idempotent replay covers the full response surface.
- Clarified break-glass semantics by separating authenticated approver identity from optional target actor; delegated target actors now require dual-approval metadata.
- Extended runtime status and Postgres health checks to surface side-effect idempotency and SQL-backed model-cost readiness.
- Extended Real GA evidence summaries with workflow run, job status, source commit, and optional artifact digest binding.

## 1.12.5+root-monorepo-production-ga-candidate

- Added trusted proxy handling for API resource guard client keys so `x-forwarded-for` is ignored unless the immediate peer matches configured proxy IPs or CIDR ranges.
- Tightened production validation to require the API resource guard PostgreSQL backend in production, not only when multi-instance storage is explicitly enabled.
- Tightened production model-budget validation so production profiles require PostgreSQL-backed shared storage for the durable model-cost ledger.
- Made the runtime model router fail closed when a persistent ledger is explicitly required but no model cost store is configured.
- Replaced `/agent/run` raw `dict` input with a Pydantic schema that bounds message size and rejects unknown fields such as caller-supplied actor.
- Added OAuth state cleanup for expired and used Gmail OAuth states.
- Documented the new production proxy and ledger constraints in Docker and Helm production configuration examples.

## 1.12.4+root-monorepo-production-ga-candidate

- Bound Gmail OAuth state to the authenticated actor on start and callback so callback exchange cannot be completed under a different actor.
- Added persistent API resource guard backends and fail-closed production validation for multi-instance deployments that require PostgreSQL-backed rate-limit state.
- Emitted audit events for API resource guard denials and added dedicated abuse-limit coverage for public, chat, and `/agent/run` surfaces.
- Required a persistent model-budget ledger in production profiles so per-actor model spend attribution is durable rather than process-local.
- Extended CI evidence manifests to include successful job result and uploaded artifact identity for each Python matrix cell.
- Added focused regression tests for OAuth routes, resource guards, model-budget enforcement, public readiness redaction, and agent-run abuse limits.

## 1.12.3+root-monorepo-production-ga-candidate

- Added API resource guards for public and authenticated surfaces: body-size caps, IP/endpoint/actor/role/org rate limits, chat and `/agent/run` actor limits, and concurrency backpressure.
- Redacted public `/ready` to only return `ok`; detailed runtime, database, sandbox, and missing-secret diagnostics now stay behind `/admin/ready`.
- Made model spend budgets positive by default, fixed durable per-actor model-cost attribution, and restored a hard `per_task_max_llm_calls` cap.
- Added `OMNIDESK_REQUIRE_PRODUCTION_GUARDS=true` as an explicit production-safety signal and wired it into Docker, Helm, and systemd production profiles.
- Extended release gates and tests so resource guards, spend budgets, and production guard enforcement remain source-controlled candidate requirements.

## 1.12.2+root-monorepo-production-ga-candidate

- Added per-matrix CI evidence manifests that bind commit SHA, GitHub Actions run metadata, coverage JSON/XML hashes, and captured ruff/pyright/pytest logs.
- Added static CI evidence and security workflow policy gates so source-trunk evidence capture, CodeQL, gitleaks, dependency review, license policy, and Trivy coverage remain enforced.
- Strengthened release-channel policy so explicit `real-ga` runs reject candidate/source-gated artifact naming and require external evidence audit `blocker_count == 0` with `status == passed`.
- Added `scripts/check_release_channel_policy.py` and wired it into CI, GA release gate, Makefile, and a dedicated Release Policy workflow.
- Added a source-controlled branch protection contract and expanded CODEOWNERS coverage for workflows, scripts, deploy assets, security code, and release evidence.
- Hardened source-root hygiene and ignore rules so generated OmniDesk package directories and wrapper zips cannot remain in or re-enter the source trunk.
- Reclassified the next package as a source-gated production GA candidate rather than a completed customer-distribution GA.
- Promoted the repository from a package-only root toward a standard monorepo shape with root-level README, architecture, security, contribution, version, license, package-boundary, infra, test-layer, workflow, and release schema entrypoints.
- Added `scripts/check_monorepo_layout.py` and regression coverage so the repository cannot silently regress to a package-only root.
- Added `/api/chat` as a unified audited non-streaming chat endpoint across Web, Desktop, Mobile, and direct API clients, while keeping `/api/chat/stream` fail-closed until streaming evidence is production-gated.
- Added distribution package manifest generation and verification so package-only GitHub trees expose source commit, artifact SHA-256 values, artifact sizes, and external GA blocker status.
- Extended `package-final-gate` to verify `release-manifest.json` alongside release hygiene and portable checksums.
- Added documentation clarifying that the public package-only tree can rate as Pre-GA while the full source package has stronger source-gated controls.

## 1.11.8+source-gated-enterprise-chat-candidate

- Added `/app/conversations/{conversation_id}/ask` for Gateway-audited direct model Q&A through `ModelRouter(task="chat")`.
- Persisted user and assistant chat messages with provider/model/profile/usage/trace metadata across JSON and PostgreSQL AppSync stores.
- Added Web Admin, Desktop Tauri, and Mobile Flutter ask-mode clients/UI while keeping desktop task execution on the existing approval-gated `/messages` path.
- Added chat smoke tests for backend routing, role enforcement, idempotent replay, shared API contract, and Web/Desktop/Mobile client endpoints.
- Added structured `omnidesk doctor`, `omnidesk onboard`, `omnidesk evidence doctor`, `omnidesk channel onboard`, `omnidesk device pair`, and `omnidesk app connect` entrypoints.
- Added Channel Capability Matrix and Channel Identity Firewall so unknown senders default to pairing, OAuth/device drift triggers reverification, and high-risk channel actions require owner approval.
- Added Codex-style execution profiles, CIK Guard, signed skill registry, AGENTS rules, and an `ai/*` repair PR workflow.
- Split self-healing into observe-only review, structured repair proposal, deterministic gate, and PR evidence bundle modules.
- Added a minimal Agent Eval Harness and Desktop Control Hub status model for runtime/evidence visibility.
- Kept customer-distribution GA blocked until real native build, signing, APNS/FCM, Postgres soak, rollback, backup/restore, and self-healing failure-injection evidence is attached.

## 1.10+production-ga-real-evidence-audit

- Added a real external GA evidence checker that blocks customer-distribution GA without verifiable native build, signed artifact, APNS/FCM, Postgres soak, rollback, backup/restore, and self-healing failure injection evidence.
- Added `release/external-ga-evidence.required.json` and a generated current audit report for the required evidence layout.
- Added Makefile targets for source-safe evidence audit and strict distribution evidence gating.
- Wired promotion workflow to require external evidence before production deployment.
- Added explicit self-healing failure injection report status without fabricating a successful drill.

## 1.09+production-ga-self-healing-evidence

- Fixed Web Admin Docker runtime packaging by committing `apps/web-admin-next/public/.gitkeep`.
- Removed the Desktop Tauri undeclared `dirs::home_dir()` dependency and made Rust source checks fail closed with `cargo check --locked`.
- Fixed Mobile Flutter push token registration to use the named `platform` argument and added an AppDelegate-compatible iOS plugin registrant placeholder for source packages.
- Upgraded tri-app release gates to require npm clean installs, locked Rust checks, Dart analysis, Android appbundle builds, and iOS IPA release builds in real release environments.
- Added GA runtime evidence tests and shared device-signature headers to the tri-app API contract.

## 1.07+production-ga-release-integrity

- Expanded version consistency from backend-only release metadata to the full release surface: Python, Docker, workflows, Helm, release evidence, shared API contract, Web Admin, Desktop Tauri, and Mobile Flutter.
- Hardened release hygiene to reject frontend build/cache artifacts, native build outputs, runtime databases, keys, and token files before packaging.
- Made clean source zip packaging non-mutating and deterministic, with explicit Python interpreter fallback for environments without a `python` shim.
- Adjusted the GA release gate to allow the top-level `.git` directory in live CI checkouts while release packages and generated zips remain VCS-free.
- Added regression coverage for the stricter hygiene rules, deterministic clean zip behavior, and live-checkout GA gate behavior.

## 1.06+production-ga-closure-hardening

- Hardened the GA release gate so Helm chart and evidence-manifest checks derive the current project version from `pyproject.toml` instead of carrying stale release literals.
- Fixed webhook forced-signature test discovery for newer FastAPI/Starlette route objects that do not expose an `endpoint` attribute.
- Revalidated the source package with an external test virtualenv so release hygiene remains a real gate and local dependency environments do not pollute the source tree.
- Regenerated the tri-app source release set with portable SHA256 checksums and clean package roots.

## 1.05+production-ga-closure

- Promoted the RC3 line into a GA source package with a unified `1.05+production-ga-closure` identity across Python, Docker, workflows, Helm, shared API contract, Web Admin, Desktop Tauri, and Mobile Flutter.
- Replaced the stale Helm `0.7.32` chart identity with `1.05.0` / `1.05+production-ga-closure`, and made the final app image digest a required release-pipeline injection instead of a static placeholder.
- Hardened Web Admin security by removing CSP `unsafe-eval` / `unsafe-inline`, adding `object-src 'none'`, enforcing `__Host-` session cookies, and adding bounded session lifetime.
- Closed production WebSocket query-token compatibility: query auth is now opt-in for non-production only and is rejected by the production validator.
- Added production device enrollment guardrails: desktop/mobile registrations must provide a public key and cannot use predictable device IDs in production.
- Added per-install Desktop keypair generation with OS secure storage and per-install Mobile Ed25519 keypair generation with `flutter_secure_storage`.
- Added a GA release gate script that checks version consistency, package hygiene, Helm digest injection, Web CSP, session cookie hardening, WebSocket auth policy, AppSync Postgres policy, and per-install native device identity.
- Added Helm production AppSync Postgres config and strengthened production validation for AppSync multi-instance safety.

## 1.02+production-rc2-tri-app-hardening

- Promoted the tri-app line from enterprise-staging to production-rc2 with a unified 1.02 release identity across Python, Docker, workflows, shared API contract, Web Admin, Desktop Tauri, and Mobile Flutter.
- Split tri-app readiness into source and release modes so source CI does not fail solely because Flutter/Rust signing toolchains are absent.
- Added PostgreSQL transaction primitives for AppSync task claim, idempotency, leases, push outbox, and device credential challenge/verify flows.
- Added Web Admin server-side session proxy routes so browser clients can call Omni through HTTP-only session cookies and CSRF checks instead of exposing Gateway bearer tokens.
- Added Desktop Runtime capability executor interfaces with approval/scope enforcement hooks and non-shell dry-run execution as the default safe Production RC mode.
- Expanded Mobile Flutter scaffolding with iOS/Android native release files, Firebase push registration hooks, and fail-closed approval confirmation policy.
- Added release zip hygiene enforcement to exclude caches, bytecode, egg-info, node_modules, build outputs, and local artifacts.

## 0.7.39+controlled-staging-tri-app-hardening

- Hardened the Tri-App Foundation into a controlled-staging release.
- Added configurable AppSync persistence with `json` and PostgreSQL-backed modes.
- Added AppSync idempotency keys for message/task creation and approval decisions.
- Added desktop runtime task claim/lease flow at `/app/runtime/desktop/claim`.
- Made `/app/ws` authenticated with viewer-or-higher token support.
- Added device enrollment metadata fields for public key, token hash, organization, push token, capabilities, and heartbeat continuity.
- Hardened Web Admin with in-memory session token handling, role-aware approval controls, mandatory audit reason, idempotency headers, security headers, Dockerfile, and release gate notes.
- Hardened Desktop Tauri with OS secure storage commands, strict CSP, task claim/complete flow, updater artifact flag, signing gate notes, and keychain dependency.
- Hardened Mobile Flutter with secure storage, biometric/PIN approval confirmation hook, idempotency headers, push notification dependency hooks, Android/iOS release scaffolds, and signing gate notes.
- Added `tri-app-quality.yml` CI workflow and expanded release readiness checks for claim endpoint, Tauri CSP, secure desktop storage, mobile secure storage, and platform directories.

## 0.7.36+industrial-ga20-native-channels

- Promoted Slack, Discord, Google Chat, Signal, iMessage, Microsoft Teams, Matrix, and QQ from OpenClaw reference catalog entries to configurable native channel adapters.
- Kept WhatsApp Cloud, Telegram, LINE, and WeChat Official as native adapters and added user-facing aliases for WhatsApp, WeChat, and Teams.
- Added signed webhook routes, parse/envelope contracts, outbound `send_text` implementations, idempotency metadata, and runtime adapter registration for the expanded channel set.
- Expanded UI Bridge allowed apps, connector coverage validation, production config templates, and native-channel documentation.
- Added regression tests proving the requested channels are native catalog entries and parse normalized inbound messages without bypassing approval or audit gates.

## 0.7.35+industrial-ga19-openclaw-aligned

- Added an OpenClaw-aligned channel ecosystem catalog that clearly separates OmniDesk native adapters from OpenClaw reference channels.
- Added persistent per-channel/per-actor interaction profiles so planning can learn preferred surfaces without bypassing approvals.
- Added planner integration for OpenClaw-style reference channels such as Slack, Discord, Teams, Matrix, Zalo, QQ, WebChat, Voice Wake, and Live Canvas.
- Added `/admin/channels/ecosystem` for read-only operator visibility into channel support status and required security controls.
- Kept OmniDesk security and production closure model authoritative: webhook signatures, allowlists, permission approvals, dual approval, sandboxing, audit logs, release hygiene, workflow pinning, and deployment checks remain the gate.

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

## 1.05+production-ga-closure

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
