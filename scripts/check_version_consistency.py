#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import plistlib
import re
import sys
from pathlib import Path


def _read(path: Path) -> str:
    if not path.exists():
        raise RuntimeError(f"missing file: {path}")
    return path.read_text(encoding="utf-8")


def _regex(path: Path, pattern: str, label: str) -> str:
    text = _read(path)
    match = re.search(pattern, text, re.MULTILINE)
    if not match:
        raise RuntimeError(f"could not find {label} in {path}")
    return match.group(1)


def _json(path: Path) -> dict:
    return json.loads(_read(path))


def _plist(path: Path) -> dict:
    if not path.exists():
        raise RuntimeError(f"missing file: {path}")
    with path.open("rb") as handle:
        loaded = plistlib.load(handle)
    if not isinstance(loaded, dict):
        raise RuntimeError(f"plist root must be a dictionary: {path}")
    return loaded


def _all_regex(path: Path, pattern: str, label: str) -> list[str]:
    text = _read(path)
    values = re.findall(pattern, text, re.MULTILINE)
    if not values:
        raise RuntimeError(f"could not find {label} in {path}")
    return list(values)


def _cargo_lock_package_version(path: Path, package_name: str) -> str:
    text = _read(path)
    pattern = (
        r'^\[\[package\]\]\n'
        r'(?:(?!^\[\[package\]\]).)*?'
        rf'^name\s*=\s*"{re.escape(package_name)}"\n'
        r'(?:(?!^\[\[package\]\]).)*?'
        r'^version\s*=\s*"([^"]+)"'
    )
    match = re.search(pattern, text, re.MULTILINE | re.DOTALL)
    if not match:
        raise RuntimeError(f"could not find {package_name} package version in {path}")
    return match.group(1)


def _native_version(app_version: str) -> str:
    parts = app_version.split(".")
    if len(parts) == 2:
        return f"{app_version}.0"
    return app_version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check OmniDesk package, workflow, Docker, and changelog version consistency.")
    parser.add_argument("root", nargs="?", default=".")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()

    full_sources: dict[str, str] = {}
    full_sources["VERSION"] = _read(root / "VERSION").strip()
    full_sources["pyproject.toml"] = _regex(root / "pyproject.toml", r'^version\s*=\s*"([^"]+)"', "project version")
    full_version = full_sources["VERSION"]
    app_version = full_version.split("+", 1)[0]
    native_version = _native_version(app_version)
    app_sources: dict[str, str] = {}
    chart_sources: dict[str, str] = {}

    full_sources["omnidesk_agent/__init__.py"] = _regex(root / "omnidesk_agent" / "__init__.py", r'^__version__\s*=\s*"([^"]+)"', "package __version__")

    docker_versions = _all_regex(root / "Dockerfile", r'^ARG\s+OMNIDESK_VERSION=([^\s]+)', "Dockerfile OMNIDESK_VERSION")
    for idx, value in enumerate(docker_versions, start=1):
        full_sources[f"Dockerfile ARG OMNIDESK_VERSION #{idx}"] = value

    full_sources[".github/workflows/release.yml expected version"] = _regex(
        root / ".github" / "workflows" / "release.yml",
        r'^\s*EXPECTED_VERSION:\s*([^\s]+)',
        "release.yml EXPECTED_VERSION",
    )

    for workflow in ("deploy-staging.yml", "promote-production.yml"):
        full_sources[f".github/workflows/{workflow} expected_version default"] = _regex(
            root / ".github" / "workflows" / workflow,
            r'expected_version:[\s\S]*?default:\s*([^\s]+)',
            f"{workflow} expected_version default",
        )

    full_sources["CHANGELOG.md latest heading"] = _regex(root / "CHANGELOG.md", r'^##\s+([^\s]+)', "latest changelog heading")
    full_sources["apps/shared/omni-app-api.contract.json"] = _json(root / "apps" / "shared" / "omni-app-api.contract.json").get("version", "")
    full_sources["release/production-evidence.manifest.json"] = _json(root / "release" / "production-evidence.manifest.json").get("version", "")
    full_sources["Helm appVersion"] = _regex(root / "deploy" / "kubernetes" / "helm" / "omnidesk" / "Chart.yaml", r'^appVersion:\s*([^\s]+)', "Helm appVersion")

    for package in ("web-admin-next", "desktop-tauri"):
        package_json = _json(root / "apps" / package / "package.json")
        package_lock = _json(root / "apps" / package / "package-lock.json")
        app_sources[f"apps/{package}/package.json"] = package_json.get("version", "")
        app_sources[f"apps/{package}/package-lock.json root"] = package_lock.get("version", "")
        app_sources[f"apps/{package}/package-lock.json package"] = package_lock.get("packages", {}).get("", {}).get("version", "")

    app_sources["apps/desktop-tauri/src/App.tsx"] = _regex(root / "apps" / "desktop-tauri" / "src" / "App.tsx", r"^const VERSION = '([^']+)'", "desktop app version")

    chart_sources["Helm chart version"] = _regex(root / "deploy" / "kubernetes" / "helm" / "omnidesk" / "Chart.yaml", r'^version:\s*([^\s]+)', "Helm chart version")
    chart_sources["apps/desktop-tauri/src-tauri/tauri.conf.json"] = _json(root / "apps" / "desktop-tauri" / "src-tauri" / "tauri.conf.json").get("version", "")
    chart_sources["apps/desktop-tauri/src-tauri/Cargo.toml"] = _regex(root / "apps" / "desktop-tauri" / "src-tauri" / "Cargo.toml", r'^version\s*=\s*"([^"]+)"', "desktop Cargo version")
    chart_sources["apps/desktop-tauri/src-tauri/Cargo.lock package"] = _cargo_lock_package_version(root / "apps" / "desktop-tauri" / "src-tauri" / "Cargo.lock", "omnidesk_desktop")
    chart_sources["apps/mobile-flutter/pubspec.yaml"] = _regex(root / "apps" / "mobile-flutter" / "pubspec.yaml", r'^version:\s*([0-9.]+)\+\d+', "mobile pubspec version")
    chart_sources["apps/mobile-flutter/android/app/build.gradle"] = _regex(root / "apps" / "mobile-flutter" / "android" / "app" / "build.gradle", r'versionName\s+"([^"]+)"', "Android versionName")
    chart_sources["apps/mobile-flutter/ios/Flutter/Generated.xcconfig"] = _regex(root / "apps" / "mobile-flutter" / "ios" / "Flutter" / "Generated.xcconfig", r'^FLUTTER_BUILD_NAME=([^\s]+)', "iOS Flutter build name")

    failures: list[str] = []
    for label, value in sorted(full_sources.items()):
        if value != full_version:
            failures.append(f"{label}: expected {full_version}, got {value}")
    for label, value in sorted(app_sources.items()):
        if value != app_version:
            failures.append(f"{label}: expected {app_version}, got {value}")
    for label, value in sorted(chart_sources.items()):
        if value != native_version:
            failures.append(f"{label}: expected {native_version}, got {value}")

    ios_info_path = root / "apps" / "mobile-flutter" / "ios" / "Runner" / "Info.plist"
    ios_info = _plist(ios_info_path)
    expected_ios_variables = {
        "CFBundleIdentifier": "$(PRODUCT_BUNDLE_IDENTIFIER)",
        "CFBundleShortVersionString": "$(FLUTTER_BUILD_NAME)",
        "CFBundleVersion": "$(FLUTTER_BUILD_NUMBER)",
    }
    for key, expected in expected_ios_variables.items():
        actual = ios_info.get(key)
        if actual != expected:
            failures.append(f"{ios_info_path.relative_to(root)} {key}: expected {expected}, got {actual}")

    if failures:
        print("version consistency check failed:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    print(f"version consistency ok: {full_version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
