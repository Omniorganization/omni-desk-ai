## 1.12.7+root-monorepo-production-ga-candidate

- Added a stronger main verification evidence contract with manual `workflow_dispatch`, commit-addressed artifact naming, SHA-256 evidence digests, a machine-readable artifact manifest, and release-policy enforcement.
- Added `scripts/check_main_verification_contract.py` so source gates fail closed if the post-merge evidence workflow or production evidence manifest loses its audit binding.
- Added `scripts/bump_version_surfaces.py` to perform a single consistency-gated release surface bump across Python, Docker, workflows, Web, Desktop, Mobile, Helm, shared contract, package locks, and release manifests.
- Replaced the offline AppSync monkey patch with explicit `ApplyingAppSyncStore` and `ApplyingDurablePostgresAppSyncStore` classes so uploaded operation application is a formal backend contract rather than a process-global runtime mutation.
- Added a `main` post-merge verification workflow that reruns source release gates, focused runtime regressions, Web Admin checks, Desktop checks, and emits a `main-verification-evidence.json` artifact bound to the merge commit.
- Hardened runtime auto-update trust boundaries so reconnect updates require Ed25519 manifest signatures, mandatory artifact/SBOM SHA-256 digests, disabled background-download enforcement, and a runtime-marker health check before activation.
- Closed the offline sync application gap by applying uploaded conversation/message/task/approval/notification operations into AppSync state instead of only recording inbox rows.
- Added a durable PostgreSQL offline-sync store wrapper that hydrates and mirrors local outbox, inbox, cursors, conflicts, and operation logs across restarts and multi-instance gateways.
- Added regression coverage for updater signature/hash/download/health gates and uploaded offline operation state mutation.
- Added `runtime.offline_mode` and update policy configuration so core model tasks are forced to the local Ollama profile while external channel/Gmail access is disabled in offline mode.
- Added durable AppSync local outbox/inbox/cursor/conflict records plus bidirectional `POST /app/sync` for reconnect upload, dedupe, conflict capture, and cursor updates.
- Added a reconnect worker that gates auto-update checks behind successful outbox synchronization.
- Added signed runtime update components for manifest fetch, Ed25519 verification, artifact/SBOM checks, release slot staging, health-check activation, audit logging, and rollback to `previous`.
- Added offline doctor checks and documentation for offline caches, required local models, signed manifests, and auto-activation boundaries.

- Bound Real GA evidence summaries to the audited JSON evidence file by default with a `sha256:<digest>` artifact identity.
- Kept workflow, Makefile, package, Web, Desktop, Mobile, Helm, Docker, and release-evidence version surfaces aligned after the evidence hardening change.
- Added regression coverage so generated evidence summaries record the source commit, artifact name, job status, and audit report digest without manual parameters.

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
