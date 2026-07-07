# Real External Evidence Completion Guide for 1.12.7

Version: `1.12.7+root-monorepo-production-ga-candidate`

This guide explains how to move from a source-gated GA candidate to customer-distribution Real GA. Do not place placeholders, examples, mock outputs, or hand-written pass files under `release/external-evidence/`.

## Validation rule

Candidate audit:

```bash
python scripts/check_real_ga_complete.py . --audit-only --write-report release/real-ga-evidence-audit-1.12.7.json
```

Real GA gate:

```bash
python scripts/check_real_ga_complete.py . --write-report release/real-ga-evidence-audit-1.12.7.json
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
| Native signed artifact binding | Main Verification artifact that binds all native build and signed artifact evidence paths and hashes to the merge commit |
| Branch protection | Live GitHub branch protection API result for `main` |
| Team governance | Live GitHub organization/team API result proving organization ownership, resolved teams, team CODEOWNERS, enforced code-owner review, and no personal fallback |
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
release/external-evidence/control-plane/native-signed-artifact-binding.json
release/external-evidence/control-plane/github-branch-protection-live.json
release/external-evidence/control-plane/github-team-governance-live.json
release/external-evidence/model/model-live-smoke.json
release/external-evidence/integrations/bigseller-live-smoke.json
release/external-evidence/push/apns-live-delivery.json
release/external-evidence/push/fcm-live-delivery.json
release/external-evidence/drills/postgres-multi-instance-soak.json
release/external-evidence/drills/rollback-drill.json
release/external-evidence/drills/backup-restore-drill.json
release/external-evidence/drills/self-healing-failure-injection.json
```

## GitHub organization/team governance

The current repository is owned by a GitHub `User`, so true team CODEOWNERS cannot be enforced yet. Source-candidate governance keeps the personal fallback owner explicit in `.github/CODEOWNERS`, but customer-distribution Real GA requires:

1. Transfer the repository to the approved GitHub organization.
2. Create the teams declared in `.github/team-governance.required.json`.
3. Replace the personal fallback CODEOWNERS entries with real `@org/team` owners.
4. Enable branch protection with CODEOWNERS review and admin enforcement.
5. Produce `release/external-evidence/control-plane/github-team-governance-live.json` from a live GitHub API probe.

## Execution order

1. Merge all source-side closure PRs.
2. Transfer the repository to the approved GitHub organization and configure the required teams before Real GA.
3. Run native builds on real builders.
4. Produce signed mobile and desktop artifacts.
5. Run Main Verification with the complete external evidence artifact so it emits `native-signed-artifact-binding.json`.
6. Run real-device smoke on BrowserStack and/or AWS Device Farm and upload a raw `release/external-evidence` shaped artifact.
7. Run model, push, and integration live smoke checks and upload their raw evidence artifact.
8. Run Postgres soak, rollback, backup/restore, and self-healing drills in the approved Kubernetes or systemd staging environment.
9. Run `Real GA Evidence Control Plane` with the BrowserStack/AWS Device Farm run id, the staging operations run id, and any release/live-services evidence run ids. Producer runs should use the shared `provider_evidence_artifact_name` artifact name, defaulting to `external-ga-evidence-raw`. The control plane assembles the raw evidence, validates it, uploads `external-ga-evidence`, and calls `Real GA Readiness`.
10. If a single complete raw bundle was produced outside the control-plane flow, run `Remote Evidence Pipeline` with that raw artifact run id, then run `Real GA Readiness` with `external_evidence_run_id` set to the Remote Evidence Pipeline run id.
11. Run the Real GA gate without `--audit-only`.
12. Regenerate the customer distribution report.

## Remote evidence handoff

The raw artifact must contain the same relative paths listed above, plus any artifact files referenced by `artifacts[].path`. The importer validates the complete bundle before copying it into `release/external-evidence/`; partial bundles, wrong versions, unresolved GitHub teams, personal CODEOWNERS fallbacks, unbound native/signed artifacts, placeholders, mock values, and missing artifact hashes stay blocked.
