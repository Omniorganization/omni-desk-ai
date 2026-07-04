#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

METHOD_RE = re.compile(r'@app\.(get|post|put|patch|websocket)\("([^"]+)"\)')
PATH_LITERAL_RE = re.compile(r"['\"`](/(?:app|api/chat)[^'\"`]*)['\"`]")
OMNI_PROXY_RE = re.compile(r"omniProxy\(['\"`]([^'\"`]+)['\"`]")

CLIENT_REQUIRED_PATHS = {
    "/app/bootstrap",
    "/app/devices/register",
    "/app/conversations",
    "/app/conversations/{conversation_id}/messages",
    "/app/conversations/{conversation_id}/ask",
    "/app/approvals/{approval_id}/decide",
    "/app/notifications",
    "/app/runtime/desktop/heartbeat",
    "/app/runtime/desktop/claim",
    "/app/sync",
    "/app/devices/{device_id}/push-token",
}

SUBSTITUTIONS = {
    "${encodeURIComponent(conversationId)}": "{conversation_id}",
    "${_pathSegment(conversationId)}": "{conversation_id}",
    "${conversationId}": "{conversation_id}",
    "${encodeURIComponent(approvalId)}": "{approval_id}",
    "${_pathSegment(approvalId)}": "{approval_id}",
    "${approvalId}": "{approval_id}",
    "${encodeURIComponent(deviceId)}": "{device_id}",
    "${_pathSegment(deviceId)}": "{device_id}",
    "${deviceId}": "{device_id}",
    "${taskId}": "{task_id}",
    "${encodeURIComponent(enrollmentId)}": "{enrollment_id}",
    "${enrollmentId}": "{enrollment_id}",
}


def _normalize_path(value: str) -> str:
    path = value.split("?", 1)[0]
    for needle, replacement in SUBSTITUTIONS.items():
        path = path.replace(needle, replacement)
    return path


def _contract_paths(root: Path) -> set[tuple[str, str]]:
    contract = json.loads(
        (root / "apps" / "shared" / "omni-app-api.contract.json").read_text(
            encoding="utf-8"
        )
    )
    return {(item["method"], item["path"]) for item in contract.get("endpoints", [])}


def _backend_paths(root: Path) -> set[tuple[str, str]]:
    paths: set[tuple[str, str]] = set()
    for rel in ["omnidesk_agent/appsync/routes.py", "omnidesk_agent/server.py"]:
        text = (root / rel).read_text(encoding="utf-8")
        for method, path in METHOD_RE.findall(text):
            normalized_method = "WS" if method == "websocket" else method.upper()
            paths.add((normalized_method, path))
    return paths


def _client_paths(root: Path) -> set[str]:
    bases = [
        root / "apps" / "web-admin-next",
        root / "apps" / "desktop-tauri" / "src",
        root / "apps" / "mobile-flutter" / "lib",
    ]
    paths: set[str] = set()
    for base in bases:
        for suffix in ("*.ts", "*.tsx", "*.dart"):
            for path in sorted(base.rglob(suffix)):
                text = path.read_text(encoding="utf-8")
                for value in PATH_LITERAL_RE.findall(text):
                    paths.add(_normalize_path(value))
                for value in OMNI_PROXY_RE.findall(text):
                    if value.startswith("/app") or value.startswith("/api/chat"):
                        paths.add(_normalize_path(value))
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Diff shared API contract both ways.")
    parser.add_argument("root", nargs="?", default=".")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    contract = _contract_paths(root)
    backend = _backend_paths(root)
    client = _client_paths(root)
    rest_contract = {item for item in contract if item[0] != "WS"}

    issues: list[str] = []
    missing_backend = sorted(contract - backend)
    if missing_backend:
        issues.append(f"contract endpoints missing backend routes: {missing_backend}")

    contract_path_set = {path for _, path in rest_contract}
    client_outside_contract = sorted(path for path in client if path not in contract_path_set)
    if client_outside_contract:
        issues.append(f"client paths missing from shared contract: {client_outside_contract}")

    missing_client_coverage = sorted(CLIENT_REQUIRED_PATHS - client)
    if missing_client_coverage:
        issues.append(f"release-critical contract paths missing client use: {missing_client_coverage}")

    backend_outside_contract = sorted(
        (method, path)
        for method, path in backend
        if path.startswith("/app") or path.startswith("/api/chat")
        if (method, path) not in contract
    )
    if backend_outside_contract:
        issues.append(f"backend app/chat routes missing from contract: {backend_outside_contract}")

    if issues:
        for issue in issues:
            print(issue, file=sys.stderr)
        return 1
    print(
        "contract bidirectional diff ok: "
        f"{len(contract)} contract endpoints, {len(client)} client paths"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
