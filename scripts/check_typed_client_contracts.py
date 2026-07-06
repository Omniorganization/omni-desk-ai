#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

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


def _load_contract(root: Path) -> tuple[set[str], list[dict[str, Any]]]:
    contract = json.loads((root / "apps/shared/omni-app-api.contract.json").read_text(encoding="utf-8"))
    surfaces = contract.get("surfaces") or []
    endpoints = contract.get("endpoints") or []
    if not isinstance(surfaces, list) or not all(isinstance(item, str) for item in surfaces):
        raise ValueError("apps/shared/omni-app-api.contract.json surfaces must be a list of strings")
    if not isinstance(endpoints, list) or not all(isinstance(item, dict) for item in endpoints):
        raise ValueError("apps/shared/omni-app-api.contract.json endpoints must be a list of objects")
    return set(surfaces), endpoints


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
            if lines[index].strip() in {
                "),",
                ");",
            }:
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


def _endpoint_key(endpoint: dict[str, Any]) -> tuple[str, str] | None:
    method = endpoint.get("method")
    path = endpoint.get("path")
    if not isinstance(method, str) or not isinstance(path, str):
        return None
    return method, path


def _string_list(value: Any) -> list[str] | None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return value


def _check_contract_coverage(root: Path) -> list[str]:
    issues: list[str] = []
    declared_surfaces, endpoints = _load_contract(root)
    unknown_test_surfaces = set(TYPED_TEST_FILES) - declared_surfaces
    if unknown_test_surfaces:
        issues.append(f"typed client contract: test config references unknown surfaces {sorted(unknown_test_surfaces)}")

    typed_cases_by_surface: dict[str, dict[tuple[str, str], dict[str, str | bool]]] = {}
    for surface, test_spec in TYPED_TEST_FILES.items():
        test_path = root / test_spec["path"]
        if not test_path.exists():
            issues.append(f"typed client contract: missing {test_spec['path']}")
            continue
        text = test_path.read_text(encoding="utf-8")
        if test_spec["marker"] not in text:
            issues.append(f"typed client contract: missing marker {test_spec['marker']} in {test_spec['path']}")
        typed_cases_by_surface[surface] = _extract_typed_cases(text)

    contract_required: dict[str, set[tuple[str, str]]] = {surface: set() for surface in TYPED_TEST_FILES}
    signed_required: dict[str, set[tuple[str, str]]] = {surface: set() for surface in TYPED_TEST_FILES}
    for endpoint in endpoints:
        key = _endpoint_key(endpoint)
        if key is None:
            issues.append(f"typed client contract: endpoint must declare string method/path: {endpoint}")
            continue
        client_surfaces = _string_list(endpoint.get("client_surfaces"))
        if client_surfaces is None:
            issues.append(f"typed client contract: {key[0]} {key[1]} must declare client_surfaces list")
            continue
        unknown_client_surfaces = set(client_surfaces) - declared_surfaces
        if unknown_client_surfaces:
            issues.append(f"typed client contract: {key[0]} {key[1]} has unknown client_surfaces {sorted(unknown_client_surfaces)}")
        signed_surfaces = _string_list(endpoint.get("signed_device_required_in_production") or [])
        if signed_surfaces is None:
            issues.append(f"typed client contract: {key[0]} {key[1]} signed_device_required_in_production must be a list when present")
            signed_surfaces = []
        unknown_signed_surfaces = set(signed_surfaces) - declared_surfaces
        if unknown_signed_surfaces:
            issues.append(f"typed client contract: {key[0]} {key[1]} has unknown signed surfaces {sorted(unknown_signed_surfaces)}")
        for surface in client_surfaces:
            if surface in contract_required:
                contract_required[surface].add(key)
                if surface in signed_surfaces:
                    signed_required[surface].add(key)

    for surface, required_cases in contract_required.items():
        typed_cases = typed_cases_by_surface.get(surface, {})
        for method, path in sorted(required_cases):
            if (method, path) not in typed_cases:
                issues.append(f"typed client contract: {surface} test does not cover contract-declared {method} {path}")
        for method, path in sorted(signed_required[surface]):
            if typed_cases.get((method, path), {}).get("signedInProduction") is not True:
                issues.append(f"typed client contract: {surface} must assert signed production route {method} {path}")
        for method, path in sorted(typed_cases):
            if (method, path) not in required_cases:
                issues.append(f"typed client contract: {surface} has typed test for route not declared in client_surfaces: {method} {path}")
    return issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify typed Web/Desktop/Mobile client contract tests cover contract-declared API routes.")
    parser.add_argument("root", nargs="?", default=".")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    issues = _check_contract_coverage(root)
    if issues:
        for issue in issues:
            print(issue, file=sys.stderr)
        return 1
    print("typed client contract tests verified from contract client_surfaces")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
