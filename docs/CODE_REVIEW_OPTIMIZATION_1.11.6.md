# Code Review Optimization 1.11.6

Date: 2026-06-18

Scope: real GA evidence closure for iOS real-device evidence, APNS delivery evidence, tri-app live smoke evidence, and workflow governance wiring.

## Industrial Readiness Finding

1.11.5 successfully merged tri-app release governance with iOS real-device evidence import, but several evidence paths were still configuration-heavy instead of proof-heavy. The most important gap was that iOS evidence JSON could reference an artifact path that did not exist and still pass import. APNS evidence also had a template/importer mismatch because the importer required artifacts while the APNS template did not include one.

## Fixes Completed

1. Missing iOS artifacts now fail validation.
2. Artifact paths are constrained to canonical relative paths under `IOS_EVIDENCE_RAW_DIR`.
3. Absolute artifact paths, `.` segments, `..` segments, and raw-dir escape attempts are rejected.
4. Artifact SHA256 validation now requires a real file and exact hash match.
5. APNS live delivery template now includes a required artifact reference.
6. `ios-evidence` preflight now performs semantic validation through `import_ios_real_device_evidence.validate_raw_dir()`.
7. `tri-app-live-smoke` preflight now requires an existing live-smoke report and validates its scenario, org id, roundtrip steps, trace id, and latency.
8. Added `scripts/import_tri_app_live_smoke_evidence.py` to validate and optionally import live roundtrip smoke reports.
9. Added `scripts/check_workflow_governance.py` to validate release workflow / Makefile gate wiring.
10. Updated apply instructions to use `v1.11.6-apply.patch`.
11. Updated evidence template versions to `1.11.6+real-ga-evidence-closure`.
12. Archived the historical `v1.11.1` patch to prevent accidental application.

## Validation

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q tests
PYTHONDONTWRITEBYTECODE=1 python -m py_compile scripts/check_release_configuration.py scripts/import_ios_real_device_evidence.py scripts/import_tri_app_live_smoke_evidence.py scripts/check_workflow_governance.py
```

Expected result: all tests pass and scripts compile.

## Remaining Non-Code GA Evidence

Passing this package does not prove external credentials or devices are real. Final GA still requires a live GitHub Actions run, real Android/iOS signing credentials, real signed IPA, real physical iPhone install, APNS delivery receipt file, and tri-app approval-to-audit-to-Web-Admin visibility smoke output.
