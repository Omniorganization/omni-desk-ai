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
STREAM_CONTRACT_KEY = ("POST", "/api/chat/stream")
STREAM_TEST_FILES = {
    "web_admin": {
        "source": "apps/web-admin-next/lib/api.ts",
        "source_markers": ("streamChat(", "/api/omni/chat/stream"),
        "test": "apps/web-admin-next/tests/streaming.test.ts",
        "test_markers": ("streamChat", "last-event-id", "chat.completed"),
    },
    "desktop": {
        "source": "apps/desktop-tauri/src/api.ts",
        "source_markers": ("streamChat(", "/api/chat/stream"),
        "test": "apps/desktop-tauri/tests/streaming.test.ts",
        "test_markers": ("streamChat", "last-event-id", "chat.completed"),
    },
    "mobile": {
        "source": "apps/mobile-flutter/lib/omni_api.dart",
        "source_markers": ("streamChat(", "/api/chat/stream"),
        "test": "apps/mobile-flutter/test/omni_streaming_test.dart",
        "test_markers": ("streamChat", "last-event-id", "chat.completed"),
    },
}
SUCCESS_MESSAGE = "typed client contract tests verified from contract client_surfaces"
FIELD_RE = re.compile(
    r"\b(?P<field>method|contractPath|signedInProduction)\s*:\s*"
    r"(?P<value>true|false|'[^']*'|\"[^\"]*\")"
)


def _load_contract(root: Path) -> tuple[set[str], list[dict[str, Any]]]:
    contract = json.loads(
        (root / "apps/shared/omni-app-api.contract.json").read_text(encoding="utf-8")
    )
    surfaces = contract.get("surfaces") or []
    endpoints = contract.get("endpoints") or []
    if not isinstance(surfaces, list) or not all(
        isinstance(item, str) for item in surfaces
    ):
        raise ValueError("contract surfaces must be a list of strings")
    if not isinstance(endpoints, list) or not all(
        isinstance(item, dict) for item in endpoints
    ):
        raise ValueError("contract endpoints must be a list of objects")
    return set(surfaces), endpoints


def _case_blocks(text: str) -> list[str]:
    blocks = [
        line
        for line in text.splitlines()
        if "contractPath:" in line and "method:" in line
    ]
    lines = text.splitlines()
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


def _extract_typed_cases(text: str) -> dict[tuple[str, str], dict[str, str | bool]]:
    cases: dict[tuple[str, str], dict[str, str | bool]] = {}
    for block in _case_blocks(text):
        fields: dict[str, str | bool] = {}
        for match in FIELD_RE.finditer(block):
            raw = match.group("value")
            value: str | bool
            if raw == "true":
                value = True
            elif raw == "false":
                value = False
            else:
                value = raw[1:-1]
            fields[match.group("field")] = value
        method = fields.get("method")
        path = fields.get("contractPath")
        if isinstance(method, str) and isinstance(path, str):
            cases[(method, path)] = fields
    return cases


def _endpoint_key(endpoint: dict[str, Any]) -> tuple[str, str] | None:
    method = endpoint.get("method")
    path = endpoint.get("path")
    if isinstance(method, str) and isinstance(path, str):
        return method, path
    return None


def _string_list(value: Any) -> list[str] | None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return value


def _marker_issues(
    root: Path,
    *,
    path: str,
    markers: tuple[str, ...],
    label: str,
) -> list[str]:
    target = root / path
    if not target.exists():
        return [f"typed client contract: missing {label} file {path}"]
    text = target.read_text(encoding="utf-8")
    return [
        f"typed client contract: {label} file {path} is missing marker {marker}"
        for marker in markers
        if marker not in text
    ]


def _stream_issues(
    root: Path,
    declared_surfaces: set[str],
    stream_surfaces: set[str],
) -> list[str]:
    issues: list[str] = []
    expected = set(STREAM_TEST_FILES)
    if stream_surfaces != expected:
        issues.append(
            "typed client contract: POST /api/chat/stream must declare exactly "
            f"{sorted(expected)} client surfaces, got {sorted(stream_surfaces)}"
        )
    unknown = expected - declared_surfaces
    if unknown:
        issues.append(
            f"typed client contract: streaming config has unknown surfaces {sorted(unknown)}"
        )
    for surface, spec in STREAM_TEST_FILES.items():
        issues.extend(
            _marker_issues(
                root,
                path=spec["source"],
                markers=spec["source_markers"],
                label=f"{surface} streaming source",
            )
        )
        issues.extend(
            _marker_issues(
                root,
                path=spec["test"],
                markers=spec["test_markers"],
                label=f"{surface} streaming test",
            )
        )
    return issues


def _check_contract_coverage(root: Path) -> list[str]:
    issues: list[str] = []
    declared_surfaces, endpoints = _load_contract(root)
    unknown_test_surfaces = set(TYPED_TEST_FILES) - declared_surfaces
    if unknown_test_surfaces:
        issues.append(
            f"typed client contract: unknown test surfaces {sorted(unknown_test_surfaces)}"
        )

    typed_cases: dict[str, dict[tuple[str, str], dict[str, str | bool]]] = {}
    for surface, spec in TYPED_TEST_FILES.items():
        path = root / spec["path"]
        if not path.exists():
            issues.append(f"typed client contract: missing {spec['path']}")
            continue
        text = path.read_text(encoding="utf-8")
        if spec["marker"] not in text:
            issues.append(
                f"typed client contract: missing marker {spec['marker']} in {spec['path']}"
            )
        typed_cases[surface] = _extract_typed_cases(text)

    required = {surface: set() for surface in TYPED_TEST_FILES}
    signed = {surface: set() for surface in TYPED_TEST_FILES}
    stream_surfaces: set[str] | None = None
    for endpoint in endpoints:
        key = _endpoint_key(endpoint)
        if key is None:
            issues.append(f"typed client contract: invalid endpoint {endpoint}")
            continue
        client_surfaces = _string_list(endpoint.get("client_surfaces"))
        if client_surfaces is None:
            issues.append(
                f"typed client contract: {key[0]} {key[1]} must declare client_surfaces list"
            )
            continue
        unknown = set(client_surfaces) - declared_surfaces
        if unknown:
            issues.append(
                f"typed client contract: {key[0]} {key[1]} has unknown client_surfaces {sorted(unknown)}"
            )
        signed_surfaces = _string_list(
            endpoint.get("signed_device_required_in_production") or []
        )
        if signed_surfaces is None:
            issues.append(
                f"typed client contract: {key[0]} {key[1]} signed surfaces must be a list"
            )
            signed_surfaces = []
        unknown_signed = set(signed_surfaces) - declared_surfaces
        if unknown_signed:
            issues.append(
                f"typed client contract: {key[0]} {key[1]} has unknown "
                f"signed surfaces {sorted(unknown_signed)}"
            )
        if key == STREAM_CONTRACT_KEY:
            stream_surfaces = set(client_surfaces)
            continue
        for surface in client_surfaces:
            if surface in required:
                required[surface].add(key)
                if surface in signed_surfaces:
                    signed[surface].add(key)

    if stream_surfaces is None:
        issues.append("typed client contract: missing POST /api/chat/stream contract")
    else:
        issues.extend(_stream_issues(root, declared_surfaces, stream_surfaces))

    for surface, required_cases in required.items():
        cases = typed_cases.get(surface, {})
        for method, path in sorted(required_cases):
            if (method, path) not in cases:
                issues.append(
                    f"typed client contract: {surface} test does not cover "
                    f"contract-declared {method} {path}"
                )
        for method, path in sorted(signed[surface]):
            if cases.get((method, path), {}).get("signedInProduction") is not True:
                issues.append(
                    f"typed client contract: {surface} must assert signed production "
                    f"route {method} {path}"
                )
        for method, path in sorted(cases):
            if (method, path) not in required_cases:
                issues.append(
                    f"typed client contract: {surface} has typed test for route not "
                    f"declared in client_surfaces: {method} {path}"
                )
    return issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify typed clients cover contract-declared JSON routes and dedicated "
            "SSE tests cover the streaming contract."
        )
    )
    parser.add_argument("root", nargs="?", default=".")
    args = parser.parse_args(argv)
    issues = _check_contract_coverage(Path(args.root).resolve())
    if issues:
        for issue in issues:
            print(issue, file=sys.stderr)
        return 1
    print(SUCCESS_MESSAGE)
    print("dedicated Web, Desktop, and Mobile streaming clients verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
