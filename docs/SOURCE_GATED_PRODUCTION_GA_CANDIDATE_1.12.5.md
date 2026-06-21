# OmniDesk 1.12.5 Root Monorepo Production GA Candidate

This version is intentionally named `1.12.5+root-monorepo-production-ga-candidate`. It hardens the 1.12.4 root-monorepo candidate without claiming customer-distribution Production GA.

## What changed

- API resource guard no longer trusts `x-forwarded-for` from arbitrary clients; forwarded client IPs are honored only when the immediate peer matches configured `trusted_proxy_ips`.
- Production validation now requires `api_resource_guard.backend=postgres` and a configured DSN for production profiles.
- Production model-budget validation now requires PostgreSQL-backed shared storage so the model-cost ledger cannot silently fall back to single-node accounting.
- `ModelRouter` fails closed when persistent ledger enforcement is explicitly requested without a configured model cost store.
- `/agent/run` now uses a Pydantic request schema with bounded message size and forbidden unknown fields, keeping actor identity sourced from the authenticated admin decision.
- Gmail OAuth state storage now purges expired and used state rows opportunistically.
- Docker and Helm production examples expose the trusted proxy list and persistent ledger requirement explicitly.

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
