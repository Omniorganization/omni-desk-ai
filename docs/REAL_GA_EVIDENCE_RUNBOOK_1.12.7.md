# Real GA Evidence Runbook for 1.12.7

This runbook closes the P0/P1/P2 industrialization gap without fabricating evidence. Source gates prove enforcement exists; Real GA requires evidence from live signer, registry, store, staging, push, database, and operations systems.

## Scope

Version: `1.12.7+root-monorepo-production-ga-candidate`

Status after this runbook is added: source-ready, external evidence still required.

## P0: Customer-distribution Real GA blockers

Preferred control-plane run:

1. Produce real-device evidence from BrowserStack and/or AWS Device Farm controller workflows. The uploaded artifact must be named `external-ga-evidence-raw` by default and must contain `release/external-evidence` shaped files, including any referenced artifacts.
2. Produce staging operations evidence from the approved Kubernetes or systemd staging environment. This artifact must include the Postgres soak, rollback, backup/restore, and self-healing failure-injection JSON files under `release/external-evidence/drills/`.
3. Run `.github/workflows/real-ga-evidence-control-plane.yml` with the provider run ids and `staging_operations_evidence_run_id`. All producer runs should upload the shared artifact name from `provider_evidence_artifact_name`, which defaults to `external-ga-evidence-raw`. This workflow assembles provider artifacts, validates the complete bundle, uploads `external-ga-evidence`, and invokes `real-ga-readiness.yml`.

Run `.github/workflows/real-ga-readiness.yml` manually.

- Use `readiness_channel=candidate` to collect an audit report without blocking on missing evidence.
- Use `readiness_channel=real-ga` only when all pre-release evidence exists under `release/external-evidence/` and the live control-plane token is available. This mode enforces the complete external-evidence gate, but it only audits the final Customer GA boundary because the current Release run does not exist yet.
- Configure `OMNIDESK_GITHUB_GOVERNANCE_TOKEN` from a GitHub App installation token or fine-grained token that can read repository Administration metadata and organization Members. Do not copy a developer's broad personal CLI token into Actions.
- Every required production team must be closed/visible, have its declared repository permission, contain at least two members, and include at least one reviewer other than the checked commit author.
- When evidence is produced outside the repository runner, first upload a raw `release/external-evidence` shaped bundle as a GitHub Actions artifact, then run `.github/workflows/remote-evidence-pipeline.yml`. That workflow validates the complete bundle with `scripts/import_external_ga_evidence.py`, uploads a clean `external-ga-evidence` artifact, and does not create or soften evidence.
- To consume the clean bundle, pass the Remote Evidence Pipeline run id to `real-ga-readiness.yml` or `release.yml` through `external_evidence_run_id` and keep `external_evidence_artifact_name=external-ga-evidence`.

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
release/external-evidence/control-plane/github-team-governance-live.json
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

Generate the organization/team report with the same checked commit and the branch report:

```bash
python scripts/check_github_team_governance_live.py \
  . \
  --repository "$GITHUB_REPOSITORY" \
  --commit "$GITHUB_SHA" \
  --branch-protection-report release/external-evidence/control-plane/github-branch-protection-live.json \
  --write-report release/external-evidence/control-plane/github-team-governance-live.json
```

The report fails closed when team/member APIs cannot be read, a team is a one-person shell, the commit author is the only reviewer, CODEOWNERS contains a personal owner, or an applied Ruleset has a bypass actor.

## P1b: Current Release artifact binding

Main Verification must enumerate each Android, iOS, macOS, and Windows artifact separately. During `Release Build`, `scripts/check_current_release_artifact_binding.py` rehashes the actual files downloaded from this release run and requires exact digest-set equality with external signer evidence, each attestation subject, and the selected Main Verification run. The workflow runs this before `check_customer_distribution_ga.py` and before `scripts/sign_release.py`; candidate runs record blockers, while `real-ga` fails closed.

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

A release can be called customer-distribution Real GA only when the `Release Build` workflow's final boundary passes without `--audit-only`:

```bash
python scripts/check_customer_distribution_ga.py . \
  --repository "$GITHUB_REPOSITORY" \
  --commit "$GITHUB_SHA" \
  --current-release-binding-report dist/current-release-artifact-binding.json \
  --write-report release/real-ga-evidence-audit-1.12.7.json
```

This is intentionally a Release-workflow boundary: a pre-release readiness workflow cannot claim Customer GA without the current Release run's native files.
