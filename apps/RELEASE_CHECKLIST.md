# Omni Tri-App Release Checklist

This source drop is a development and build foundation. It is not a signed `.ipa`, `.apk`, `.dmg`, or Windows installer.

## Local Functional Gate

Run these before any signed build work:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e ".[test]"
make tri-app-quality PYTHON=.venv/bin/python
```

For a release machine, also run:

```bash
.venv/bin/python scripts/check_tri_app_release_readiness.py .
```

## Backend Gateway

- Set `OMNIDESK_OWNER_TOKEN`, `OMNIDESK_OPERATOR_TOKEN`, and `OMNIDESK_VIEWER_TOKEN`.
- Run `python -m pytest -q tests/test_tri_app_foundation.py`.
- Confirm the shared contract in `apps/shared/omni-app-api.contract.json` matches every `/app/*` route used by the three clients.

## Web Admin

- Run `npm install`, `npm test`, `npm run typecheck`, and `npm run build` in `apps/web-admin-next`.
- Configure the production Gateway URL through deployment environment or runtime configuration.
- Deploy behind the same auth boundary as the Gateway.

## Desktop Tauri

- Install Rust, Cargo, and the Tauri CLI.
- Run `npm install`, `npm test`, `npm run typecheck`, `npm run build`, and `npm run tauri:build` in `apps/desktop-tauri`.
- Configure macOS Developer ID signing and notarization before distributing `.dmg`.
- Configure Windows code signing before producing installer artifacts.

## Mobile Flutter

- Install Flutter and platform SDKs.
- Run `flutter create . --platforms=android,ios` once in `apps/mobile-flutter` if platform folders are not present.
- Run `flutter pub get`, `flutter test`, and `flutter analyze`.
- Configure Android application id, keystore, key alias, and Play signing.
- Configure iOS bundle id, Apple team, provisioning profile, and push notification entitlements.

## Push Notifications And Device Login

- Configure Firebase Cloud Messaging for Android and APNs for iOS.
- Store mobile push tokens through `/app/devices/register`.
- Keep device login tokens role-scoped: viewer for read-only, operator for task/device operations, owner for approval decisions.
- Rotate tokens before production rollout and verify login on at least one physical iOS device, one physical Android device, one macOS desktop build, and one Windows desktop build.
