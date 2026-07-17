from __future__ import annotations

import plistlib
from pathlib import Path


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
