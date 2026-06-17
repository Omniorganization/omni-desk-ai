# Omni-desk-AI 1.11.4 Web/Desktop Chain Hardening Package

Build date: 2026-06-17
Base package: Omni-desk-AI-1.11.3-release-preflight-governance.zip

## Purpose

This package adds Web Admin and Desktop specialized preflight gates plus tri-app approval, audit, notification, and session chain smoke coverage.

## Included Files

- `scripts/check_release_configuration.py`
- `scripts/tri_app_chain_smoke_test.py`
- `tests/test_release_configuration_preflight.py`
- `tests/test_tri_app_chain_smoke_test.py`
- `docs/RELEASE_CONFIGURATION_PREFLIGHT.md`
- `docs/CODE_REVIEW_OPTIMIZATION_1.11.4.md`
- `VERSION_MANIFEST.md`

## Verification

Validated with:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q tests/test_release_configuration_preflight.py tests/test_tri_app_chain_smoke_test.py
```

Expected result:

```text
26 passed
```

Live preflight scopes `web-admin`, `desktop`, and `tri-app-smoke` intentionally fail until real environment secrets and variables are provided.
