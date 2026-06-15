# Omni 0.7.39 Tri-App Enterprise Staging

This release moves the tri-app line from controlled staging toward enterprise staging. It is still not declared Full Production GA because real signing credentials, live push provider credentials, and full physical-device release tests must be supplied by the deployment operator.

## Landed hardening

- PostgreSQL AppSync now creates normalized tables for organizations, users, devices, conversations, messages, tasks, approvals, notifications, runtime status, device enrollments, idempotency keys, sync events, and task leases.
- AppSync write operations now enforce idempotency in the shared API layer when `app_sync.require_idempotency` is true. Reusing a key with a different payload fails with `409`.
- Desktop runtime includes an enterprise-staging auto worker that claims approved desktop tasks, renews heartbeat, and completes a dry-run adapter. Real shell/browser/file execution remains approval-gated and must be wired through signed capabilities.
- Mobile Flutter includes secure storage, fail-closed biometric/PIN approval confirmation, Firebase Messaging token registration hooks, and expanded Android/iOS release scaffolds.
- Web Admin includes HTTP-only session-cookie and CSRF route scaffolds for the production proxy model.
- Shared API contract is updated to `0.7.39+enterprise-staging-tri-app-hardening`.

## Remaining GA blockers

- Replace the desktop dry-run worker with signed capability adapters and sandboxed execution.
- Run PostgreSQL multi-instance tests against a real Postgres service.
- Provide real Android keystore, Apple provisioning profile, Firebase config, APNS capability, macOS notarization identity, and Windows signing certificate.
- Run physical-device E2E: Mobile creates task -> Web Admin approves -> Desktop executes -> Mobile receives push -> Web Admin sees audit.
