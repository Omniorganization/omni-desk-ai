# Real External Evidence Completion Guide for 1.12.7

Version: `1.12.7+root-monorepo-production-ga-candidate`

This guide explains how to move from a source-gated GA candidate to customer-distribution Real GA. Do not place placeholders, examples, mock outputs, or hand-written pass files under `release/external-evidence/`.

## Validation rule

Candidate audit:

```bash
python scripts/check_external_ga_evidence.py . --audit-only --write-report release/real-ga-evidence-audit-1.12.7.json
```

Real GA gate:

```bash
python scripts/check_external_ga_evidence.py . --write-report release/real-ga-evidence-audit-1.12.7.json
```

Customer report:

```bash
python scripts/write_real_ga_customer_distribution_report.py . --audit-report release/real-ga-evidence-audit-1.12.7.json --output release/real-ga-customer-distribution-report.md
```

A customer-distribution Real GA claim is allowed only when the non-audit Real GA gate returns `status: passed`.

## Required evidence groups

| Group | Required proof source |
|---|---|
| Native builds | Real Android, iOS, Tauri, and Rust builders with artifact hashes |
| Signed artifacts | Real Android signing, iOS signing, macOS notarization, and Windows signing output |
| Branch protection | Live GitHub branch protection API result for `main` |
| Model live smoke | Real model request, non-empty response, trace id, audit id, cost ledger id, and budget enforcement |
| BigSeller live smoke | Approved staging or production integration if BigSeller is in release scope |
| Push delivery | Real APNS and FCM delivery receipts from registered devices |
| Operations drills | Postgres multi-instance soak, rollback, backup/restore, and self-healing failure injection |

## Minimum external files

```text
release/external-evidence/native-build/flutter-android-release.json
release/external-evidence/native-build/flutter-ios-release.json
release/external-evidence/native-build/tauri-desktop-release.json
release/external-evidence/native-build/rust-cargo-check-locked.json
release/external-evidence/signed-artifacts/android-signed-aab.json
release/external-evidence/signed-artifacts/ios-signed-ipa.json
release/external-evidence/signed-artifacts/desktop-macos-notarized.json
release/external-evidence/signed-artifacts/desktop-windows-signed.json
release/external-evidence/control-plane/github-branch-protection-live.json
release/external-evidence/model/model-live-smoke.json
release/external-evidence/integrations/bigseller-live-smoke.json
release/external-evidence/push/apns-live-delivery.json
release/external-evidence/push/fcm-live-delivery.json
release/external-evidence/drills/postgres-multi-instance-soak.json
release/external-evidence/drills/rollback-drill.json
release/external-evidence/drills/backup-restore-drill.json
release/external-evidence/drills/self-healing-failure-injection.json
```

## Execution order

1. Merge all source-side closure PRs.
2. Run native builds on real builders.
3. Produce signed mobile and desktop artifacts.
4. Run Web, Desktop, and Mobile smoke checks against staging.
5. Run model, push, and integration live smoke checks.
6. Run Postgres soak and operations drills.
7. Run the Real GA gate without `--audit-only`.
8. Regenerate the customer distribution report.
