# Patch Manifest 1.04

Version: `1.04+production-ga-industrial-hardening`

## GA hardening applied

- Unified version identity across Python package, Docker build args, GitHub release/promote workflows, Helm Chart, shared API contract, Web Admin, Desktop Tauri, and Mobile Flutter.
- Fixed Helm stale versioning and changed static image digests into required release-pipeline values.
- Added `scripts/check_ga_release_gate.py` and wired it into release and tri-app quality workflows.
- Hardened Web Admin CSP and HTTP-only `__Host-` cookies.
- Blocked WebSocket query-token auth in production.
- Required public keys and non-predictable device IDs for desktop/mobile registration in production.
- Added Desktop per-install P-256 device identity stored in OS secure storage.
- Added Mobile per-install Ed25519 device identity stored in `flutter_secure_storage`.
- Added production validation for AppSync Postgres/multi-instance settings.
- Cleaned the dev package lineage by rebuilding it from 1.04 GA components only.

## Evidence boundary

This source package contains GA gates and production-readiness controls. It does not contain externally unverifiable artifacts such as Apple notarization tickets, Android Play signing certificates, APNS/FCM provider receipts, or real production load-test reports. Those are enforced as release gates and must be produced by the deployment CI/CD environment.
