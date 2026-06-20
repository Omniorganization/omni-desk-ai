# OmniDesk 1.12.2 Root Monorepo Production GA Candidate

This version is intentionally named `1.12.2+root-monorepo-production-ga-candidate`. It upgrades the 1.11.8 enterprise chat candidate toward the requested production-GA-candidate shape without claiming customer-distribution GA.

## What changed

- `scripts/check_release_channel_policy.py` now verifies that candidate releases stay audit-only, Real GA releases never use `--audit-only`, and generated package names keep an honest candidate/source-gated label.
- Explicit `real-ga` checks now require non-candidate artifact naming plus external evidence audit `blocker_count == 0` and `status == passed`.
- CI now writes a per-Python-matrix `ci-evidence.json` artifact with source commit, Actions run URL, coverage hashes, and captured ruff/pyright/pytest logs.
- Security workflow coverage now includes CodeQL, gitleaks secret scanning, dependency review where GitHub Dependency Graph is enabled, license policy checks, bandit, pip-audit, and the existing Trivy Docker scan.
- CI and the dedicated Release Policy workflow now run release-channel, workflow-governance, and external-GA evidence-contract checks before merge.
- `.github/branch-protection.required.json`, `docs/BRANCH_PROTECTION.md`, and CODEOWNERS now cover `.github/`, `scripts/`, `deploy/`, `omnidesk_agent/security/`, and `release/`.
- Source-root hygiene now rejects generated OmniDesk package directories and wrapper zips so old distribution outputs cannot drift back into the GitHub source trunk.
- The generated distribution folder now carries a machine-readable `release-manifest.json`.
- The manifest binds the package slug, semantic version, source commit, artifact sizes, artifact SHA-256 values, and the external GA evidence audit status.
- `package-final-gate` now verifies the distribution manifest in addition to release hygiene and portable `SHA256SUMS.txt`.
- The repository root now exposes source, apps, packages, infra, tests, docs, workflows, and release evidence directly instead of requiring GitHub readers to open a versioned package directory first.
- `/api/chat` is available as the unified audited non-streaming model Q&A path; `/api/chat/stream` is reserved and fails closed until streaming audit/chunking is production-gated.
- The release remains blocked for customer GA while external evidence categories are missing.

## Current rating boundary

The earlier package-only `65/100` rating is accurate for the public GitHub root before this monorepo restructuring. It is not a full source audit score: the full source package already contained workflows, apps, tests, deployment assets, checksums, and evidence gates.

The defensible classification is:

- GitHub-visible source shape after the root monorepo restructure: source-gated production-GA candidate.
- Preserved historical package directory: Enterprise Candidate / Pre-GA evidence snapshot.
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

This must pass without `--audit-only` before any GA claim.
