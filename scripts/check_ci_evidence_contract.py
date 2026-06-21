#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path


REQUIRED_CI_SNIPPETS = [
    "reports/ci/${{ matrix.python-version }}",
    "ruff.txt",
    "pyright.txt",
    "pytest-unit.txt",
    "pytest-coverage.txt",
    "coverage-gates.txt",
    "optional-connector-coverage.txt",
    "coverage.json",
    "coverage.xml",
    "scripts/write_ci_evidence_manifest.py",
    "ci-evidence.json",
    "ci-evidence-${{ matrix.python-version }}",
    "--artifact-name \"ci-evidence-${{ matrix.python-version }}\"",
    "actions/upload-artifact@",
]

REQUIRED_WRITER_SNIPPETS = [
    '"job_result": "success"',
    '"artifacts": [',
    '"expected_paths": [',
]


def check(root: Path) -> list[str]:
    issues: list[str] = []
    workflow = root / ".github" / "workflows" / "ci.yml"
    writer = root / "scripts" / "write_ci_evidence_manifest.py"
    if not workflow.exists():
        return ["missing CI workflow: .github/workflows/ci.yml"]
    if not writer.exists():
        issues.append("missing CI evidence writer: scripts/write_ci_evidence_manifest.py")
        writer_text = ""
    else:
        writer_text = writer.read_text(encoding="utf-8")
    text = workflow.read_text(encoding="utf-8")
    for snippet in REQUIRED_CI_SNIPPETS:
        if snippet not in text:
            issues.append(f"CI evidence workflow missing snippet: {snippet}")
    if "--cov-report=json" not in text or "--cov-report=xml" not in text:
        issues.append("CI must emit both coverage JSON and coverage XML reports")
    if "set -o pipefail" not in text or "| tee" not in text:
        issues.append("CI evidence logs must be captured without masking command failures")
    for snippet in REQUIRED_WRITER_SNIPPETS:
        if snippet not in writer_text:
            issues.append(f"CI evidence writer missing snippet: {snippet}")
    return issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify CI uploads source-trunk evidence artifacts for every Python matrix cell.")
    parser.add_argument("root", nargs="?", default=".")
    args = parser.parse_args(argv)
    issues = check(Path(args.root))
    if issues:
        for issue in issues:
            print(issue, file=sys.stderr)
        return 1
    print("CI evidence contract verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
