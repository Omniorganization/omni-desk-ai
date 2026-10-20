# OmniDesk AI 1.04 Production GA

This release hardens the 1.02 RC2 source candidate for repeatable industrial build gates.

## RC3 hardening

- Web Admin production build now resolves `@/lib/*` imports consistently.
- Web Admin browser code uses only same-origin `/api/omni/*` proxy calls; Gateway bearer tokens remain server-side.
- Gateway URL selection is allowlisted to prevent arbitrary server-side fetch targets.
- The tri-app quality gate now includes source tests, typechecks, and Web/Desktop production frontend builds.
- Android mobile scaffold has a single namespace-correct `MainActivity`.

## Remaining GA blockers

- Run real Flutter toolchain and generate signed AAB/IPA artifacts.
- Run Rust/Tauri toolchain and generate signed/notarized DMG plus Windows installer artifacts.
- Validate Firebase/APNS delivery against real provider credentials.
- Exercise PostgreSQL claim/lease concurrency under multi-gateway load.
- Capture staging rollback and SLO evidence.
