# OmniDesk 1.11.7 Source-Gated GA Candidate

Date: 2026-06-18

This repository version is intentionally named `1.11.7+source-gated-ga-candidate`. It is a source-gated release candidate, not a customer-distribution Production GA release.

## What Is Closed

- Source version consistency is enforced across Python package metadata, Docker defaults, GitHub workflows, Helm appVersion, Web Admin, Desktop, Mobile, shared contract, and release manifests.
- Release hygiene rejects cache, VCS, generated, and runtime-secret artifacts.
- Package checksums are written as portable relative paths and can be verified after moving or extracting the package.
- Real GA evidence checks remain fail-closed through `scripts/check_external_ga_evidence.py`.
- Release workflow imports iOS evidence and tri-app live smoke evidence only when real configured paths and credentials exist.

## What Is Still Blocked

The project must not be described as real Production GA until these external evidence groups are produced by real CI, signer, push, staging, and operations systems:

- Android, iOS, Tauri, and Rust native build evidence.
- Android signed AAB, iOS signed IPA, macOS notarized artifact, and Windows signed artifact evidence.
- APNS and FCM live push delivery receipts or device logs.
- Postgres multi-instance soak evidence.
- Rollback drill evidence.
- Backup/restore drill evidence.
- Self-healing failure injection evidence.
- Final `dist/` release payload with wheel, sdist, SBOM, SLSA provenance, release metadata, checksums, signatures, Cosign sidecars, locked Helm values, and external evidence audit report.

## Required Package Gate

For generated source packages:

```bash
python scripts/check_release_hygiene.py "$PACKAGE_DIR"
python scripts/write_portable_sha256s.py --base-dir "$PACKAGE_DIR" --output SHA256SUMS.txt --verify
```

For final distribution GA, `scripts/check_external_ga_evidence.py` must pass without `--audit-only` against real evidence. Source package gates alone are not enough.
