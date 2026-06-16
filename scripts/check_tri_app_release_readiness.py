from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REQUIRED_ENV_GROUPS = {
    "ios_signing": ["APPLE_TEAM_ID", "IOS_BUNDLE_ID", "IOS_PROVISIONING_PROFILE"],
    "android_signing": ["ANDROID_APPLICATION_ID", "ANDROID_KEYSTORE_PATH", "ANDROID_KEY_ALIAS"],
    "desktop_signing": ["TAURI_BUNDLE_IDENTIFIER", "MACOS_SIGNING_IDENTITY", "WINDOWS_SIGNING_CERT_PATH"],
    "push_notifications": ["FIREBASE_PROJECT_ID", "APNS_KEY_ID", "APNS_TEAM_ID"],
    "device_login": ["OMNIDESK_OWNER_TOKEN", "OMNIDESK_OPERATOR_TOKEN", "OMNIDESK_VIEWER_TOKEN"],
}

def _version(command: str) -> str:
    try:
        completed = subprocess.run([command, "--version"], check=False, capture_output=True, text=True, timeout=10)
    except Exception as exc:
        return f"unavailable: {exc}"
    output = (completed.stdout or completed.stderr).strip().splitlines()
    return output[0] if output else "version output unavailable"

def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))

def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")

def _check(condition: bool, message: str, failures: list[str], ok: list[str]) -> None:
    (ok if condition else failures).append(message)

def _toolchain_required(mode: str) -> list[str]:
    if mode == "source": return ["node", "npm"]
    if mode == "desktop-release": return ["node", "npm", "cargo", "rustc"]
    if mode in {"mobile-release", "mobile-android-release", "mobile-ios-release"}: return ["flutter"]
    return ["node", "npm", "flutter", "cargo", "rustc"]

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check OmniDesk tri-app readiness.")
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--mode", choices=["source", "release", "desktop-release", "mobile-release", "mobile-android-release", "mobile-ios-release"], default="release")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve(); apps = root / "apps"
    failures: list[str] = []; warnings: list[str] = []; ok: list[str] = []
    _check(sys.version_info >= (3, 10), f"Python runtime is >=3.10 ({sys.version.split()[0]})", failures, ok)
    required_files = [apps/"shared"/"omni-app-api.contract.json", apps/"web-admin-next"/"package.json", apps/"web-admin-next"/"lib"/"gateway.ts", apps/"desktop-tauri"/"package.json", apps/"desktop-tauri"/"src-tauri"/"tauri.conf.json", apps/"desktop-tauri"/"src-tauri"/"build.rs", apps/"desktop-tauri"/"src-tauri"/"icons"/"icon.png", apps/"mobile-flutter"/"pubspec.yaml"]
    for path in required_files: _check(path.exists(), f"Required tri-app file exists: {path.relative_to(root)}", failures, ok)
    for command in _toolchain_required(args.mode):
        if shutil.which(command): ok.append(f"{command} available: {_version(command)}")
        elif args.mode == "source": warnings.append(f"{command} not found; source-mode does not require release toolchains")
        else: failures.append(f"{command} is required for {args.mode} builds but was not found on PATH")
    web_package = _load_json(apps/"web-admin-next"/"package.json"); desktop_package = _load_json(apps/"desktop-tauri"/"package.json")
    contract = _load_json(apps/"shared"/"omni-app-api.contract.json"); tauri_config = _load_json(apps/"desktop-tauri"/"src-tauri"/"tauri.conf.json"); web_tsconfig = _load_json(apps/"web-admin-next"/"tsconfig.json")
    web_docker = _read(apps/"web-admin-next"/"Dockerfile")
    for script in ["build", "typecheck", "test"]: _check(script in web_package.get("scripts", {}), f"web-admin-next has npm script: {script}", failures, ok)
    for script in ["build", "typecheck", "test", "tauri:build"]: _check(script in desktop_package.get("scripts", {}), f"desktop-tauri has npm script: {script}", failures, ok)
    _check((apps/"web-admin-next"/"public").is_dir(), "Web Admin public directory exists for Docker runtime copy", failures, ok)
    _check("COPY --from=build /app/public ./public" in web_docker, "Web Admin Docker runtime copies public assets from build stage", failures, ok)
    _check("NODE_BASE_IMAGE=node:22-bookworm-slim@sha256:" in web_docker and "FROM node:" not in web_docker, "Web Admin Docker base image is digest-pinned", failures, ok)
    _check("USER 10001:10001" in web_docker and "HEALTHCHECK" in web_docker and 'VOLUME ["/tmp"]' in web_docker, "Web Admin Docker runtime is non-root, healthchecked, and read-only-rootfs ready", failures, ok)
    _check("output: 'standalone'" in _read(apps/"web-admin-next"/"next.config.mjs"), "Web Admin Next standalone output is enabled", failures, ok)
    contract_methods = {(item["method"], item["path"]) for item in contract.get("endpoints", [])}
    expected_contract = {("GET", "/app/bootstrap"), ("POST", "/app/devices/register"), ("POST", "/app/approvals/{approval_id}/decide"), ("POST", "/app/runtime/desktop/claim"), ("POST", "/app/devices/enrollment/{enrollment_id}/challenge"), ("POST", "/app/devices/enrollment/{enrollment_id}/verify"), ("GET", "/app/sync"), ("WS", "/app/ws")}
    missing_contract = sorted(expected_contract - contract_methods); _check(not missing_contract, f"shared API contract includes release-critical endpoints: {missing_contract or 'all present'}", failures, ok)
    contract_headers = "\n".join(contract.get("auth", {}).get("headers", []))
    for header in ["X-OmniDesk-Device-Id", "X-OmniDesk-Timestamp", "X-OmniDesk-Nonce", "X-OmniDesk-Device-Signature"]:
        _check(header in contract_headers, f"shared API contract declares device signature header: {header}", failures, ok)
    _check(tauri_config.get("identifier") == "ai.omnidesk.desktop", "Tauri bundle identifier is set", failures, ok)
    _check(web_tsconfig.get("compilerOptions", {}).get("paths", {}).get("@/*") == ["./*"], "Web Admin @ path alias resolves application sources", failures, ok)
    _check(bool(tauri_config.get("bundle", {}).get("active")), "Tauri bundle generation is enabled", failures, ok)
    csp = tauri_config.get("app", {}).get("security", {}).get("csp"); _check(bool(csp) and "default-src" in csp, "Tauri CSP is enforced", failures, ok)
    desktop_app = (apps/"desktop-tauri"/"src"/"App.tsx").read_text(encoding="utf-8")
    _check("localStorage.getItem('omni.operatorToken')" not in desktop_app and "secure_get" in desktop_app, "Desktop token is stored through secure OS storage", failures, ok)
    _check((apps/"desktop-tauri"/"src"/"executor.ts").exists(), "Desktop capability executor exists", failures, ok)
    _check("RuntimeExecutor" in (apps/"desktop-tauri"/"src"/"executor.ts").read_text(encoding="utf-8"), "Desktop RuntimeExecutor contract is declared", failures, ok)
    desktop_main = _read(apps/"desktop-tauri"/"src-tauri"/"src"/"main.rs")
    desktop_cargo = _read(apps/"desktop-tauri"/"src-tauri"/"Cargo.toml")
    uses_dirs_crate = "dirs::" in desktop_main
    declares_dirs_crate = bool(re.search(r'^\s*dirs\s*=', desktop_cargo, re.MULTILINE))
    _check((not uses_dirs_crate) or declares_dirs_crate, "Desktop Rust has no undeclared dirs crate usage", failures, ok)
    _check("home_directory()" in desktop_main and "std::env::var_os" in desktop_main, "Desktop workspace resolver is stdlib-only and release-checkable", failures, ok)
    desktop_build = _read(apps/"desktop-tauri"/"src-tauri"/"build.rs")
    _check("tauri_build::build()" in desktop_build, "Desktop Tauri build script generates macro context", failures, ok)
    mobile_pubspec = (apps/"mobile-flutter"/"pubspec.yaml").read_text(encoding="utf-8")
    _check("flutter_secure_storage" in mobile_pubspec and "local_auth" in mobile_pubspec, "Mobile secure storage and biometric/PIN dependencies are declared", failures, ok)
    _check("firebase_messaging" in mobile_pubspec, "Mobile push dependency is declared", failures, ok)
    mobile_push = _read(apps/"mobile-flutter"/"lib"/"push_service.dart")
    _check("platform: 'mobile'" in mobile_push, "Mobile push token registration passes platform as a named argument", failures, ok)
    _check("registerPushToken(deviceId, token, 'mobile')" not in mobile_push and "registerPushToken(deviceId, newToken, 'mobile')" not in mobile_push, "Mobile push service has no stale positional platform calls", failures, ok)
    _check("catch (_)" in mobile_push and "return null" in mobile_push, "Mobile push service degrades when platform Firebase config is unavailable", failures, ok)
    mobile_gradle = _read(apps/"mobile-flutter"/"android/app/build.gradle")
    _check("compileSdk 36" in mobile_gradle or "compileSdk = 36" in mobile_gradle, "Android release build compiles against SDK 36", failures, ok)
    _check('ndkVersion "27.0.12077973"' in mobile_gradle or 'ndkVersion = "27.0.12077973"' in mobile_gradle, "Android release build pins NDK 27 for native plugins", failures, ok)
    _check("minSdk 24" in mobile_gradle or "minSdkVersion 24" in mobile_gradle, "Android release build minSdk satisfies local_auth_android", failures, ok)
    _check("sourceCompatibility JavaVersion.VERSION_17" in mobile_gradle and "targetCompatibility JavaVersion.VERSION_17" in mobile_gradle and 'jvmTarget = "17"' in mobile_gradle, "Android release Java and Kotlin JVM targets are aligned to 17", failures, ok)
    _check("com.google.gms.google-services\" apply false" in mobile_gradle and "hasGoogleServicesConfig" in mobile_gradle, "Android Google Services plugin is conditional on Firebase config", failures, ok)
    for rel in ["android/app/build.gradle", "android/app/src/main/AndroidManifest.xml", "android/app/src/main/res/drawable/ic_launcher.xml", "android/app/src/main/res/drawable/launch_background.xml", "android/app/src/main/res/values/styles.xml", "ios/Runner/AppDelegate.swift", "ios/Runner/Info.plist", "ios/Runner.xcodeproj/project.pbxproj", "ios/Flutter/Generated.xcconfig"]:
        _check((apps/"mobile-flutter"/rel).exists(), f"Mobile native scaffold exists: apps/mobile-flutter/{rel}", failures, ok)
    registrant = _read(apps/"mobile-flutter"/"ios"/"Runner"/"GeneratedPluginRegistrant.swift")
    _check("final class GeneratedPluginRegistrant" in registrant and "static func register(with registry: FlutterPluginRegistry)" in registrant, "iOS GeneratedPluginRegistrant matches AppDelegate call shape", failures, ok)
    if args.mode in {"release", "mobile-ios-release"}:
        _check("Source-package placeholder" not in registrant, "iOS plugin registrant has been regenerated by Flutter release CI", failures, ok)
    _check((apps/"mobile-flutter"/"android/app/src/main/kotlin/com/omnidesk/mobile/MainActivity.kt").exists(), "Android MainActivity matches com.omnidesk.mobile namespace", failures, ok)
    _check(not (apps/"mobile-flutter"/"android/app/src/main/kotlin/ai/omnidesk/mobile/MainActivity.kt").exists(), "Android legacy ai.omnidesk.mobile MainActivity is absent", failures, ok)
    makefile = _read(root/"Makefile")
    release_contract = makefile + "\n" + _read(root/".github/workflows/tri-app-quality.yml")
    _check("tri-app-release-builds" in makefile and "tri-app-release-web" in makefile and "tri-app-release-desktop" in makefile and "tri-app-release-mobile" in makefile, "Makefile declares strict tri-app release build targets", failures, ok)
    _check("cargo check --locked" in release_contract, "Desktop release gate uses cargo check --locked", failures, ok)
    _check("flutter build appbundle --release" in release_contract, "Mobile release gate requires Android appbundle build", failures, ok)
    _check("OMNI_ANDROID_KEYSTORE" in release_contract, "Mobile Android release gate provisions signing credentials", failures, ok)
    _check("flutter build ipa --release" in release_contract, "Mobile release gate requires iOS release build", failures, ok)
    _check("dart analyze" in release_contract, "Mobile release gate runs Dart analyzer", failures, ok)
    _check("npm ci" in release_contract, "Web/Desktop release gate uses npm ci", failures, ok)
    if args.mode != "source":
        for group, names in REQUIRED_ENV_GROUPS.items():
            missing = [name for name in names if not os.environ.get(name)]
            if missing: warnings.append(f"{group} env not fully configured: {', '.join(missing)}")
            else: ok.append(f"{group} env configured")
    print(f"Tri-app readiness ({args.mode})")
    for message in ok: print(f"OK      {message}")
    for message in warnings: print(f"WARN    {message}")
    for message in failures: print(f"BLOCKER {message}")
    return 1 if failures else 0

if __name__ == "__main__":
    raise SystemExit(main())
