# Security Exception: GHSA-wrw7-89jp-8q8g

Exception ID: GHSA-wrw7-89jp-8q8g
Status: active
Owner: security-release-owner
Scope: Linux desktop Tauri 2.11 transitive GTK stack only
Expires At: 2026-10-31
Upstream Tracking: Tauri/wry Linux GTK dependency migration to patched glib line
Compensating Controls: dependency-review allowlist is narrow, security workflow validates this exception, desktop release remains signed and evidence-gated

## Impact

This exception documents a known dependency-review advisory currently pulled through the Linux desktop Tauri dependency stack. The exception is intentionally narrow and must not be treated as a general waiver for Python runtime, web admin, mobile, gateway, sandbox, approval, or self-upgrade components.

## Runtime reachability

The known exposure is limited to target-specific Linux desktop GUI dependencies. It is not part of the backend gateway execution path, admin API authentication path, webhook signature validation path, model routing path, memory encryption path, or sandbox runner path.

## Compensating control

The repository keeps this advisory allowlisted only through `.github/workflows/security.yml`, and the workflow now requires `scripts/check_security_exceptions.py` to validate that every allowed GHSA has an active, owned, scoped, unexpired exception file. Release builds still require pinned dependencies, CodeQL, Bandit, pip-audit, dependency review, signed artifact policy, and Real GA evidence gates.

## Removal criteria

Remove this exception when upstream Tauri/wry migrates the affected Linux GTK stack to a patched dependency line, or earlier if dependency-review no longer reports this advisory. The exception must also be removed or renewed before the expiry date. Expired exceptions fail the security workflow.

## Real GA boundary

This exception does not authorize customer-distribution Real GA by itself. Real GA still requires complete external evidence, signed artifact binding, organization/team governance evidence, and release gate success.
