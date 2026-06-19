# OmniDesk AI 1.04 Production GA Source Package

This package promotes the previous RC3 engineering baseline to a GA-ready source/release tree. The repository now fails closed on the main blockers that previously prevented GA classification:

1. Version and release identity are unified across source, workflows, Helm, Docker metadata, and the tri-app contract.
2. Helm no longer ships with a stale application version or static placeholder application image digest.
3. Web Admin removes unsafe CSP directives and uses HTTP-only `__Host-` session cookies with bounded lifetime.
4. WebSocket query-token authentication is development-only and blocked by production validation.
5. Desktop and Mobile devices generate per-install identities and submit public keys during registration.
6. Production validation requires Postgres-backed AppSync when multi-instance safety is required.
7. `scripts/check_ga_release_gate.py` acts as the consolidated GA gate for source packages and release metadata.

## Mandatory external production evidence

The following cannot be truthfully generated inside an offline source zip and must be produced by the deployment organization:

- Signed Android AAB/APK and iOS IPA/TestFlight evidence.
- macOS notarization ticket and Windows code-signing evidence.
- APNS/FCM provider delivery receipts.
- Registry-published final OCI image digest.
- Real multi-pod Postgres pressure test output.
- Real rollback, backup-restore, SLO burn-rate, and incident drill reports.

The GA package enforces these through release metadata, workflows, readiness scripts, and documented gates rather than pretending they were executed locally.
