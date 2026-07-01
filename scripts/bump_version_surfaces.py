#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


FULL_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+\+[A-Za-z0-9_.-]+$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _json(path: Path) -> dict[str, Any]:
    return json.loads(_read(path))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    _write(path, json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


def _replace(path: Path, old: str, new: str) -> None:
    text = _read(path)
    if old not in text:
        raise RuntimeError(f"expected to find {old!r} in {path}")
    _write(path, text.replace(old, new))


def _replace_regex(path: Path, pattern: str, repl: str) -> None:
    text = _read(path)
    updated, count = re.subn(pattern, repl, text, flags=re.MULTILINE)
    if count == 0:
        raise RuntimeError(f"pattern not found in {path}: {pattern}")
    _write(path, updated)


def _replace_cargo_lock_package_version(path: Path, package_name: str, version: str) -> None:
    text = _read(path)
    pattern = (
        r'(^\[\[package\]\]\n'
        r'(?:(?!^\[\[package\]\]).)*?'
        rf'^name\s*=\s*"{re.escape(package_name)}"\n'
        r'(?:(?!^\[\[package\]\]).)*?'
        r'^version\s*=\s*")([^"]+)(")'
    )
    updated, count = re.subn(
        pattern,
        lambda match: f"{match.group(1)}{version}{match.group(3)}",
        text,
        count=1,
        flags=re.MULTILINE | re.DOTALL,
    )
    if count == 0:
        raise RuntimeError(f"could not find {package_name} package version in {path}")
    _write(path, updated)


def _update_package_lock(path: Path, app_version: str) -> None:
    payload = _json(path)
    payload["version"] = app_version
    packages = payload.setdefault("packages", {})
    root_package = packages.setdefault("", {})
    root_package["version"] = app_version
    _write_json(path, payload)


def bump(root: Path, *, full_version: str, android_version_code: int, ios_build_number: int) -> None:
    if not FULL_VERSION_RE.match(full_version):
        raise RuntimeError("full version must look like 1.12.8+root-monorepo-production-ga-candidate")
    app_version = full_version.split("+", 1)[0]
    if not SEMVER_RE.match(app_version):
        raise RuntimeError("app version must be semver")
    suffix = full_version.split("+", 1)[1]
    slug = f"Omni-desk-AI-{app_version}-{suffix.replace('+', '-')}"

    old_full = _json(root / "release" / "production-evidence.manifest.json").get("version", "")
    if not old_full:
        raise RuntimeError("could not determine current full version from production evidence manifest")
    old_app = old_full.split("+", 1)[0]

    # Full-version source surfaces.
    _replace_regex(root / "pyproject.toml", r'^version\s*=\s*"[^"]+"', f'version = "{full_version}"')
    _replace_regex(root / "omnidesk_agent" / "__init__.py", r'^__version__\s*=\s*"[^"]+"', f'__version__ = "{full_version}"')
    _replace(root / "VERSION", old_full, full_version)
    _replace(root / "Dockerfile", old_full, full_version)
    _replace(root / ".github" / "workflows" / "release.yml", old_full, full_version)
    _replace(root / ".github" / "workflows" / "deploy-staging.yml", old_full, full_version)
    _replace(root / ".github" / "workflows" / "promote-production.yml", old_full, full_version)
    _replace(root / "Makefile", old_full, full_version)
    _replace(root / "Makefile", f"Omni-desk-AI-{old_app}-{old_full.split('+', 1)[1].replace('+', '-')}", slug)

    contract = _json(root / "apps" / "shared" / "omni-app-api.contract.json")
    contract["version"] = full_version
    _write_json(root / "apps" / "shared" / "omni-app-api.contract.json", contract)

    evidence = _json(root / "release" / "production-evidence.manifest.json")
    evidence["version"] = full_version
    _write_json(root / "release" / "production-evidence.manifest.json", evidence)

    # Native/app-version surfaces.
    for package in ("web-admin-next", "desktop-tauri"):
        package_json_path = root / "apps" / package / "package.json"
        package_json = _json(package_json_path)
        package_json["version"] = app_version
        _write_json(package_json_path, package_json)
        _update_package_lock(root / "apps" / package / "package-lock.json", app_version)

    _replace_regex(root / "apps" / "desktop-tauri" / "src" / "App.tsx", r"^const VERSION = '[^']+'", f"const VERSION = '{app_version}'")
    tauri_conf = _json(root / "apps" / "desktop-tauri" / "src-tauri" / "tauri.conf.json")
    tauri_conf["version"] = app_version
    _write_json(root / "apps" / "desktop-tauri" / "src-tauri" / "tauri.conf.json", tauri_conf)
    _replace_regex(root / "apps" / "desktop-tauri" / "src-tauri" / "Cargo.toml", r'^version\s*=\s*"[^"]+"', f'version = "{app_version}"')
    _replace_cargo_lock_package_version(root / "apps" / "desktop-tauri" / "src-tauri" / "Cargo.lock", "omnidesk_desktop", app_version)

    _replace_regex(root / "deploy" / "kubernetes" / "helm" / "omnidesk" / "Chart.yaml", r'^version:\s*[^\s]+', f"version: {app_version}")
    _replace_regex(root / "deploy" / "kubernetes" / "helm" / "omnidesk" / "Chart.yaml", r'^appVersion:\s*[^\s]+', f"appVersion: {full_version}")

    _replace_regex(root / "apps" / "mobile-flutter" / "pubspec.yaml", r'^version:\s*[0-9.]+\+\d+', f"version: {app_version}+{android_version_code}")
    _replace_regex(root / "apps" / "mobile-flutter" / "android" / "app" / "build.gradle", r'versionCode\s+\d+;\s*versionName\s+"[^"]+"', f'versionCode {android_version_code}; versionName "{app_version}"')
    _replace_regex(root / "apps" / "mobile-flutter" / "ios" / "Flutter" / "Generated.xcconfig", r'^FLUTTER_BUILD_NAME=[^\s]+', f"FLUTTER_BUILD_NAME={app_version}")
    _replace_regex(root / "apps" / "mobile-flutter" / "ios" / "Flutter" / "Generated.xcconfig", r'^FLUTTER_BUILD_NUMBER=\d+', f"FLUTTER_BUILD_NUMBER={ios_build_number}")
    _replace_regex(root / "apps" / "mobile-flutter" / "ios" / "Runner" / "Info.plist", r'CFBundleShortVersionString</key><string>[^<]+</string>', f"CFBundleShortVersionString</key><string>{app_version}</string>")
    _replace_regex(root / "apps" / "mobile-flutter" / "ios" / "Runner" / "Info.plist", r'CFBundleVersion</key><string>\d+</string>', f"CFBundleVersion</key><string>{ios_build_number}</string>")

    changelog = _read(root / "CHANGELOG.md")
    if not changelog.startswith(f"## {full_version}\n"):
        _write(root / "CHANGELOG.md", f"## {full_version}\n\n- Prepared release surface bump for {full_version}.\n\n" + changelog)


def main() -> int:
    parser = argparse.ArgumentParser(description="Bump all OmniDesk version surfaces in a single, consistency-gated pass.")
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--version", required=True, help="Full version, e.g. 1.12.8+root-monorepo-production-ga-candidate")
    parser.add_argument("--android-version-code", type=int, required=True)
    parser.add_argument("--ios-build-number", type=int, required=True)
    args = parser.parse_args()
    bump(Path(args.root).resolve(), full_version=args.version, android_version_code=args.android_version_code, ios_build_number=args.ios_build_number)
    print(f"version surfaces bumped to {args.version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
