# Code Review Optimization 1.11.4

Date: 2026-06-17

Scope: tri-app release governance preflight for Web Admin, Desktop, Mobile, and cross-end approval/audit/notification configuration.

## Industrial Readiness Finding

Version 1.11.3 improved release preflight governance by adding structured JSON output, report writing, stricter image references, and canonical systemd script validation. The remaining release-governance gap was that the checks still centered on the backend/release pipeline and mobile signing configuration. Web Admin, Desktop Agent, Mobile approval runtime, and cross-end tri-app business-line configuration were not represented as first-class preflight scopes.

## Bugs / Gaps Found And Fixed

1. Web Admin release target was only indirectly covered.

   Impact: a Web Admin deployment could miss its admin token, auth secret, base URL, API base URL, or digest-pinned image and still pass generic release checks.

   Fix: added `--scope web-admin` with required `WEB_ADMIN_*` secrets/vars, https-or-local URL validation, and digest-pinned `WEB_ADMIN_IMAGE` validation.

2. Desktop Agent / Control Hub runtime boundary was not checked.

   Impact: desktop bridge token, HMAC secret, local agent base URL, update endpoint, bridge origin, or app identifier could be missing or malformed before packaging.

   Fix: added `--scope desktop` with required `DESKTOP_*` values, reverse-DNS app identifier validation, https-or-local URL validation, and origin-only bridge validation.

3. Mobile approval runtime configuration was separate from mobile signing configuration.

   Impact: Android/iOS signing checks could pass while the mobile app had no approval callback, push HMAC secret, or valid package/bundle identifiers for runtime integration.

   Fix: added `--scope mobile` with approval token, push HMAC secret, API base URL, approval callback URL, Android package name, and iOS bundle id validation.

4. Cross-end business-line governance was not represented.

   Impact: Desktop -> Mobile approval -> Backend audit -> Web Admin visibility could not be preflighted as a shared tri-app contract.

   Fix: added `--scope tri-app` with backend, Web Admin, Mobile callback, Desktop agent URLs plus admin/mobile/desktop/audit tokens and organization id validation.

5. Release evidence remained backend-centric.

   Impact: preflight JSON reports existed but could not be emitted for each product surface.

   Fix: all new scopes support the same `--format json` and `--report-path` mechanism introduced in 1.11.3.

## Optimized Files

- `scripts/check_release_configuration.py`
- `tests/test_release_configuration_preflight.py`
- `docs/RELEASE_CONFIGURATION_PREFLIGHT.md`
- `docs/CODE_REVIEW_OPTIMIZATION_1.11.4.md`
- `VERSION_MANIFEST.md`

## New Preflight Scopes

```bash
python scripts/check_release_configuration.py --scope web-admin
python scripts/check_release_configuration.py --scope desktop
python scripts/check_release_configuration.py --scope mobile
python scripts/check_release_configuration.py --scope tri-app --format json --report-path dist/tri-app-preflight.json
```

## Verification Plan

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q tests/test_release_configuration_preflight.py
PYTHONDONTWRITEBYTECODE=1 python -m py_compile scripts/check_release_configuration.py
```

Expected result: all tests pass. Live release status remains blocked until real GitHub secrets, protected environment variables, signing material, and staging/production smoke endpoints are configured.
