# Code Review Optimization 1.11.7

Date: 2026-06-18

Scope: semantic hardening for the 1.11.6 real GA evidence closure package.

## Industrial Readiness Finding

1.11.6 closed important evidence-presence gaps, but several gates still accepted evidence that could be structurally present while semantically weak. The main risk was release confidence drift: native iOS build failures, APNS non-delivery artifacts, mismatched evidence versions, patch-only workflow snippets, and stale cache files could still make a package look more complete than the real release state.

## Fixes Completed

1. iOS native build evidence now requires `exit_code: 0`.
2. iOS evidence version must match the explicit `--expected-version` value.
3. iOS evidence preflight uses explicit `IOS_EVIDENCE_EXPECTED_VERSION`, now aligned to the source-gated package version.
4. APNS delivery evidence now requires a delivery-specific artifact `kind`: `apns_provider_receipt`, `device_notification_log`, or `firebase_delivery_receipt`.
5. APNS delivery evidence rejects `.ipa` artifacts as delivery proof.
6. Signed IPA evidence may declare `source_native_artifact_sha256` when exported/signed artifacts differ from the native build output.
7. Tri-app live smoke evidence now validates non-placeholder trace ids, timestamp ordering, and latency consistency.
8. Privacy linting now rejects broader raw secret/token/certificate/device identifiers while allowing hash/fingerprint fields.
9. Workflow governance can run in `--require-real-workflows` mode and no longer accepts patch snippets as a substitute for `.github/workflows/release.yml`.
10. Release workflow uploads 1.11.7 external-evidence audit reports and preflight artifacts.
11. Historical patches are archived with explicit warnings.
12. Release package hygiene excludes `.pytest_cache`, `__pycache__`, `*.pyc`, and `*.pyo`.

## Validation

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m py_compile scripts/check_release_configuration.py scripts/import_ios_real_device_evidence.py scripts/import_tri_app_live_smoke_evidence.py scripts/check_workflow_governance.py
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q tests/test_release_configuration_preflight.py tests/test_ios_real_device_evidence_import.py tests/test_tri_app_live_smoke_evidence_import.py tests/test_workflow_governance.py
python3 scripts/check_workflow_governance.py . --require-real-workflows
git diff --check
```

## Remaining External Evidence

This version improves repository gates and package hygiene. It is a source-gated GA candidate and still does not fabricate real GA evidence. Final GA remains blocked until the configured GitHub repository and environments provide real signing credentials, real iOS evidence from a physical iPhone, APNS delivery receipts or device logs, and a real tri-app approval/audit/Web Admin smoke report.
