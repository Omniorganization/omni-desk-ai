#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

WEB_ADMIN_ROUTE_MAP = {
    "/app/bootstrap": "bootstrap/route.ts",
    "/app/projects": "projects/route.ts",
    "/app/projects/{project_id}": "projects/[projectId]/route.ts",
    "/app/devices/register": "devices/register/route.ts",
    "/app/conversations": "conversations/route.ts",
    "/app/conversations/{conversation_id}/messages": "conversations/[id]/messages/route.ts",
    "/app/conversations/{conversation_id}/ask": "conversations/[id]/ask/route.ts",
    "/app/approvals": "approvals/route.ts",
    "/app/approvals/{approval_id}/decide": "approvals/[id]/decide/route.ts",
    "/app/notifications": "notifications/route.ts",
    "/app/devices/enrollment/start": "devices/enrollment/start/route.ts",
    "/app/devices/enrollment/{enrollment_id}/complete": "devices/enrollment/[enrollmentId]/complete/route.ts",
    "/app/devices/enrollment/{enrollment_id}/challenge": "devices/enrollment/[enrollmentId]/challenge/route.ts",
    "/app/devices/enrollment/{enrollment_id}/verify": "devices/enrollment/[enrollmentId]/verify/route.ts",
}

MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _load_contract(root: Path) -> list[dict[str, Any]]:
    contract_path = root / "apps" / "shared" / "omni-app-api.contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    endpoints = contract.get("endpoints")
    if not isinstance(endpoints, list):
        raise ValueError("apps/shared/omni-app-api.contract.json endpoints must be a list")
    return [endpoint for endpoint in endpoints if isinstance(endpoint, dict)]


def _method_export_present(text: str, method: str) -> bool:
    return f"export async function {method}" in text or f"export function {method}" in text


def _check(root: Path) -> list[str]:
    issues: list[str] = []
    route_root = root / "apps" / "web-admin-next" / "app" / "api" / "omni"
    for endpoint in _load_contract(root):
        method = str(endpoint.get("method") or "")
        path = str(endpoint.get("path") or "")
        surfaces = endpoint.get("client_surfaces") or []
        if "web_admin" not in surfaces:
            continue
        route_rel = WEB_ADMIN_ROUTE_MAP.get(path)
        if route_rel is None:
            issues.append(f"web_admin proxy route map missing for {method} {path}")
            continue
        route_path = route_root / route_rel
        if not route_path.exists():
            issues.append(f"web_admin proxy route file missing for {method} {path}: {route_path.relative_to(root)}")
            continue
        text = route_path.read_text(encoding="utf-8")
        if not _method_export_present(text, method):
            issues.append(f"web_admin proxy route {route_path.relative_to(root)} does not export {method} for {path}")
        expected_proxy = path.split("/{", 1)[0]
        if "omniProxy(" not in text or expected_proxy not in text:
            issues.append(f"web_admin proxy route {route_path.relative_to(root)} does not proxy to {path}")
        if method in MUTATING_METHODS and "assertCsrf()" not in text:
            issues.append(f"web_admin mutating route {method} {path} must call assertCsrf()")
        signed = endpoint.get("signed_device_required_in_production") or []
        if "web_admin" in signed and "deviceSignatureHeaders" not in text:
            issues.append(f"web_admin signed production route {method} {path} must forward deviceSignatureHeaders(request)")
    return issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify Web Admin /api/omni proxy routes cover shared contract web_admin surfaces.")
    parser.add_argument("root", nargs="?", default=".")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    issues = _check(root)
    if issues:
        for issue in issues:
            print(issue, file=sys.stderr)
        return 1
    print("web admin proxy contract verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
