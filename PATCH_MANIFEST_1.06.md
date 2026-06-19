# Patch Manifest 1.06

Version: `1.06+production-ga-closure-hardening`

## Changed

1. Promoted the source identity from `1.05+production-ga-closure` to `1.06+production-ga-closure-hardening` across Python, Docker, workflows, Helm, shared API, Web Admin, Desktop Tauri, and Mobile Flutter.
2. Made the GA release gate derive the expected Helm chart version and release evidence version from `pyproject.toml`, reducing future version-drift risk.
3. Updated Helm release tests to derive the expected chart/app version from the current project version instead of hard-coded 1.05 values.
4. Fixed webhook forced-signature test discovery for FastAPI/Starlette route entries that do not expose an `endpoint` attribute.
5. Revalidated source quality with the Python test environment outside the source tree so release hygiene remains meaningful.

## Verification

- `make test-fast`
- `make tri-app-quality`
- `python scripts/check_version_consistency.py .`
- `python scripts/check_release_hygiene.py . --allow-vcs`
- `python scripts/check_tri_app_release_readiness.py . --mode source`
- `python scripts/check_ga_release_gate.py .`

Flutter and native Tauri release builds still require external Flutter/Rust/signing toolchains.
