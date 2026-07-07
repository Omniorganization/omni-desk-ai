#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REQUIRED_FILES = [
    "docs/REAL_EXTERNAL_EVIDENCE_COMPLETION_GUIDE_1.12.7.md",
    "deploy/docker/docker-compose.observability.override.yml",
    "scripts/check_remaining_real_evidence_source_fixes.py",
    ".github/workflows/remaining-real-evidence-source-fixes.yml",
]


def _read(path: Path, issues: list[str]) -> str:
    if not path.exists():
        issues.append(f"missing required source fix asset: {path}")
        return ""
    return path.read_text(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify source-side fixes that support remaining Real GA evidence collection.")
    parser.add_argument("root", nargs="?", default=".")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    issues: list[str] = []

    for rel in REQUIRED_FILES:
        _read(root / rel, issues)

    branch_protection = json.loads((root / ".github" / "branch-protection.required.json").read_text(encoding="utf-8"))
    status_checks = set(branch_protection.get("required_status_checks") or [])
    required_jobs = set(branch_protection.get("required_jobs") or [])
    expected_checks = {
        "P0 P1 P2 Feasible Closure": "feasible-closure",
        "Remaining Real GA Source Fixes": "remaining-real-evidence-source-fixes",
    }
    for check_name, job_name in expected_checks.items():
        if check_name not in status_checks:
            issues.append(f"branch protection must require {check_name}")
        if job_name not in required_jobs:
            issues.append(f"branch protection must require {job_name} job")

    override_text = _read(root / "deploy" / "docker" / "docker-compose.observability.override.yml", issues)
    for term in ("OMNIDESK_OTLP_ENDPOINT", "otel-collector:4318/v1/traces", "omnidesk-observability"):
        if term not in override_text:
            issues.append(f"observability override missing {term}")

    guide = _read(root / "docs" / "REAL_EXTERNAL_EVIDENCE_COMPLETION_GUIDE_1.12.7.md", issues)
    for term in (
        "python scripts/check_real_ga_complete.py . --write-report",
        "Native builds",
        "Signed artifacts",
        "Team governance",
        "Native signed artifact binding",
        "Push delivery",
        "Operations drills",
        "customer-distribution Real GA",
    ):
        if term not in guide:
            issues.append(f"real evidence guide missing section or command: {term}")

    workflow = root / ".github" / "workflows" / "remaining-real-evidence-source-fixes.yml"
    workflow_text = _read(workflow, issues)
    if "check_remaining_real_evidence_source_fixes.py" not in workflow_text:
        issues.append("remaining real evidence source fixes workflow must run this gate")
    if "check_real_ga_complete.py" not in workflow_text or "--audit-only" not in workflow_text:
        issues.append("workflow must write a candidate complete Real GA evidence audit without claiming Real GA")

    if issues:
        for issue in issues:
            print(issue, file=sys.stderr)
        return 1
    print("remaining Real GA source-side evidence fixes verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
