#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REQUIRED_SURFACE_ROUTES: dict[str, tuple[tuple[str, str], ...]] = {
    "web_admin": (
        ("GET", "/app/bootstrap"),
        ("POST", "/app/devices/register"),
        ("GET", "/app/conversations"),
        ("POST", "/app/conversations"),
        ("GET", "/app/conversations/{conversation_id}/messages"),
        ("POST", "/app/conversations/{conversation_id}/ask"),
        ("GET", "/app/approvals"),
        ("POST", "/app/approvals/{approval_id}/decide"),
        ("GET", "/app/notifications"),
        ("POST", "/app/devices/enrollment/start"),
        ("POST", "/app/devices/enrollment/{enrollment_id}/complete"),
        ("POST", "/app/devices/enrollment/{enrollment_id}/challenge"),
        ("POST", "/app/devices/enrollment/{enrollment_id}/verify"),
    ),
    "desktop": (
        ("GET", "/app/bootstrap"),
        ("POST", "/app/devices/register"),
        ("POST", "/app/conversations"),
        ("GET", "/app/conversations/{conversation_id}/messages"),
        ("POST", "/app/conversations/{conversation_id}/ask"),
        ("POST", "/app/runtime/desktop/heartbeat"),
        ("POST", "/app/runtime/desktop/claim"),
        ("POST", "/app/tasks/{task_id}/status"),
        ("GET", "/app/sync"),
        ("POST", "/app/devices/{device_id}/push-token"),
        ("POST", "/app/devices/enrollment/{enrollment_id}/complete"),
    ),
    "mobile": (
        ("GET", "/app/bootstrap"),
        ("POST", "/app/devices/register"),
        ("POST", "/app/conversations"),
        ("GET", "/app/conversations/{conversation_id}/messages"),
        ("POST", "/app/conversations/{conversation_id}/messages"),
        ("POST", "/app/conversations/{conversation_id}/ask"),
        ("POST", "/app/approvals/{approval_id}/decide"),
        ("POST", "/app/devices/{device_id}/push-token"),
        ("GET", "/app/notifications"),
    ),
}

TYPED_TEST_FILES = {
    "web_admin": {
        "path": "apps/web-admin-next/tests/api.test.ts",
        "marker": "WEB_ADMIN_TYPED_CLIENT_CONTRACT_CASES",
    },
    "desktop": {
        "path": "apps/desktop-tauri/tests/api.test.ts",
        "marker": "DESKTOP_TYPED_CLIENT_CONTRACT_CASES",
    },
    "mobile": {
        "path": "apps/mobile-flutter/test/omni_api_test.dart",
        "marker": "mobileTypedClientContractCases",
    },
}

FIELD_RE = re.compile(
    r"\b(?P<field>method|contractPath|signedInProduction)\s*:\s*(?P<value>true|false|'[^']*'|\"[^\"]*\")"
)


def _load_contract(root: Path) -> dict[tuple[str, str], dict]:
    contract = json.loads((root / "apps/shared/omni-app-api.contract.json").read_text(encoding="utf-8"))
    return {(item["method"], item["path"]): item for item in contract.get("endpoints", [])}


def _case_blocks(text: str) -> list[str]:
    blocks: list[str] = []
    lines = text.splitlines()
    for line in lines:
        if "contractPath:" in line and "method:" in line:
            blocks.append(line)

    index = 0
    while index < len(lines):
        if "TypedClientContractCase(" not in lines[index]:
            index += 1
            continue
        block = [lines[index]]
        index += 1
        while index < len(lines):
            block.append(lines[index])
            if lines[index].strip() in {"),", ");"}:
                break
            index += 1
        blocks.append("\n".join(block))
        index += 1
    return blocks


def _parse_case_fields(block: str) -> dict[str, str | bool]:
    fields: dict[str, str | bool] = {}
    for match in FIELD_RE.finditer(block):
        raw_value = match.group("value")
        if raw_value == "true":
            value: str | bool = True
        elif raw_value == "false":
            value = False
        else:
            value = raw_value[1:-1]
        fields[match.group("field")] = value
    return fields


def _extract_typed_cases(text: str) -> dict[tuple[str, str], dict[str, str | bool]]:
    cases: dict[tuple[str, str], dict[str, str | bool]] = {}
    for block in _case_blocks(text):
        fields = _parse_case_fields(block)
        method = fields.get("method")
        contract_path = fields.get("contractPath")
        if not isinstance(method, str) or not isinstance(contract_path, str):
            continue
        cases[(method, contract_path)] = fields
    return cases


def _check_contract_coverage(root: Path) -> list[str]:
    issues: list[str] = []
    contract = _load_contract(root)
    for surface, routes in REQUIRED_SURFACE_ROUTES.items():
        test_spec = TYPED_TEST_FILES[surface]
        test_path = root / test_spec["path"]
        if not test_path.exists():
            issues.append(f"typed client contract: missing {test_spec['path']}")
            continue
        text = test_path.read_text(encoding="utf-8")
        if test_spec["marker"] not in text:
            issues.append(f"typed client contract: missing marker {test_spec['marker']} in {test_spec['path']}")
        typed_cases = _extract_typed_cases(text)
        for method, path in routes:
            if (method, path) not in contract:
                issues.append(f"typed client contract: shared contract missing {method} {path}")
            if (method, path) not in typed_cases:
                issues.append(f"typed client contract: {surface} test does not cover {method} {path}")
        for (method, path), entry in contract.items():
            signed_surfaces = set(entry.get("signed_device_required_in_production") or [])
            if surface in signed_surfaces and (method, path) in routes:
                if typed_cases.get((method, path), {}).get("signedInProduction") is not True:
                    issues.append(
                        f"typed client contract: {surface} must assert signed production route {method} {path}"
                    )
    return issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify typed Web/Desktop/Mobile client contract tests cover shared API routes.")
    parser.add_argument("root", nargs="?", default=".")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    issues = _check_contract_coverage(root)
    if issues:
        for issue in issues:
            print(issue, file=sys.stderr)
        return 1
    print("typed client contract tests verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
