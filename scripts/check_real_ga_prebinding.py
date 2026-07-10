#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

try:
    from scripts.check_real_ga_complete import audit as audit_complete
except ModuleNotFoundError:  # Direct execution sets scripts/ as sys.path[0].
    from check_real_ga_complete import audit as audit_complete


def audit(root: Path, evidence_dir: Path) -> dict[str, Any]:
    """Audit every Real GA category except the binding produced by Main Verification itself."""
    report = audit_complete(root, evidence_dir)
    categories = dict(report.get("categories") or {})
    categories.pop("native_signed_artifact_bindings", None)
    blocker_count = sum(1 for category in categories.values() if not category.get("ok"))
    report["schema"] = "omnidesk-real-ga-prebinding-audit/v1"
    report["categories"] = categories
    report["blocker_count"] = blocker_count
    report["status"] = "passed" if blocker_count == 0 else "blocked_missing_external_evidence"
    report["policy"] = (
        "Main Verification may emit a passed native/signed binding only after every independent "
        "Real GA evidence and live governance category has passed semantic validation."
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the complete Real GA evidence set before Main Verification binding.")
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--evidence-dir", default="release/external-evidence")
    parser.add_argument("--audit-only", action="store_true")
    parser.add_argument("--write-report", default="")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    evidence_dir = Path(args.evidence_dir)
    if not evidence_dir.is_absolute():
        evidence_dir = root / evidence_dir
    report = audit(root, evidence_dir)
    if args.write_report:
        out = Path(args.write_report)
        if not out.is_absolute():
            out = root / out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    if report["status"] != "passed":
        for category_name, category in report.get("categories", {}).items():
            if not category.get("ok"):
                print(f"BLOCKER {category_name}", file=sys.stderr)
    return 0 if args.audit_only or report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
