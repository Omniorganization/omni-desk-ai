# OmniDesk 1.12.7 Root Monorepo Production GA Candidate

This version is intentionally named `1.12.7+root-monorepo-production-ga-candidate`. It hardens the 1.12.6 root-monorepo candidate without claiming customer-distribution Production GA.

## What changed

- Real GA evidence summaries now bind the audited JSON report by default with a `sha256:<digest>` artifact identity.
- Release workflow, Makefile, distribution package, and local summary generation all use the same audit-report binding path.
- Evidence-summary regression coverage now verifies source commit, job status, artifact name, and computed digest.
- Release version surfaces are bumped to `1.12.7+root-monorepo-production-ga-candidate` with matching Web, Desktop, Mobile, Helm, workflow, and package identities.

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
