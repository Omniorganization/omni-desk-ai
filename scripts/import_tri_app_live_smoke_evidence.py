#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import re
import shutil
from pathlib import Path
from typing import Any

OK_STATUSES = {"ok", "passed", "success", "succeeded", "verified"}
PLACEHOLDER_RE = re.compile(r"\b(REPLACE_WITH_|todo|tbd|placeholder|example|mock|fake|sample)\b", re.IGNORECASE)
SCHEMA_VERSION = "tri-app-live-smoke/v1"
REQUIRED_TOP_LEVEL = ["status", "scenario_id", "org_id", "trace_id", "started_at", "finished_at", "latency_ms", "steps"]
REQUIRED_STEPS = [
    "desktop_action_proposed",
    "backend_approval_created",
    "mobile_push_received",
    "mobile_approval_decision_submitted",
    "desktop_action_resumed",
    "audit_event_written",
    "web_admin_audit_visible",
]
TRACE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$")
SENSITIVE_KEY_RE = re.compile(
    r"(token|secret|password|authorization|bearer|private[_-]?key|p8|p12|provisioning|udid|apns|fcm|firebase)",
    re.IGNORECASE,
)
ALLOWED_HASHED_SENSITIVE_KEY_RE = re.compile(r"(sha256|hash|hmac|fingerprint)$", re.IGNORECASE)


def _is_status_signal(value: Any) -> bool:
    return value is None or isinstance(value, (bool, int, float))


def _contains_placeholder(value: Any) -> bool:
    if isinstance(value, str):
        return bool(PLACEHOLDER_RE.search(value))
    if isinstance(value, list):
        return any(_contains_placeholder(item) for item in value)
    if isinstance(value, dict):
        return any(_contains_placeholder(item) for item in value.values())
    return False


def _validate_privacy(value: Any, path: str = "") -> list[str]:
    issues: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if (
                SENSITIVE_KEY_RE.search(str(key))
                and not ALLOWED_HASHED_SENSITIVE_KEY_RE.search(str(key))
                and not _is_status_signal(child)
            ):
                issues.append(f"sensitive raw field is not allowed in tri-app smoke evidence: {child_path}")
            issues.extend(_validate_privacy(child, child_path))
    elif isinstance(value, list):
        for i, child in enumerate(value):
            issues.extend(_validate_privacy(child, f"{path}[{i}]"))
    return issues


def _parse_ts(value: Any, field: str, issues: list[str]) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        issues.append(f"{field} must be an ISO-8601 timestamp")
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _step_ok(value: Any) -> bool:
    if value is True:
        return True
    if isinstance(value, dict):
        return value.get("ok") is True
    return False


def validate_report(doc: dict[str, Any], *, expected_org_id: str | None = None, expected_scenario_id: str | None = None) -> list[str]:
    issues: list[str] = []
    if not isinstance(doc, dict):
        return ["tri-app live smoke report must be a JSON object"]
    for field in REQUIRED_TOP_LEVEL:
        if doc.get(field) in (None, "", []):
            issues.append(f"missing required field: {field}")
    if str(doc.get("status") or "").strip().lower() not in OK_STATUSES:
        issues.append("status must be ok/passed/success/succeeded/verified")
    schema = doc.get("schema_version")
    if schema not in (None, SCHEMA_VERSION):
        issues.append(f"schema_version must be {SCHEMA_VERSION} when present")
    if expected_org_id and doc.get("org_id") != expected_org_id:
        issues.append(f"org_id mismatch: expected {expected_org_id}")
    if expected_scenario_id and doc.get("scenario_id") != expected_scenario_id:
        issues.append(f"scenario_id mismatch: expected {expected_scenario_id}")
    trace_id = str(doc.get("trace_id") or "")
    if not TRACE_ID_RE.fullmatch(trace_id) or _contains_placeholder(trace_id):
        issues.append("trace_id must be a non-placeholder safe id")
    latency = doc.get("latency_ms")
    if not isinstance(latency, int) or latency < 0:
        issues.append("latency_ms must be a non-negative integer")
    started = _parse_ts(doc.get("started_at"), "started_at", issues)
    finished = _parse_ts(doc.get("finished_at"), "finished_at", issues)
    if started and finished:
        if finished <= started:
            issues.append("finished_at must be after started_at")
        elif isinstance(latency, int):
            observed_ms = int((finished - started).total_seconds() * 1000)
            tolerance = max(100, int(observed_ms * 0.1))
            if abs(latency - observed_ms) > tolerance:
                issues.append("latency_ms must be within 10% of finished_at-started_at")
    steps = doc.get("steps")
    if not isinstance(steps, dict):
        issues.append("steps must be an object")
    else:
        for step in REQUIRED_STEPS:
            if not _step_ok(steps.get(step)):
                issues.append(f"steps.{step} must be true")
    if _contains_placeholder(doc):
        issues.append("placeholder/mock/example values are not accepted as real tri-app smoke evidence")
    issues.extend(_validate_privacy(doc))
    return issues


def validate_report_file(path: Path, *, expected_org_id: str | None = None, expected_scenario_id: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"status": "passed", "report_path": str(path), "issues": []}
    if not path.exists():
        result["status"] = "failed"
        result["issues"].append(f"missing tri-app live smoke report: {path}")
        return result
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        issues = validate_report(doc, expected_org_id=expected_org_id, expected_scenario_id=expected_scenario_id)
        result["issues"] = issues
        if issues:
            result["status"] = "failed"
    except Exception as exc:  # noqa: BLE001
        result["status"] = "failed"
        result["issues"].append(f"invalid tri-app live smoke json: {exc}")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate and optionally import tri-app live smoke evidence.")
    parser.add_argument("--report", required=True, help="Tri-app live smoke JSON report produced by a real roundtrip run.")
    parser.add_argument("--dest-dir", default="release/external-evidence/tri-app-live-smoke")
    parser.add_argument("--copy", action="store_true")
    parser.add_argument("--expected-org-id")
    parser.add_argument("--expected-scenario-id")
    parser.add_argument("--write-report", default="release/tri-app-live-smoke-evidence-import-report.json")
    args = parser.parse_args(argv)

    report_path = Path(args.report).resolve()
    result = validate_report_file(report_path, expected_org_id=args.expected_org_id, expected_scenario_id=args.expected_scenario_id)
    if result["status"] == "passed" and args.copy:
        dest_dir = Path(args.dest_dir).resolve()
        dest_dir.mkdir(parents=True, exist_ok=True)
        copied_to = dest_dir / report_path.name
        shutil.copyfile(report_path, copied_to)
        result["copied_to"] = str(copied_to)

    output = Path(args.write_report)
    if not output.is_absolute():
        output = Path.cwd() / output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
