#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

OK_STATUSES = {"ok", "passed", "success", "succeeded", "verified"}
PLACEHOLDER_RE = re.compile(r"\b(todo|tbd|placeholder|example|mock|fake|sample)\b", re.IGNORECASE)

REQUIRED_FIELDS = (
    "status",
    "produced_at",
    "producer",
    "environment",
    "backend_base_url",
    "scenario_id",
    "model_request_id",
    "trace_id",
    "audit_event_id",
    "cost_ledger_entry_id",
)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _status_ok(value: Any) -> bool:
    return str(value or "").strip().lower() in OK_STATUSES


def _bool_true(value: Any) -> bool:
    return value is True or str(value).strip().lower() in {"true", "yes", "1", "verified", "passed", "ok", "success"}


def _contains_placeholder(value: Any) -> bool:
    if isinstance(value, str):
        return bool(PLACEHOLDER_RE.search(value))
    if isinstance(value, list):
        return any(_contains_placeholder(item) for item in value)
    if isinstance(value, dict):
        return any(_contains_placeholder(item) for item in value.values())
    return False


def audit(path: Path) -> dict[str, Any]:
    failures: list[str] = []
    doc: dict[str, Any] = {}
    if not path.exists():
        failures.append(f"missing model live smoke evidence: {path}")
    else:
        try:
            doc = _read_json(path)
        except Exception as exc:
            failures.append(f"invalid json: {exc}")

    if doc:
        for field in REQUIRED_FIELDS:
            if not str(doc.get(field) or "").strip():
                failures.append(f"{field} is required")
        if not _status_ok(doc.get("status")):
            failures.append("status must be passed/succeeded/verified")
        if _contains_placeholder(doc):
            failures.append("placeholder/mock/example values are not accepted as live smoke evidence")
        if str(doc.get("environment", "")).strip().lower() not in {"staging", "production", "prod"}:
            failures.append("environment must be staging or production")
        if not _bool_true(doc.get("response_non_empty")):
            failures.append("response_non_empty must be true")
        if not _bool_true(doc.get("audit_logged")):
            failures.append("audit_logged must be true")
        if not _bool_true(doc.get("cost_ledger_recorded")):
            failures.append("cost_ledger_recorded must be true")
        if not _bool_true(doc.get("budget_enforced")):
            failures.append("budget_enforced must be true")
        if not _bool_true(doc.get("approval_required_on_budget_exceeded")):
            failures.append("approval_required_on_budget_exceeded must be true")
        try:
            p95 = float(doc.get("p95_latency_ms"))
            if p95 <= 0 or p95 > 15000:
                failures.append("p95_latency_ms must be > 0 and <= 15000")
        except Exception:
            failures.append("p95_latency_ms must be numeric")
        try:
            error_rate = float(doc.get("error_rate"))
            if error_rate < 0 or error_rate > 0.01:
                failures.append("error_rate must be between 0 and 0.01")
        except Exception:
            failures.append("error_rate must be numeric")

    return {
        "schema": "omnidesk-model-live-smoke/v1",
        "status": "passed" if not failures else "blocked",
        "evidence_file": str(path),
        "failures": failures,
        "boundary": "This validates live model smoke evidence. It does not call a model by itself and does not replace signed artifacts, push, soak, rollback, backup/restore, or failure-injection evidence.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate live model Q&A smoke evidence.")
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--evidence-file", default="release/external-evidence/model/model-live-smoke.json")
    parser.add_argument("--audit-only", action="store_true")
    parser.add_argument("--write-report", default="")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    evidence = Path(args.evidence_file)
    if not evidence.is_absolute():
        evidence = root / evidence
    report = audit(evidence)

    if args.write_report:
        out = Path(args.write_report)
        if not out.is_absolute():
            out = root / out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    print(json.dumps({"status": report["status"], "failure_count": len(report["failures"])}, ensure_ascii=False, sort_keys=True))
    for failure in report["failures"]:
        print(f"BLOCKER {failure}", file=sys.stderr)
    return 0 if args.audit_only or report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
