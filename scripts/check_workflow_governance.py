#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REQUIRED_SNIPPETS = [
    "--scope web-admin",
    "--scope desktop",
    "--scope mobile",
    "--scope tri-app",
    "--scope ios-evidence",
    "--scope tri-app-live-smoke",
    "IOS_EVIDENCE_EXPECTED_VERSION",
    "import_ios_real_device_evidence.py",
    "import_tri_app_live_smoke_evidence.py",
    "real-ga-evidence-audit-1.11.8.json",
    "tri-app-live-smoke-evidence-import-report.json",
]

REPORT_UPLOAD_SNIPPETS = [
    "upload-artifact",
    "dist/",
    "release/real-ga-evidence-audit-1.11.8.json",
    "release/ios-real-device-evidence-import-report.json",
    "release/tri-app-live-smoke-evidence-import-report.json",
]


def _read_existing(paths: list[Path]) -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in paths if p.exists() and p.is_file())


def check(root: Path, *, require_real_workflows: bool = False) -> list[str]:
    issues: list[str] = []
    workflow_paths = sorted((root / ".github" / "workflows").glob("*.yml")) + sorted((root / ".github" / "workflows").glob("*.yaml"))
    release_workflow = root / ".github" / "workflows" / "release.yml"
    fallback_paths = [root / "patches" / "v1.11.8-apply.patch", root / "Makefile"]
    if require_real_workflows:
        if not release_workflow.exists():
            return ["real workflow mode requires .github/workflows/release.yml"]
        text = _read_existing([release_workflow])
    else:
        text = _read_existing(workflow_paths + fallback_paths)
    if not text:
        issues.append("no workflow, Makefile, or v1.11.8 patch content found for governance validation")
        return issues
    for snippet in REQUIRED_SNIPPETS:
        if snippet not in text:
            issues.append(f"workflow governance missing required snippet: {snippet}")
    # Upload-artifact evidence is required in real workflows.
    if workflow_paths or require_real_workflows:
        for snippet in REPORT_UPLOAD_SNIPPETS:
            if snippet not in text:
                issues.append(f"workflow evidence upload missing snippet: {snippet}")
    if require_real_workflows:
        for snippet in ("release_metadata", "attestation", "write_slsa_provenance.py"):
            if snippet not in text:
                issues.append(f"real workflow governance missing release metadata/attestation snippet: {snippet}")
    return issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify release workflows wire tri-app and real-device GA evidence gates.")
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--require-real-workflows", action="store_true")
    args = parser.parse_args(argv)
    issues = check(Path(args.root), require_real_workflows=args.require_real_workflows)
    if issues:
        for issue in issues:
            print(issue, file=sys.stderr)
        return 1
    print("workflow governance contract verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
