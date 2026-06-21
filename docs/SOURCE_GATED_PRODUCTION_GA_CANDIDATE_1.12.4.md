# OmniDesk 1.12.4 Root Monorepo Production GA Candidate

This version is intentionally named `1.12.4+root-monorepo-production-ga-candidate`. It hardens the 1.12.3 root-monorepo candidate without claiming customer-distribution Production GA.

## What changed

- Gmail OAuth state is now bound to the authenticated actor at `/oauth/gmail/start` and verified again during `/oauth/gmail/callback`.
- Caller-supplied OAuth state is no longer accepted by the start route; state is generated server-side, TTL-bound, redirect-bound, actor-bound, and single-use.
- API resource guards now support memory, SQLite, and PostgreSQL backends, with production multi-instance mode requiring a PostgreSQL backend and DSN.
- API resource guard denials are emitted to the audit sink so abuse throttling leaves an operational trail.
- Production model-budget configuration now requires a persistent ledger in addition to positive hard budget limits.
- Public `/ready` remains redacted; detailed readiness diagnostics stay behind authenticated `/admin/ready`.
- CI evidence manifests now bind the source commit, GitHub run URL, matrix cell, successful job result, expected uploaded artifact name, coverage files, and persisted logs.
- Focused regression coverage was added for Gmail OAuth routes, resource-guard persistence/configuration, agent-run abuse limits, model-budget enforcement, and public-readiness redaction.
- The generated distribution package carries `release-manifest.json`, portable `SHA256SUMS.txt`, and the current external GA audit status.
- The release remains blocked for customer GA while external evidence categories are missing.

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
