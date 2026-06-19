# Desktop Signing Release Gate

Required before production:

- macOS: Developer ID certificate, hardened runtime, notarization, stapling.
- Windows: EV/OV code-signing certificate and signed installer.
- Linux: signed AppImage/deb/rpm checksums.
- Tauri updater: signed update manifest and rollback test.
- Runtime token: OS Keychain / Windows Credential Manager / Secret Service only.
- Run `npm ci && npm run typecheck && npm test && cargo check --manifest-path src-tauri/Cargo.toml`.
