# Real GA Evidence Runbook for 1.12.7

This runbook closes the P0/P1/P2 industrialization gap without fabricating evidence. Source gates prove enforcement exists; Real GA requires evidence from live signer, registry, store, staging, push, database, and operations systems.

## Scope

Version: `1.12.7+root-monorepo-production-ga-candidate`

Status after this runbook is added: source-ready, external evidence still required.

## P0: Customer-distribution Real GA blockers

Run `.github/workflows/real-ga-readiness.yml` manually.

- Use `readiness_channel=candidate` to collect an audit report without blocking on missing evidence.
- Use `readiness_channel=real-ga` only when all evidence exists under `release/external-evidence/` and the live control-plane token is available to the workflow environment.

Required external evidence files:

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
release/external-evidence/push/apns-live-delivery.json
release/external-evidence/push/fcm-live-delivery.json
release/external-evidence/drills/postgres-multi-instance-soak.json
release/external-evidence/drills/rollback-drill.json
release/external-evidence/drills/backup-restore-drill.json
release/external-evidence/drills/self-healing-failure-injection.json
```

## P1: Live control-plane verification

Generate the branch-protection report with:

```bash
python scripts/check_live_branch_protection_contract.py \
  . \
  --repository "$GITHUB_REPOSITORY" \
  --write-report release/external-evidence/control-plane/github-branch-protection-live.json
```

The report must show `status: passed` and no failures. It verifies live GitHub branch protection against `.github/branch-protection.required.json`.

## P2: Model and tri-app smoke evidence

Create `release/external-evidence/model/model-live-smoke.json` from a real staging or production model request. Minimum required fields:

```json
{
  "schema": "omnidesk-model-live-smoke/v1",
  "status": "passed",
  "produced_at": "ISO-8601 timestamp from the live run",
  "producer": "ci-or-operator-identity",
  "environment": "staging",
  "backend_base_url": "live backend URL used by the smoke",
  "scenario_id": "stable smoke scenario id",
  "model_request_id": "provider or gateway request id",
  "trace_id": "distributed trace id",
  "audit_event_id": "audit log event id",
  "cost_ledger_entry_id": "model cost ledger entry id",
  "response_non_empty": true,
  "audit_logged": true,
  "cost_ledger_recorded": true,
  "budget_enforced": true,
  "approval_required_on_budget_exceeded": true,
  "p95_latency_ms": 2500,
  "error_rate": 0
}
```

Do not commit placeholder, mock, fake, sample, or example values into `release/external-evidence/`. The external evidence gate rejects those strings.

## Release decision rule

A release can be called customer-distribution Real GA only when:

```bash
python scripts/check_external_ga_evidence.py . --write-report release/real-ga-evidence-audit-1.12.7.json
```

returns `status: passed` without `--audit-only`.
