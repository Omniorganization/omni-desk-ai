# Industrial P0 / P1 / P2 Remediation

Status: source implementation candidate; customer-distribution GA remains evidence-gated.

## P0 runtime correctness

- PostgreSQL schema changes are versioned and executed by a dedicated migration command and Helm pre-install/pre-upgrade Job.
- Application startup validates the current migration version and does not require DDL privileges.
- The high-volume chat path uses direct SQL transactions instead of whole-state process caches.
- Chat reservation, generated conversation, user message and idempotency ownership are committed atomically.
- Stream events are append-only rows keyed by request and monotonically increasing sequence.
- Every event, completion and failure write is fenced by the exact database lease owner, preventing stale workers from overwriting a recovered request.
- Inherited lower-volume AppSync operations reload under a PostgreSQL advisory lock as a compatibility bridge until each domain receives a dedicated repository.
- Corrupt JSON development state is quarantined and startup fails closed instead of silently creating empty state.

## P1 engineering gates

- Repository governance, Ruff and Pyright run once per revision.
- Python 3.10-3.13 compatibility contexts remain present, while the full coverage suite runs once on Python 3.11.
- A real PostgreSQL service test exercises concurrent reservation, lease fencing, append-only replay and cross-instance visibility.
- Security aggregates Python, JavaScript/TypeScript, Rust and Flutter dependency scanning plus two-language CodeQL.
- Desktop checks build Linux, macOS and Windows targets; the required `desktop-tauri` context aggregates all three.
- iOS source validation now performs a real simulator build.

## P2 governance and deployment

- Runtime dependencies have explicit compatible upper bounds while hash-locked production and development lockfiles remain authoritative.
- Dependabot covers pip, npm, Cargo, Pub and GitHub Actions ecosystems.
- `VERSION` is enforced as the canonical release value for package metadata and the README release boundary.
- Helm templates use `.Release.Namespace`, configurable secret names, bounded AppSync pool/lease values and a stateless multi-replica PostgreSQL default.

## Validation boundary

Source and CI closure does not replace external evidence. Signed artifacts, device farms, store distribution, staging operations, failure injection, backup/restore, soak and organizational governance evidence remain required by the existing Real GA gate.
