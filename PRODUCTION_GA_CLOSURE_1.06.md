# OmniDesk AI 1.06 Production GA Closure Hardening Source Package

This package tightens the 1.05 GA closure source line for repeatable local validation and cleaner release-package handoff.

## 1.06 hardening

- Project, workflow, Helm, shared API, Web Admin, Desktop, and Mobile source identities are unified on `1.06+production-ga-closure-hardening`.
- GA release gate version checks derive expected chart and evidence values from the current project version.
- Webhook forced-signature tests tolerate FastAPI/Starlette route objects without an `endpoint` attribute.
- Source validation is intended to run from an external virtualenv so the tree can pass release hygiene without local dependency artifacts.
- Distributed zip artifacts include portable SHA256 checksums.

## Remaining external evidence boundary

- macOS notarization and Windows code signing must be produced by native signer CI.
- Android signed AAB/APK and iOS signed IPA/TestFlight evidence must be produced by mobile signer CI.
- APNS/FCM live push, registry attestation, multi-instance soak, rollback, and backup/restore drills must be attached from real staging or production-equivalent systems.
