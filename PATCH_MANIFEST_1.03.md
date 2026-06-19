# Patch Manifest 1.04

Version: `1.04+production-ga-industrial-hardening`

## Changed

1. Fixed Web Admin Next path aliasing so `/api/omni/*` route handlers compile in production builds.
2. Updated Web Admin client tests to match the server-side proxy architecture instead of the older direct-Gateway browser calls.
3. Added Gateway URL allowlist resolution to prevent browser-supplied SSRF targets in the Web Admin proxy.
4. Hardened Web Admin proxy header handling so session bearer headers cannot be overridden by forwarded request headers.
5. Promoted `tri-app-quality` to include Web Admin and Desktop production frontend builds.
6. Removed the stale Android `ai.omnidesk.mobile` MainActivity duplicate; the app now has one namespace-aligned `com.omnidesk.mobile` entrypoint.
7. Added release-readiness checks for Web path aliases, Gateway helper presence, and Android namespace hygiene.

## Verification

- Backend tri-app contract tests.
- Web Admin API/proxy unit tests, typecheck, and production build.
- Desktop Tauri API unit tests, typecheck, and Vite production build.
- Source-mode release preflight.

Flutter and native Tauri release builds still require external Flutter/Rust/signing toolchains.
