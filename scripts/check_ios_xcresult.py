from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


EXPECTED_CASE = "RunnerTests/testNativeKeychainWorksAndOfflineSyncReportsEnrollment"
KNOWN_NODE_TYPES = {
    "Test Plan",
    "Unit test bundle",
    "UI test bundle",
    "Test Suite",
    "Test Case",
    "Device",
    "Test Plan Configuration",
    "Arguments",
    "Repetition",
    "Test Case Run",
    "Failure Message",
    "Source Code Reference",
    "Attachment",
    "Expression",
    "Test Value",
    "Runtime Warning",
}


def _case_id(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("test case identifier must be a nonempty string")
    return value.removesuffix("()")


def _test_cases(
    nodes: object,
    ancestors: tuple[tuple[str, str], ...] = (),
    *,
    require_passed: bool = False,
) -> list[dict]:
    if not isinstance(nodes, list):
        raise ValueError("test nodes must be an array")
    cases = []
    for node in nodes:
        if not isinstance(node, dict) or not isinstance(node.get("nodeType"), str) or node["nodeType"] not in KNOWN_NODE_TYPES:
            raise ValueError("unknown test node type")
        kind, name = node["nodeType"], node.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("test node name is missing")
        if "result" in node and node["result"] not in ("Passed", "Failed"):
            raise ValueError("test tree contains a skipped, expected, or unknown result")
        if require_passed and (node.get("result") == "Failed" or kind == "Failure Message"):
            raise ValueError("successful native evidence contains a failure")
        if kind == "Test Case":
            if node.get("result") not in ("Passed", "Failed"):
                raise ValueError("test case result is missing")
            _case_id(node.get("nodeIdentifier"))
            if _case_id(node["nodeIdentifier"]) == EXPECTED_CASE:
                if ("Unit test bundle", "RunnerTests") not in ancestors:
                    raise ValueError("expected case is outside the RunnerTests unit test bundle")
                if ("Test Suite", "RunnerTests") not in ancestors:
                    raise ValueError("expected case is outside the RunnerTests suite")
                if _case_id(name) != EXPECTED_CASE.split("/", 1)[1]:
                    raise ValueError("expected case name does not match its identifier")
            cases.append(node)
        cases.extend(
            _test_cases(
                node.get("children", []),
                ancestors + ((kind, name),),
                require_passed=require_passed,
            )
        )
    return cases


def validate_xcresult(
    summary: object,
    tests: object,
    xcodebuild_exit: int,
    expected_failure_marker: str | None = None,
) -> None:
    if not isinstance(summary, dict) or not isinstance(tests, dict):
        raise ValueError("summary and tests must be JSON objects")
    if type(xcodebuild_exit) is not int:
        raise ValueError("xcodebuild exit status must be an integer")
    if expected_failure_marker is not None and not expected_failure_marker.strip():
        raise ValueError("expected failure marker must not be empty")
    counts = {}
    for field in ("totalTestCount", "passedTests", "failedTests", "skippedTests", "expectedFailures"):
        value = summary.get(field)
        if type(value) is not int or value < 0:
            raise ValueError(f"{field} must be a nonnegative integer")
        counts[field] = value
    if counts["totalTestCount"] < 1 or counts["skippedTests"] or counts["expectedFailures"]:
        raise ValueError("native regression must execute tests without skips or expected failures")
    cases = _test_cases(tests.get("testNodes"), require_passed=expected_failure_marker is None)
    by_id = {_case_id(case["nodeIdentifier"]): case for case in cases}
    if len(by_id) != len(cases) or EXPECTED_CASE not in by_id:
        raise ValueError("expected native case is missing or test identifiers are duplicated")
    if len(cases) != counts["totalTestCount"]:
        raise ValueError("test tree and summary total disagree")
    for result, field in (("Passed", "passedTests"), ("Failed", "failedTests")):
        if sum(case["result"] == result for case in cases) != counts[field]:
            raise ValueError(f"test tree and summary {field} disagree")
    failures = summary.get("testFailures")
    if not isinstance(failures, list):
        raise ValueError("summary testFailures must be an array")
    if expected_failure_marker is None:
        if xcodebuild_exit != 0 or summary.get("result") != "Passed" or failures:
            raise ValueError("xcodebuild and summary must both report success")
        if counts["failedTests"] or counts["passedTests"] != counts["totalTestCount"]:
            raise ValueError("every native test case must pass")
        return
    if xcodebuild_exit == 0 or summary.get("result") != "Failed" or not counts["failedTests"]:
        raise ValueError("negative control must be an actual failed native test run")
    if by_id[EXPECTED_CASE]["result"] != "Failed" or counts["failedTests"] != 1:
        raise ValueError("negative control must fail the expected native case")
    marked_expected_failure = False
    failure_ids = set()
    for failure in failures:
        if not isinstance(failure, dict) or not isinstance(failure.get("failureText"), str):
            raise ValueError("invalid summary failure record")
        identifier = _case_id(failure.get("testIdentifierString"))
        if identifier not in by_id or by_id[identifier]["result"] != "Failed":
            raise ValueError("failure record is not bound to a failed test case")
        if failure.get("targetName") != "RunnerTests":
            raise ValueError("failure record is outside the native regression target")
        failure_ids.add(identifier)
        if identifier == EXPECTED_CASE and expected_failure_marker in failure["failureText"]:
            marked_expected_failure = True
    if failure_ids != {identifier for identifier, case in by_id.items() if case["result"] == "Failed"}:
        raise ValueError("summary failure records do not cover the failed test cases")
    if not marked_expected_failure:
        raise ValueError("expected native case failure does not contain the control marker")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify native iOS Dart results exported by xcresulttool")
    parser.add_argument("--summary", required=True, type=Path)
    parser.add_argument("--tests", required=True, type=Path)
    parser.add_argument("--xcodebuild-exit", required=True, type=int)
    parser.add_argument("--expected-failure-marker")
    args = parser.parse_args(argv)
    try:
        validate_xcresult(
            json.loads(args.summary.read_text(encoding="utf-8")),
            json.loads(args.tests.read_text(encoding="utf-8")),
            args.xcodebuild_exit,
            args.expected_failure_marker,
        )
    except (OSError, ValueError, RecursionError) as exc:
        print(f"iOS native test evidence rejected: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "verified": True,
                "mode": "negative" if args.expected_failure_marker is not None else "positive",
                "expected_case": EXPECTED_CASE,
                "xcodebuild_exit": args.xcodebuild_exit,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
