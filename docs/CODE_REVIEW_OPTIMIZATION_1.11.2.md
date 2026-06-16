# Code Review Optimization 1.11.2

Date: 2026-06-16

Scope: release configuration preflight, downstream deploy preflight, and related workflow governance tests.

## Industrial Readiness Finding

The previous release preflight correctly blocked missing GitHub secrets and variables before expensive Release jobs started. The remaining weaknesses were mostly false-pass and late-failure cases, where invalid values could pass preflight but fail during signing, deployment, or smoke tests.

## Bugs Found And Fixed

1. Whitespace-only values were treated as configured.

   Impact: a secret or variable containing only spaces could pass the presence check and fail later in a platform-specific signing or deploy step.

   Fix: required values now strip whitespace for presence checks and reject leading or trailing whitespace without printing secret values.

2. Smoke-test URLs were validated only by prefix.

   Impact: values such as `https://` could pass preflight even though they are not usable service endpoints.

   Fix: URL validation now requires `http` or `https` plus a host.

3. `kubectl` image digest pinning was enforced only for production.

   Impact: staging and rollback `kubectl` deployments pass an artifact image digest to `deploy_artifact.sh`; an unpinned image value could pass preflight but fail later during deployment.

   Fix: every downstream `kubectl` preflight now requires `OMNIDESK_IMAGE` to be pinned as `image@sha256:<64 lowercase hex chars>`.

4. `systemd` preflight required a remote script even though the deploy script has a safe default.

   Impact: valid systemd deployments using the default `/usr/local/bin/omnidesk-deploy-artifact` were blocked unnecessarily.

   Fix: `OMNIDESK_REMOTE_DEPLOY_SCRIPT` is now optional in preflight. When provided, it must remain under `/usr/local/bin` and use safe token characters.

5. Apple Team ID shape was not checked.

   Impact: malformed `OMNI_IOS_APPLE_TEAM_ID` values would fail later during iOS signing.

   Fix: release preflight now requires a 10-character uppercase alphanumeric Apple team id.

## Optimized Files

- `scripts/check_release_configuration.py`
- `tests/test_release_configuration_preflight.py`
- `docs/RELEASE_CONFIGURATION_PREFLIGHT.md`
- `docs/CODE_REVIEW_OPTIMIZATION_1.11.2.md`

## Verification Plan

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q tests/test_release_configuration_preflight.py tests/test_release_governance_assets.py
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python scripts/check_script_executability.py .
git diff --check
```

Expected result: all tests and static checks pass. Release preflight should still fail on GitHub until real secrets and environment variables are configured.
