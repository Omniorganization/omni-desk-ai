# Security Attack Surface Closure for 1.12.7

This document records the source/config hardening added after the 1.12.7 security review.

## Scope

This closure reduces exploitable attack-surface regressions in source-controlled production defaults and CI gates. It does not claim customer-distribution Real GA and does not replace real external evidence for signing, stores, push providers, soak tests, rollback drills, backup/restore drills, or failure-injection drills.

## Fixed in this branch

- Added `scripts/check_security_attack_surface.py` as a fail-closed source/config contract for production security posture.
- Added `.github/workflows/security-attack-surface.yml` with focused regression coverage for admin auth, self-upgrade auth, webhook signature enforcement, device signed requests, abuse limits, file path escape, browser security, and API resource guard behavior.
- Added the attack-surface gate to `Security`, `Release Policy`, `Main Verification`, branch protection requirements, source maturity closure, and production evidence metadata.
- Changed production templates so `shell`, `browser`, `ui_bridge`, `gmail`, and `channels` default to disabled. These capabilities must be enabled only after tenant/actor-specific approval, sandbox, audit, budget, and channel allowlist controls are verified.
- Made Docker and Helm production templates explicitly require admin token envs, shared secret envs, sandbox runner token/HMAC envs, remote approval, no-TTY deny, remote Docker sandboxing, and always-ask coverage for high-risk tools.

## Remaining non-source blockers

The following remain real-world evidence blockers, not source-only fixes:

- Live GitHub branch protection must be verified against the GitHub control plane.
- Signed Android/iOS/Desktop artifacts must be produced by signer CI.
- APNS/FCM push must be verified against real provider credentials.
- Multi-instance PostgreSQL soak and abuse-load tests must run with real gateways/workers.
- Rollback, backup/restore, and failure-injection drills must attach non-mock evidence.

## Required commands

```bash
python scripts/check_security_attack_surface.py . --write-report dist/evidence/security-attack-surface.json
python scripts/check_source_maturity_closure.py . --write-report dist/evidence/source-maturity-closure.json
python scripts/check_external_ga_evidence.py . --audit-only --write-report dist/evidence/external-ga-evidence-audit.json
```

Customer-distribution GA still requires `scripts/check_external_ga_evidence.py` to pass without `--audit-only` against real evidence.
