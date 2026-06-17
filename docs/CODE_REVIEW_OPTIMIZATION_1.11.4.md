# Code Review Optimization 1.11.4

Date: 2026-06-17

Scope: Web Admin preflight, Desktop preflight, and tri-app approval/audit/notification/session chain smoke coverage.

## Implemented Changes

1. Added Web Admin specialized preflight.

   The new `web-admin` scope validates admin token presence, base/API URLs, and the four chain paths used by session, approval, audit, and notification checks.

2. Added Desktop specialized preflight.

   The new `desktop` scope validates Desktop client token, API URL, four chain paths, and update channel. Accepted channels are `stable`, `beta`, `internal`, and `nightly`.

3. Added tri-app chain smoke preflight.

   The new `tri-app-smoke` scope validates Web Admin, Desktop, and Mobile client identities plus the shared chain paths required for live smoke execution.

4. Added live tri-app chain smoke script.

   `scripts/tri_app_chain_smoke_test.py` performs 12 checks: 3 clients multiplied by 4 chain steps. It sends a correlation id through each request and can require endpoints to echo that id.

5. Added focused unit tests.

   New tests cover missing specialized configuration, malformed paths, Desktop channel validation, plan generation, no-secret result payloads, `ok=false` rejection, and correlation echo enforcement.

## Additional Optimization Directions

1. Wire these scopes into GitHub Actions.

   Add `web-admin` preflight before Web Admin release/smoke jobs, `desktop` preflight before Desktop release/smoke jobs, and `tri-app-smoke` preflight before live chain smoke execution.

2. Store smoke evidence as artifacts.

   Run both preflight and live smoke with JSON output and upload the reports as release evidence. This gives audit reviewers durable proof of which endpoint, client, and correlation id were tested.

3. Add environment protection checks.

   Put `web-admin`, `desktop`, and `tri-app-smoke` under staging/production GitHub environments with required reviewers. That keeps live smoke tokens and protected URLs out of generic repository scope.

4. Add negative-path live smoke.

   After the positive chain is stable, add denied-approval, expired-session, and missing-notification checks. These should be separate jobs so they do not block the first operational rollout.

5. Make endpoint contracts explicit.

   Standardize the response body for chain endpoints as `{"ok": true, "correlation_id": "...", "step": "..."}` so `--require-correlation-echo` can become mandatory.

## Verification

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q tests/test_release_configuration_preflight.py tests/test_tri_app_chain_smoke_test.py
python3 scripts/check_release_configuration.py --scope web-admin
python3 scripts/check_release_configuration.py --scope desktop
python3 scripts/check_release_configuration.py --scope tri-app-smoke
```

The three preflight commands should fail until real environment values are exported. They should print only missing or invalid variable names, not secret values.
