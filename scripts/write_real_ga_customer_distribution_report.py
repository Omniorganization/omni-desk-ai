#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


OK_STATUS = "passed"


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _project_version(root: Path) -> str:
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        return "unknown"
    return match.group(1)


def _audit_rows(audit: dict[str, Any]) -> list[str]:
    rows = ["| Evidence category | Status | Issues |", "|---|---:|---|"]
    categories = audit.get("categories") or {}
    for name, info in sorted(categories.items()):
        ok = bool(info.get("ok"))
        issues = info.get("issues") or []
        issues_text = "None" if ok else "<br>".join(str(item) for item in issues[:12])
        if len(issues) > 12:
            issues_text += f"<br>... {len(issues) - 12} more"
        rows.append(f"| `{name}` | {'passed' if ok else 'blocked'} | {issues_text} |")
    if not categories:
        rows.append("| external evidence | missing audit report | Run `scripts/check_external_ga_evidence.py` first. |")
    return rows


def _artifact_rows(dist_dir: Path) -> list[str]:
    rows = ["| Artifact | SHA-256 / status |", "|---|---|"]
    metadata = _read_json(dist_dir / "release_metadata.json")
    signatures = _read_json(dist_dir / "release_signatures.json")
    if metadata:
        for key in ("version", "source_commit", "image_ref", "image_digest", "artifact_sha256"):
            if metadata.get(key):
                rows.append(f"| `{key}` | `{metadata[key]}` |")
    if signatures:
        rows.append("| `release_signatures.json` | present |")
    if len(rows) == 2:
        rows.append("| release metadata | not found in dist directory |")
    return rows


def build_report(root: Path, audit_path: Path, output_path: Path, dist_dir: Path) -> str:
    audit = _read_json(audit_path)
    version = _project_version(root)
    status = str(audit.get("status") or "missing_external_evidence_audit")
    blocker_count = audit.get("blocker_count", "unknown")
    generated_at = datetime.now(timezone.utc).isoformat()
    ga_ready = status == OK_STATUS

    body: list[str] = []
    body.append(f"# OmniDesk Customer Distribution GA Report")
    body.append("")
    body.append(f"Generated at: `{generated_at}`")
    body.append(f"Version: `{version}`")
    body.append(f"External evidence audit status: `{status}`")
    body.append(f"Blocker count: `{blocker_count}`")
    body.append("")
    if ga_ready:
        body.append("> Decision: customer-distribution Real GA may be claimed for this artifact set, provided the attached release artifacts match the audit inputs.")
    else:
        body.append("> Decision: do not claim customer-distribution Real GA. The repository may only be described as a source-gated GA candidate until every blocker below is closed by real external evidence.")
    body.append("")
    body.append("## External evidence status")
    body.extend(_audit_rows(audit))
    body.append("")
    body.append("## Release artifact status")
    body.extend(_artifact_rows(dist_dir))
    body.append("")
    body.append("## Required operator evidence")
    body.append("")
    body.append("Real GA requires evidence from native builders, artifact signers, app stores or install targets, model gateway, push providers, staging or production databases, rollback systems, backup/restore systems, and failure-injection drills. Source gates and templates are not evidence.")
    body.append("")
    body.append("## Validation commands")
    body.append("")
    body.append("```bash")
    body.append("python scripts/check_external_ga_evidence.py . --write-report release/real-ga-evidence-audit-1.12.7.json")
    body.append("python scripts/write_real_ga_customer_distribution_report.py . --audit-report release/real-ga-evidence-audit-1.12.7.json --output release/real-ga-customer-distribution-report.md")
    body.append("```")
    body.append("")
    text = "\n".join(body) + "\n"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text, encoding="utf-8")
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write a customer-readable GA distribution report from real external evidence audit output.")
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--audit-report", default="release/real-ga-evidence-audit-1.12.7.json")
    parser.add_argument("--output", default="release/real-ga-customer-distribution-report.md")
    parser.add_argument("--dist-dir", default="dist")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    audit_path = Path(args.audit_report)
    if not audit_path.is_absolute():
        audit_path = root / audit_path
    output = Path(args.output)
    if not output.is_absolute():
        output = root / output
    dist_dir = Path(args.dist_dir)
    if not dist_dir.is_absolute():
        dist_dir = root / dist_dir
    build_report(root, audit_path, output, dist_dir)
    print(f"wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
