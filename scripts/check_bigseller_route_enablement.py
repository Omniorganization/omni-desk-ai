#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REQUIRED_FILES = (
    "omnidesk_agent/server.py",
    "omnidesk_agent/api/routes/bigseller.py",
    "omnidesk_agent/integrations/bigseller/worker.py",
    "docs/integrations/bigseller.md",
)

ROUTE_SNIPPETS = (
    "/errors",
    "/errors/{error_id}/retry",
    "/errors/{error_id}/resolve",
    "authorize(request, \"operator\")",
)

WORKER_SNIPPETS = (
    "def list_errors",
    "def retry_error",
    "def resolve_error",
    "BigSellerOrderSyncService.action_type",
    "BigSellerInventorySyncService.action_type",
    "BigSellerFulfillmentSyncService.action_type",
)

DOC_SNIPPETS = (
    "BIGSELLER_REGISTER_ROUTES",
    "GET /integrations/bigseller/errors",
    "POST /integrations/bigseller/errors/{error_id}/retry",
    "POST /integrations/bigseller/errors/{error_id}/resolve",
)


def _read(root: Path, rel: str) -> str:
    path = root / rel
    if not path.exists():
        raise FileNotFoundError(rel)
    return path.read_text(encoding="utf-8")


def _require(snippets: tuple[str, ...], text: str, label: str, failures: list[str]) -> None:
    for snippet in snippets:
        if snippet not in text:
            failures.append(f"{label} missing snippet: {snippet}")


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[`*_]+", "", text)).strip()


def _require_explicit_register_only(server_text: str, failures: list[str]) -> None:
    if "_register_optional_bigseller_routes" not in server_text:
        failures.append("server missing optional BigSeller route registration boundary")
    if "BIGSELLER_REGISTER_ROUTES" not in server_text:
        failures.append("server missing BIGSELLER_REGISTER_ROUTES gate")
    forbidden_patterns = (
        'BIGSELLER_ENABLED") or _env_flag("BIGSELLER_REGISTER_ROUTES',
        'BIGSELLER_ENABLED")or_env_flag("BIGSELLER_REGISTER_ROUTES',
        'BIGSELLER_ENABLED") or os.getenv("BIGSELLER_REGISTER_ROUTES',
    )
    compact = re.sub(r"\s+", "", server_text)
    if any(pattern.replace(" ", "") in compact for pattern in forbidden_patterns):
        failures.append("server route gate must not register routes from BIGSELLER_ENABLED")


def _require_doc_semantics(doc_text: str, failures: list[str]) -> None:
    normalized = _normalize(doc_text)
    if "BIGSELLER_ENABLED does not register routes" not in normalized:
        failures.append("BigSeller docs must state BIGSELLER_ENABLED does not register routes")
    if "Routes are registered only when BIGSELLER_REGISTER_ROUTES=true" not in normalized:
        failures.append("BigSeller docs must state routes require BIGSELLER_REGISTER_ROUTES=true")


def audit(root: Path) -> dict[str, object]:
    failures: list[str] = []
    for rel in REQUIRED_FILES:
        if not (root / rel).exists():
            failures.append(f"missing required file: {rel}")
    try:
        _require_explicit_register_only(_read(root, "omnidesk_agent/server.py"), failures)
    except FileNotFoundError:
        pass
    try:
        _require(ROUTE_SNIPPETS, _read(root, "omnidesk_agent/api/routes/bigseller.py"), "BigSeller admin route", failures)
    except FileNotFoundError:
        pass
    try:
        _require(WORKER_SNIPPETS, _read(root, "omnidesk_agent/integrations/bigseller/worker.py"), "BigSeller worker ops", failures)
    except FileNotFoundError:
        pass
    try:
        doc_text = _read(root, "docs/integrations/bigseller.md")
        _require(DOC_SNIPPETS, doc_text, "BigSeller docs", failures)
        _require_doc_semantics(doc_text, failures)
    except FileNotFoundError:
        pass
    return {
        "schema": "omnidesk-bigseller-route-enablement/v4",
        "status": "passed" if not failures else "failed",
        "failures": failures,
        "boundary": "This gate verifies explicit route enablement and Admin error operations. It does not validate external BigSeller traffic.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate BigSeller route enablement and ops surface.")
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
    for failure in report["failures"]:
        print(f"BLOCKER {failure}", file=sys.stderr)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
