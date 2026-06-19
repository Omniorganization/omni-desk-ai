# OmniDesk AI 1.02 Production RC2

This release moves 1.01 from a source candidate toward a verified Production RC.

## RC2 hardening

- Fixed staging/production workflow expression corruption and added `check_workflow_expressions.py`.
- Added asymmetric device challenge verification using Ed25519/P-256 with one-time challenge status.
- Added real Firebase Admin SDK push adapter seam and APNS provider seam; dry-run remains source-test default.
- Added PostgreSQL transaction seams for `FOR UPDATE SKIP LOCKED` task claim, lease renewal, and task status update.
- Converted Web Admin business API calls to `/api/omni/*` server-side proxy; browser no longer sends Gateway bearer tokens.
- Added a guarded Desktop shell sandbox executor restricted to `~/OmniDesktopWorkspace` and allowlisted commands.
- Added iOS workspace/generated registrant placeholders and RC2 evidence tests.

## Remaining GA blockers

- Run real Flutter/Tauri toolchains and generate signed AAB/IPA/DMG/MSI artifacts.
- Validate PostgreSQL task claim under multi-gateway/multi-desktop concurrency.
- Configure Firebase/APNS credentials and run push delivery/retry/dead-letter drills.
- Replace placeholder iOS project with a Flutter-generated Xcode project in a macOS CI runner.
- Capture staging smoke, rollback, and SLO evidence.
