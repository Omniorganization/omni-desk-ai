from __future__ import annotations

import plistlib
from pathlib import Path

import yaml


def test_ios_xcode_project_has_real_build_configuration_graph() -> None:
    project = Path("apps/mobile-flutter/ios/Runner.xcodeproj/project.pbxproj").read_text(
        encoding="utf-8"
    )
    assert len(project.splitlines()) > 180
    assert "PBXNativeTarget" in project
    assert "XCBuildConfiguration" in project
    assert "XCConfigurationList" in project
    assert "buildConfigurationList" in project
    assert "PRODUCT_BUNDLE_IDENTIFIER = com.omnidesk.mobile;" in project
    assert "IPHONEOS_DEPLOYMENT_TARGET = 15.0;" in project
    assert "GeneratedPluginRegistrant.m in Sources" in project


def test_ios_podfile_applies_flutter_build_settings() -> None:
    podfile = Path("apps/mobile-flutter/ios/Podfile").read_text(encoding="utf-8")
    assert "post_install do |installer|" in podfile
    assert "flutter_additional_ios_build_settings(target)" in podfile
    assert "use_frameworks! :linkage => :static" in podfile
    assert "IPHONEOS_DEPLOYMENT_TARGET'] = '15.0'" in podfile


def test_ios_info_plist_uses_release_build_variables() -> None:
    with Path("apps/mobile-flutter/ios/Runner/Info.plist").open("rb") as handle:
        info = plistlib.load(handle)
    assert info["CFBundleIdentifier"] == "$(PRODUCT_BUNDLE_IDENTIFIER)"
    assert info["CFBundleShortVersionString"] == "$(FLUTTER_BUILD_NAME)"
    assert info["CFBundleVersion"] == "$(FLUTTER_BUILD_NUMBER)"
    assert info["NSFaceIDUsageDescription"]


def test_ios_workspace_and_storyboards_are_present() -> None:
    required = (
        "apps/mobile-flutter/ios/Runner.xcworkspace/contents.xcworkspacedata",
        "apps/mobile-flutter/ios/Runner.xcodeproj/xcshareddata/xcschemes/Runner.xcscheme",
        "apps/mobile-flutter/ios/Runner/Base.lproj/Main.storyboard",
        "apps/mobile-flutter/ios/Runner/Base.lproj/LaunchScreen.storyboard",
        "apps/mobile-flutter/ios/Flutter/AppFrameworkInfo.plist",
    )
    for path in required:
        assert Path(path).is_file(), path


def test_ios_keychain_entitlements_are_bound_without_enabling_push() -> None:
    runner = Path("apps/mobile-flutter/ios/Runner")
    with (runner / "Runner.entitlements").open("rb") as handle:
        entitlements = plistlib.load(handle)
    assert entitlements == {
        "keychain-access-groups": ["$(AppIdentifierPrefix)$(CFBundleIdentifier)"]
    }
    project = (runner.parent / "Runner.xcodeproj/project.pbxproj").read_text()
    assert project.count("CODE_SIGN_ENTITLEMENTS = Runner/Runner.entitlements;") == 3


def test_ios_native_smoke_has_separate_bounded_simulator_preparation() -> None:
    workflow = yaml.safe_load(Path(".github/workflows/tri-app-quality.yml").read_text())
    job = workflow["jobs"]["mobile-ios-simulator"]
    assert job["timeout-minutes"] == 30
    steps = job["steps"]
    prepare = next(step for step in steps if step.get("id") == "ios_simulator")
    native = next(step for step in steps if step.get("name") == "Native iOS Keychain and offline UI regression")
    assert steps.index(prepare) + 1 == steps.index(native)
    assert prepare["timeout-minutes"] == 8
    assert native["timeout-minutes"] == 15
    for step in (prepare, native):
        assert not step.get("continue-on-error", False)
        assert step.get("if", "success()") == "success()"
        assert 'test -n "$simulator_id"' in step["run"]
        assert step["run"].startswith("set -euo pipefail\n")
    assert "xcrun simctl list devices available -j" in prepare["run"]
    assert 'xcrun simctl bootstatus "$simulator_id" -b' in prepare["run"]
    assert prepare["run"].endswith('echo "udid=$simulator_id" >> "$GITHUB_OUTPUT"\n')
    assert native["env"]["simulator_id"] == "${{ steps.ios_simulator.outputs.udid }}"
    assert native["run"].splitlines() == [
        "set -euo pipefail",
        'test -n "$simulator_id"',
        'flutter test integration_test/ios_smoke_test.dart -d "$simulator_id" --no-pub --reporter expanded',
    ]
    assert any(step.get("with", {}).get("flutter-version") == "3.38.8" for step in steps)
    commands = {step.get("run") for step in steps}
    assert {
        "flutter pub get",
        "flutter analyze",
        "flutter test",
        "flutter build ios --simulator --debug",
        "python ../../scripts/check_tri_app_release_readiness.py ../.. --mode mobile-ios-source",
    } <= commands
