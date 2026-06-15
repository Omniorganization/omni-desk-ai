# OmniDesk AI 1.11 Patch Manifest - OpenClaw + Codex Aligned Enterprise Agent

Version: `1.11+openclaw-codex-aligned-enterprise-agent`

## Scope

1. Promoted the source identity from `1.10+production-ga-real-evidence-audit` to `1.11+openclaw-codex-aligned-enterprise-agent`.
2. Added one-command operator entrypoints for `doctor`, `onboard`, `evidence doctor`, `channel onboard`, `device pair`, and `app connect`.
3. Added Channel Capability Matrix, Channel Identity Firewall, CIK Guard, Codex-style execution profiles, and signed skill registry.
4. Added repository and scoped `AGENTS.md` rules for Web Admin, Desktop Tauri, Mobile Flutter, security, and release evidence.
5. Added Codex-style self-repair PR modules: branch runner, observe-only repair loop, review policy, evidence bundle, and PR generator.
6. Added `agent-repair-pr.yml` for `ai/*` repair PR checks.
7. Added Agent Eval Harness modules and a Desktop Control Hub status model.

## Validation

- `scripts/check_version_consistency.py`
- `scripts/check_ga_release_gate.py`
- `scripts/check_external_ga_evidence.py --audit-only`
- `tests/test_ga_1_11_openclaw_codex_alignment.py`
- Existing backend, Web Admin, and Desktop source tests where local toolchains are available.

## External Evidence Boundary

This package does not fabricate external evidence. Customer-distribution GA remains blocked until native build logs, signed Android/iOS/Desktop artifacts, APNS/FCM delivery receipts, multi-instance Postgres soak, rollback drill, backup/restore drill, and self-healing failure injection evidence are attached under `release/external-evidence`.
