#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path


REQUIRED_CI_SNIPPETS = [
    "concurrency:",
    "cancel-in-progress: true",
    "governance:",
    "postgres-integration:",
    "postgres:16-alpine",
    "tests/test_postgres_appsync_atomic_chat.py",
    'matrix.python-version == \'3.11\'',
    'matrix.python-version != \'3.11\'',
    "--cov-fail-under=80",
    "scripts/check_coverage_gates.py",
    "scripts/check_optional_connector_coverage.py",
    "scripts/write_ci_evidence_manifest.py",
    'ci-evidence-${{ matrix.python-version }}',
    '--artifact-name "ci-evidence-${{ matrix.python-version }}"',
    "actions/upload-artifact@",
    "needs: [governance, test, postgres-integration]",
    "CI matrix passed",
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
    text = workflow.read_text(encoding="utf-8")
    writer_text = writer.read_text(encoding="utf-8") if writer.exists() else ""
    if not writer.exists():
        issues.append("missing CI evidence writer: scripts/write_ci_evidence_manifest.py")
    for snippet in REQUIRED_CI_SNIPPETS:
        if snippet not in text:
            issues.append(f"CI workflow missing industrial evidence snippet: {snippet}")
    if text.count("--cov=omnidesk_agent") != 1:
        issues.append("full coverage suite must run exactly once, not once per compatibility cell")
    if text.count("python scripts/check_version_consistency.py .") != 1:
        issues.append("repository governance checks must run once in the governance job")
    if "set -o pipefail" not in text or "| tee" not in text:
        issues.append("CI evidence logs must be captured without masking command failures")
    for snippet in REQUIRED_WRITER_SNIPPETS:
        if snippet not in writer_text:
            issues.append(f"CI evidence writer missing snippet: {snippet}")
    return issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify split governance, compatibility, PostgreSQL, and evidence CI gates.")
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
