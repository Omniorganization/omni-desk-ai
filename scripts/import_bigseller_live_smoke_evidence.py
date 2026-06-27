#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any


SECRET_RE = re.compile(
    r"(?i)(access[_-]?token|refresh[_-]?token|app[_-]?key|authorization|cookie|secret|password|bearer\s+[a-z0-9._=-]+)"
)
PLACEHOLDER_RE = re.compile(r"\b(todo|tbd|placeholder|example|mock|fake|sample)\b", re.IGNORECASE)
REQUIRED_TRUE_FIELDS = (
    "auth_success",
    "order_list_success",
    "inventory_list_success",
    "webhook_signature_verified",
    "webhook_replay_guard_verified",
    "secret_leakage_checked",
    "no_secret_leakage",
)
REQUIRED_TEXT_FIELDS = (
    "schema",
    "status",
    "produced_at",
    "producer",
    "environment",
    "store_id",
    "trace_id",
    "audit_event_id",
)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _contains_bad_text(value: Any) -> bool:
    if isinstance(value, str):
        return bool(SECRET_RE.search(value) or PLACEHOLDER_RE.search(value))
    if isinstance(value, list):
        return any(_contains_bad_text(item) for item in value)
    if isinstance(value, dict):
        return any(_contains_bad_text(item) for item in value.values())
    return False


def _bool_true(value: Any) -> bool:
    return value is True or str(value).strip().lower() in {"true", "yes", "1", "ok", "passed", "verified"}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate(doc: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    if doc.get("schema") != "omnidesk-bigseller-live-smoke/v1":
        issues.append("schema must be omnidesk-bigseller-live-smoke/v1")
    if str(doc.get("status") or "").strip().lower() not in {"passed", "verified", "success", "succeeded", "ok"}:
        issues.append("status must be passed/verified")
    for field in REQUIRED_TEXT_FIELDS:
        if not str(doc.get(field) or "").strip():
            issues.append(f"{field} is required")
    if str(doc.get("environment") or "").strip().lower() not in {"staging", "production", "prod"}:
        issues.append("environment must be staging or production")
    for field in REQUIRED_TRUE_FIELDS:
        if not _bool_true(doc.get(field)):
            issues.append(f"{field} must be true")
    try:
        latency = float(doc.get("p95_latency_ms"))
        if latency <= 0 or latency > 10000:
            issues.append("p95_latency_ms must be > 0 and <= 10000")
    except Exception:
        issues.append("p95_latency_ms must be numeric")
    try:
        error_rate = float(doc.get("error_rate"))
        if error_rate < 0 or error_rate > 0.01:
            issues.append("error_rate must be between 0 and 0.01")
    except Exception:
        issues.append("error_rate must be numeric")
    if _contains_bad_text(doc):
        issues.append("evidence must not contain secrets, raw tokens, placeholders, mock, fake, sample, or example values")
    return issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate and import real BigSeller live smoke evidence.")
    parser.add_argument("input_json", help="Path to operator/workflow produced live smoke JSON")
    parser.add_argument("--root", default=".")
    parser.add_argument("--output", default="release/external-evidence/integrations/bigseller-live-smoke.json")
    parser.add_argument("--write-manifest", default="")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    input_path = Path(args.input_json).resolve()
    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = root / output_path

    doc = _read_json(input_path)
    issues = validate(doc)
    if issues:
        print("BigSeller live smoke evidence import rejected:", file=sys.stderr)
        for issue in issues:
            print(f"  {issue}", file=sys.stderr)
        return 1

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    manifest = {
        "schema": "omnidesk-bigseller-live-smoke-import/v1",
        "status": "passed",
        "source": str(input_path),
        "output": str(output_path.relative_to(root) if output_path.is_relative_to(root) else output_path),
        "sha256": _sha256(output_path),
        "policy": "Importer validates schema, status, required live checks, latency/error bounds, and rejects secrets/placeholders/mock/sample evidence.",
    }
    if args.write_manifest:
        manifest_path = Path(args.write_manifest)
        if not manifest_path.is_absolute():
            manifest_path = root / manifest_path
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
