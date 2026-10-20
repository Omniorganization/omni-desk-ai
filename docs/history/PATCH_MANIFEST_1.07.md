# Patch Manifest 1.07

Version: `1.07+production-ga-release-integrity`

## Changed

1. Promoted the release identity from `1.06+production-ga-closure-hardening` to `1.07+production-ga-release-integrity`.
2. Expanded `scripts/check_version_consistency.py` to validate full-stack version alignment across Python, Docker, workflows, Helm, release evidence, shared API, Web Admin, Desktop Tauri, and Mobile Flutter.
3. Hardened `scripts/check_release_hygiene.py` to block frontend caches/builds, native build outputs, runtime databases, keys, logs, token files, and TypeScript build metadata.
4. Reworked `scripts/package_clean_zip.sh` into a non-mutating deterministic packager with Python interpreter fallback.
5. Updated the GA release gate to allow top-level `.git` only for live checkout hygiene checks while package hygiene remains strict elsewhere.
6. Added regression tests for the stronger release-hygiene and clean-zip contracts.

## Verification

- Version consistency gate.
- Release hygiene gate.
- GA release gate.
- Release package hygiene regression tests.
- Release zip hygiene regression tests.
- Tri-app source readiness and quality gates.

Native Flutter/Tauri release builds still require external Flutter, Rust, Cargo, signing, push, and device-login environments.
