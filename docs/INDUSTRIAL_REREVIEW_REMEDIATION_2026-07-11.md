# Industrial re-review remediation — 2026-07-11

This change set addresses the source-remediable findings from the industrial re-review of `main` at `2e712c0a`.

## Closed findings

- Desktop enrollment now uses a newly constructed signed client for the first heartbeat, rather than waiting for React state propagation.
- Desktop native workspace access no longer accepts a generic command plus arguments. It exposes bounded read/list operations, rejects absolute and parent paths, canonicalizes the target, rechecks workspace containment, and limits output.
- Chat model requests include a bounded, redacted transcript of authorized prior messages.
- Chat model metadata now receives the server-authenticated role and the conversation's server-owned organization identifier.
- Provider failures return a stable `model_provider_unavailable` code and trace ID; upstream exception details remain server-side.

## Risk and rollback

- Existing desktop tasks that send `scope.command` and `scope.args` will fail closed. Producers must migrate to `scope.operation=read_file|list_directory` plus `scope.relative_path`.
- Context history increases model input size, bounded to 20 messages and 24,000 characters. Roll back the context-builder call if provider latency or cost changes exceed policy, while retaining role/organization metadata and error redaction.
- Rollback is a normal PR revert. Do not restore generic native command execution or provider-detail responses without a replacement security control.

## Validation evidence

- Full Python suite: `699 passed, 1 skipped` with warnings treated as errors.
- Targeted remediation suite: `26 passed`.
- Ruff: repository-wide pass.
- Desktop typed API contract tests: `6 passed`.
- Desktop TypeScript/Vite production build: passed.
- Rust compilation is delegated to GitHub Actions because this workstation has no Rust toolchain. Source contract tests verify removal of generic command execution.

## Explicitly unresolved external evidence

This source remediation does not create or claim macOS notarization, Windows Authenticode, iOS Distribution/TestFlight, Android production signing, APNS/FCM delivery, multi-instance PostgreSQL soak, rollback/restore drills, failure injection, self-healing, or real-provider model evidence. The Real GA gate must remain blocked until those artifacts are produced by their real systems.
