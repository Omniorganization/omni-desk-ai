# OmniDesk 1.12.6 Root Monorepo Production GA Candidate

This version is intentionally named `1.12.6+root-monorepo-production-ga-candidate`. It hardens the 1.12.5 root-monorepo candidate without claiming customer-distribution Production GA.

## What changed

- PostgreSQL model-cost ledger now uses a dedicated SQL table and SQL aggregation instead of bounded JSON-state list scans.
- Side-effect routes share a unified idempotency store for resume, approvals, upgrade evaluation, and break-glass open/revoke actions.
- `/agent/run` idempotency now stores normalized responses for non-dict orchestrator results instead of leaving completed calls uncached.
- Break-glass open requests separate `approved_by` from `target_actor`; self emergency elevation is explicit, while delegated target actors require dual-approval metadata.
- Runtime status exposes side-effect idempotency health so operators can see running/completed replay keys.
- Real GA evidence summaries now include workflow run, job status, source commit, and optional artifact digest binding fields.
- Release version surfaces are bumped to `1.12.6+root-monorepo-production-ga-candidate` with matching Web, Desktop, Mobile, Helm, workflow, and package identities.

## Current rating boundary

The defensible classification is:

- GitHub-visible source shape: source-gated production-GA candidate.
- Distribution package: source-gated production-GA candidate with local source/package integrity evidence.
- Customer-distribution Production GA: blocked until the external evidence gate passes against real systems.

## Required external evidence

The blocking categories remain:

- Native Flutter/Rust/Tauri build evidence.
- Android, iOS, and desktop signed artifacts.
- APNS and FCM live delivery receipts.
- Multi-instance PostgreSQL soak.
- Rollback drill.
- Backup and restore drill.
- Self-healing failure-injection drill.

Run:

```bash
python scripts/check_external_ga_evidence.py .
```

This must pass without `--audit-only` before any customer-distribution GA claim.
