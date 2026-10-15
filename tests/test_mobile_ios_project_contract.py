from __future__ import annotations

import json
import plistlib
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml

from scripts.check_ios_xcresult import EXPECTED_CASE, main, validate_xcresult


def test_ios_xcode_project_has_real_build_configuration_graph() -> None:
    project = Path("apps/mobile-flutter/ios/Runner.xcodeproj/project.pbxproj").read_text(encoding="utf-8")
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
    assert entitlements == {"keychain-access-groups": ["$(AppIdentifierPrefix)$(CFBundleIdentifier)"]}
    project = (runner.parent / "Runner.xcodeproj/project.pbxproj").read_text()
    assert project.count("CODE_SIGN_ENTITLEMENTS = Runner/Runner.entitlements;") == 3


def test_ios_xctest_host_runs_the_official_dart_result_bridge() -> None:
    ios = Path("apps/mobile-flutter/ios")
    assert (ios / "RunnerTests/RunnerTests.m").read_text() == (
        "@import XCTest;\n@import integration_test;\n\nINTEGRATION_TEST_IOS_RUNNER(RunnerTests)\n"
    )
    project = (ios / "Runner.xcodeproj/project.pbxproj").read_text()
    assert 'productType = "com.apple.product-type.bundle.unit-test";' in project
    assert project.count('BUNDLE_LOADER = "$(TEST_HOST)";') == 3
    assert project.count("GENERATE_INFOPLIST_FILE = YES;") == 3
    host = 'TEST_HOST = "$(BUILT_PRODUCTS_DIR)/Runner.app/$(BUNDLE_EXECUTABLE_FOLDER_PATH)/Runner";'
    assert project.count(host) == 3
    assert "TestTargetID = 97C146ED1CF9000F007C117D;" in project
    assert "target = 97C146ED1CF9000F007C117D /* Runner */;" in project
    scheme = ET.parse(ios / "Runner.xcodeproj/xcshareddata/xcschemes/Runner.xcscheme")
    testable = scheme.find("./TestAction/Testables/TestableReference")
    assert testable is not None and testable.attrib["skipped"] == "NO"
    reference = testable.find("BuildableReference")
    assert reference is not None
    assert reference.attrib["BlueprintName"] == "RunnerTests"
    assert reference.attrib["BlueprintIdentifier"] in project
    assert "target 'RunnerTests' do\n    inherit! :search_paths" in (ios / "Podfile").read_text()


def test_ios_ci_requires_real_xctest_results_and_dart_negative_control() -> None:
    workflow = yaml.safe_load(Path(".github/workflows/tri-app-quality.yml").read_text())
    job = workflow["jobs"]["mobile-ios-simulator"]
    assert job["timeout-minutes"] == 30
    steps = job["steps"]
    prepare = next(s for s in steps if s.get("id") == "ios_simulator")
    native = next(s for s in steps if s.get("name") == "Native iOS Keychain and offline UI regression")
    assert prepare["timeout-minutes"] == 8
    assert native["timeout-minutes"] == 15
    assert steps.index(native) == steps.index(prepare) + 1
    for step in (prepare, native):
        assert not step.get("continue-on-error", False)
        assert step.get("if", "success()") == "success()"
        assert 'test -n "$simulator_id"' in step["run"]
        assert step["run"].startswith("set -euo pipefail\n")
    assert "xcrun simctl list devices available -j" in prepare["run"]
    assert 'xcrun simctl bootstatus "$simulator_id" -b' in prepare["run"]
    assert prepare["run"].endswith('echo "udid=$simulator_id" >> "$GITHUB_OUTPUT"\n')
    assert prepare["run"].index('bootstatus "$simulator_id" -b') < prepare["run"].index("$GITHUB_OUTPUT")
    assert native["env"]["simulator_id"] == "${{ steps.ios_simulator.outputs.udid }}"
    script = native["run"]
    assert 'test -n "$simulator_id"' in script
    assert "--simulator --debug --config-only --no-pub" in script
    assert "--dart-define=INTEGRATION_TEST_SHOULD_REPORT_RESULTS_TO_NATIVE=true" in script
    assert "config['FLUTTER_TARGET']" in script and "config['DART_DEFINES']" in script
    assert "original=integration_test/ios_smoke_test.dart" in script
    assert 'run_xctest negative "$negative" --expected-failure-marker OMNI_IOS_XCTEST_NEGATIVE_CONTROL' in script
    assert script.index('run_xctest negative "$negative"') < script.index('run_xctest positive "$original"')
    assert "source.count(needle) == 1" in script
    assert "manifest['original_sha256']" in script
    assert "xcodebuild test" in script and "-only-testing:RunnerTests" in script
    assert "|| xcode_status=$?" in script
    assert '--xcodebuild-exit "$xcode_status"' in script
    assert "--schema-version 0.1.0" in script
    assert "flutter test integration_test/" not in script
    assert not native.get("continue-on-error")
    commands = [s.get("run") for s in steps]
    assert "flutter pub get" in commands
    assert "flutter analyze" in commands and "flutter test" in commands
    assert "flutter build ios --simulator --debug" in commands
    assert "python ../../scripts/check_tri_app_release_readiness.py ../.. --mode mobile-ios-source" in commands
    assert any(s.get("with", {}).get("flutter-version") == "3.38.8" for s in steps)
    artifact = next(s for s in steps if s.get("name") == "Preserve iOS XCTest evidence")
    assert artifact["if"] == "always()"
    assert artifact["uses"] == "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"
    assert artifact["with"]["if-no-files-found"] == "error"


_NATIVE_FAILURE_MARKER = "OMNIDESK_NATIVE_ASSERTION_CONTROL"


def _native_xcresult_fixture(*, failed: bool = False) -> tuple[dict, dict]:
    result = "Failed" if failed else "Passed"
    case = {
        "nodeType": "Test Case",
        "name": EXPECTED_CASE.split("/", 1)[1] + "()",
        "nodeIdentifier": EXPECTED_CASE,
        "result": result,
    }
    tests = {
        "testNodes": [
            {
                "nodeType": "Test Plan",
                "name": "Runner",
                "result": result,
                "children": [
                    {
                        "nodeType": "Unit test bundle",
                        "name": "RunnerTests",
                        "result": result,
                        "children": [
                            {
                                "nodeType": "Test Suite",
                                "name": "RunnerTests",
                                "result": result,
                                "children": [case],
                            }
                        ],
                    }
                ],
            }
        ]
    }
    failures = (
        [
            {
                "targetName": "RunnerTests",
                "testIdentifierString": EXPECTED_CASE + "()",
                "failureText": f"XCTAssertTrue failed: {_NATIVE_FAILURE_MARKER}",
            }
        ]
        if failed
        else []
    )
    summary = {
        "result": result,
        "totalTestCount": 1,
        "passedTests": 0 if failed else 1,
        "failedTests": 1 if failed else 0,
        "skippedTests": 0,
        "expectedFailures": 0,
        "testFailures": failures,
    }
    return summary, tests


def _native_xcresult_case(tests: dict) -> dict:
    return tests["testNodes"][0]["children"][0]["children"][0]["children"][0]


@pytest.mark.parametrize("parentheses", [False, True])
def test_native_xcresult_accepts_complete_passed_case(parentheses: bool) -> None:
    summary, tests = _native_xcresult_fixture()
    if parentheses:
        _native_xcresult_case(tests)["nodeIdentifier"] += "()"
    validate_xcresult(summary, tests, 0)


@pytest.mark.parametrize(
    "fault",
    [
        "exit",
        "missing_case",
        "zero_tests",
        "wrong_case",
        "wrong_name",
        "wrong_bundle",
        "wrong_suite",
        "count",
        "bool_count",
        "float_count",
        "skip",
        "expected_failure",
        "unknown_result",
        "unknown_node",
        "malformed_node",
        "tree_not_array",
        "failures_not_array",
        "failure_record",
        "missing_result",
        "wrapper_failed",
        "failure_message",
        "duplicate_id",
    ],
)
def test_native_xcresult_rejects_incomplete_or_false_green(fault: str) -> None:
    summary, tests = _native_xcresult_fixture()
    case = _native_xcresult_case(tests)
    plan = tests["testNodes"][0]
    bundle = plan["children"][0]
    suite = bundle["children"][0]
    exit_code = 0
    if fault == "exit":
        exit_code = 65
    elif fault == "missing_case":
        tests["testNodes"] = []
    elif fault == "zero_tests":
        summary.update(totalTestCount=0, passedTests=0)
        tests["testNodes"] = []
    elif fault == "wrong_case":
        case["nodeIdentifier"] = "RunnerTests/testUnrelated"
    elif fault == "wrong_name":
        case["name"] = "testUnrelated()"
    elif fault == "wrong_bundle":
        bundle["name"] = "OtherTests"
    elif fault == "wrong_suite":
        suite["name"] = "OtherTests"
    elif fault == "count":
        summary["totalTestCount"] = 2
    elif fault == "bool_count":
        summary["totalTestCount"] = True
    elif fault == "float_count":
        summary["totalTestCount"] = 1.0
    elif fault == "skip":
        summary["skippedTests"] = 1
    elif fault == "expected_failure":
        summary["expectedFailures"] = 1
    elif fault == "unknown_result":
        case["result"] = "unknown"
    elif fault == "unknown_node":
        plan["nodeType"] = "Future Node"
    elif fault == "malformed_node":
        plan["nodeType"] = []
    elif fault == "tree_not_array":
        tests["testNodes"] = {}
    elif fault == "failures_not_array":
        summary["testFailures"] = {}
    elif fault == "failure_record":
        summary["testFailures"] = [{"failureText": "unrelated session failure"}]
    elif fault == "missing_result":
        del case["result"]
    elif fault == "wrapper_failed":
        plan["result"] = "Failed"
    elif fault == "failure_message":
        case["children"] = [{"nodeType": "Failure Message", "name": "hidden failure"}]
    elif fault == "duplicate_id":
        suite["children"].append(dict(case))
        summary["totalTestCount"] = summary["passedTests"] = 2
    with pytest.raises(ValueError):
        validate_xcresult(summary, tests, exit_code)


def test_native_xcresult_accepts_failed_expected_case_with_bound_marker() -> None:
    summary, tests = _native_xcresult_fixture(failed=True)
    validate_xcresult(summary, tests, 65, _NATIVE_FAILURE_MARKER)


@pytest.mark.parametrize(
    "fault",
    [
        "zero_exit",
        "compile_failure",
        "wrong_marker",
        "unbound_marker",
        "wrong_target",
        "case_passed",
        "summary_passed",
        "count",
        "empty_marker",
        "no_failure_records",
    ],
)
def test_native_xcresult_negative_control_requires_real_bound_failure(fault: str) -> None:
    summary, tests = _native_xcresult_fixture(failed=True)
    exit_code, marker = 65, _NATIVE_FAILURE_MARKER
    if fault == "zero_exit":
        exit_code = 0
    elif fault == "compile_failure":
        summary.update(totalTestCount=0, failedTests=0)
        tests["testNodes"] = []
    elif fault == "wrong_marker":
        summary["testFailures"][0]["failureText"] = "some other assertion"
    elif fault == "unbound_marker":
        summary["testFailures"][0]["testIdentifierString"] = "RunnerTests/testOther()"
    elif fault == "wrong_target":
        summary["testFailures"][0]["targetName"] = "OtherTests"
    elif fault == "case_passed":
        _native_xcresult_case(tests)["result"] = "Passed"
    elif fault == "summary_passed":
        summary["result"] = "Passed"
    elif fault == "count":
        summary["failedTests"] = 2
    elif fault == "empty_marker":
        marker = " "
    elif fault == "no_failure_records":
        summary["testFailures"] = []
    with pytest.raises(ValueError):
        validate_xcresult(summary, tests, exit_code, marker)


def test_native_xcresult_cli_rejects_missing_or_invalid_export(tmp_path) -> None:
    summary, tests = _native_xcresult_fixture()
    summary_path, tests_path = tmp_path / "summary.json", tmp_path / "tests.json"
    args = ["--summary", str(summary_path), "--tests", str(tests_path), "--xcodebuild-exit", "0"]
    assert main(args) == 1
    summary_path.write_text(json.dumps(summary))
    tests_path.write_text(json.dumps(tests))
    assert main(args) == 0
    tests_path.write_text("{invalid json")
    assert main(args) == 1
