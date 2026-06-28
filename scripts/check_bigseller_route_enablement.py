#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

REQUIRED_FILES = (
    "omnidesk_agent/server.py",
    "omnidesk_agent/api/routes/bigseller.py",
    "omnidesk_agent/integrations/bigseller/worker.py",
    "docs/integrations/bigseller.md",
)


def _read(root: Path, rel: str) -> str:
    path = root / rel
    if not path.exists():
        raise FileNotFoundError(rel)
    return path.read_text(encoding="utf-8")


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _string_constants(node: ast.AST) -> set[str]:
    values: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            values.add(child.value)
    return values


def _contains_call(node: ast.AST, name: str) -> bool:
    return any(
        isinstance(child, ast.Call) and _call_name(child.func) == name
        for child in ast.walk(node)
    )


def _require_explicit_register_only(server_text: str, failures: list[str]) -> None:
    try:
        tree = ast.parse(server_text)
    except SyntaxError as exc:
        failures.append(f"server route gate is not valid Python: {exc}")
        return

    boundary_names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    if "_register_optional_bigseller_routes" not in boundary_names:
        failures.append("server missing optional BigSeller route registration boundary")

    matching_gates: list[ast.If] = []
    unsafe_gates: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        body_registers = any(
            isinstance(child, ast.Call)
            and _call_name(child.func) == "_register_optional_bigseller_routes"
            for stmt in node.body
            for child in ast.walk(stmt)
        )
        if not body_registers:
            continue
        condition_strings = _string_constants(node.test)
        if "BIGSELLER_ENABLED" in condition_strings:
            unsafe_gates.append(getattr(node, "lineno", 0))
        if "BIGSELLER_REGISTER_ROUTES" in condition_strings and _contains_call(
            node.test,
            "_env_flag",
        ):
            matching_gates.append(node)

    if unsafe_gates:
        failures.append(
            "server route gate must not register routes from BIGSELLER_ENABLED "
            f"(lines: {', '.join(str(line) for line in unsafe_gates)})"
        )
    if not matching_gates:
        failures.append(
            "server must register BigSeller routes only behind "
            "_env_flag('BIGSELLER_REGISTER_ROUTES')"
        )


def _collect_advisory_warnings(root: Path) -> list[str]:
    warnings: list[str] = []
    advisory_snippets = {
        "omnidesk_agent/api/routes/bigseller.py": (
            "/errors/{error_id}/retry",
            "/errors/{error_id}/resolve",
        ),
        "omnidesk_agent/integrations/bigseller/worker.py": (
            "def list_errors",
            "def retry_error",
            "def resolve_error",
        ),
        "docs/integrations/bigseller.md": (
            "BIGSELLER_ENABLED does not register routes",
            "Routes are registered only when `BIGSELLER_REGISTER_ROUTES=true`",
        ),
    }
    for rel, snippets in advisory_snippets.items():
        try:
            text = _read(root, rel)
        except FileNotFoundError:
            continue
        for snippet in snippets:
            if snippet not in text:
                warnings.append(f"advisory: {rel} missing snippet: {snippet}")
    return warnings


def audit(root: Path) -> dict[str, object]:
    failures: list[str] = []
    for rel in REQUIRED_FILES:
        if not (root / rel).exists():
            failures.append(f"missing required file: {rel}")
    try:
        _require_explicit_register_only(_read(root, "omnidesk_agent/server.py"), failures)
    except FileNotFoundError:
        pass
    warnings = _collect_advisory_warnings(root)
    return {
        "schema": "omnidesk-bigseller-route-enablement/v6",
        "status": "passed" if not failures else "failed",
        "failures": failures,
        "warnings": warnings,
        "boundary": (
            "This gate blocks unsafe BigSeller route enablement semantics only: "
            "routes must be registered through the optional registration boundary "
            "and only behind BIGSELLER_REGISTER_ROUTES. BigSeller connector feature "
            "completeness is enforced separately by check_bigseller_connector_contract.py."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate BigSeller route enablement semantics.")
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--write-report", default="")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    report = audit(root)
    if args.write_report:
        out = Path(args.write_report)
        if not out.is_absolute():
            out = root / out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "failure_count": len(report["failures"])}, ensure_ascii=False, sort_keys=True))
    for warning in report.get("warnings", []):
        print(f"WARNING {warning}", file=sys.stderr)
    for failure in report["failures"]:
        print(f"BLOCKER {failure}", file=sys.stderr)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
