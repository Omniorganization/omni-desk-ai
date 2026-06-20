#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path


SECURITY_SNIPPETS = [
    "github/codeql-action/init@411bbbe57033eedfc1a82d68c01345aa96c737d7",
    "github/codeql-action/analyze@411bbbe57033eedfc1a82d68c01345aa96c737d7",
    "actions/dependency-review-action@a1d282b36b6f3519aa1f3fc636f609c47dddb294",
    "gitleaks/gitleaks/v8@v8.28.0",
    "gitleaks\" detect --source . --no-git --redact --verbose --config .gitleaks.toml",
    "scripts/check_license_policy.py --lockfile requirements.dev.lock --policy release/license-policy.json",
    "scripts/check_security_workflow_policy.py .",
    "security-events: write",
    "pull-requests: read",
]

DOCKER_SCAN_SNIPPETS = [
    "aquasecurity/trivy-action@ed142fd0673e97e23eac54620cfb913e5ce36c25",
    "severity: HIGH,CRITICAL",
    'exit-code: "1"',
]

REQUIRED_FILES = [
    ".github/workflows/security.yml",
    ".github/workflows/docker-scan.yml",
    ".gitleaks.toml",
    "release/license-policy.json",
    "scripts/check_license_policy.py",
]


def _missing_snippets(path: Path, snippets: list[str]) -> list[str]:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    return [snippet for snippet in snippets if snippet not in text]


def check(root: Path) -> list[str]:
    issues: list[str] = []
    for rel in REQUIRED_FILES:
        if not (root / rel).exists():
            issues.append(f"missing security policy asset: {rel}")
    missing_security = _missing_snippets(root / ".github" / "workflows" / "security.yml", SECURITY_SNIPPETS)
    if missing_security:
        issues.append("security.yml missing snippets: " + ", ".join(missing_security))
    missing_docker = _missing_snippets(root / ".github" / "workflows" / "docker-scan.yml", DOCKER_SCAN_SNIPPETS)
    if missing_docker:
        issues.append("docker-scan.yml missing snippets: " + ", ".join(missing_docker))
    return issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify security workflows include CodeQL, secret scanning, dependency review, license policy, and container scanning gates.")
    parser.add_argument("root", nargs="?", default=".")
    args = parser.parse_args(argv)
    issues = check(Path(args.root))
    if issues:
        for issue in issues:
            print(issue, file=sys.stderr)
        return 1
    print("security workflow policy verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
