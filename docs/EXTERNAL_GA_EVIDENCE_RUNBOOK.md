# External GA Evidence Runbook

Customer-distribution GA requires evidence from real external systems. Source checks, contract tests, generated templates, and dry runs are useful preparation, but they are not distribution evidence.

## Plan Missing Evidence

Generate the current missing-evidence plan and blocked JSON templates:

```bash
python scripts/external_ga_evidence_doctor.py .
```

Outputs:

- `dist/external-ga-evidence-plan.json`
- `dist/external-ga-evidence-templates/**`

The generated templates intentionally use `status: blocked_pending_real_run`. They must not be copied into `release/external-evidence` as passing evidence. Fill evidence only from real CI, signer, registry, push-provider, staging, or operations-drill output.

## Gate Real Evidence

Audit without blocking source packaging:

```bash
make external-ga-evidence-audit
```

Block customer distribution unless every evidence category passes:

```bash
make external-ga-evidence-gate
```

## Required Runtime Evidence Categories

- Native builds: Flutter Android release, Flutter iOS release, Tauri Desktop release, Rust locked cargo check.
- Signed artifacts: Android signed AAB, iOS signed IPA, macOS notarized desktop artifact, Windows signed desktop artifact.
- Web Admin OCI: digest-pinned image, Cosign signature, SBOM attestation, SLSA attestation, non-root runtime, healthcheck, read-only runtime.
- Push delivery: real APNS and FCM delivery receipts.
- Operations drills: Postgres multi-instance soak, rollback drill, backup/restore drill, self-healing failure injection.

## Production Profile Guard

Before running evidence collection against staging or production, validate configuration profile safety defaults:

```bash
python scripts/check_config_profiles.py .
```

Production and enterprise profiles must keep remote approval, Postgres multi-instance storage, remote Docker sandboxing, trusted-only plugins, PII redaction, encrypted memory, and high-risk local UI/browser capabilities disabled by default.
