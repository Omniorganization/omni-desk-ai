#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path


SECURITY_SNIPPETS = [
    "github/codeql-action/init@411bbbe57033eedfc1a82d68c01345aa96c737d7",
    "github/codeql-action/analyze@411bbbe57033eedfc1a82d68c01345aa96c737d7",
    "actions/dependency-review-action@a1d282b36b6f3519aa1f3fc636f609c47dddb294",
    "github.com/zricethezav/gitleaks/v8@v8.28.0",
    "gitleaks\" detect --source . --no-git --redact --verbose --config .gitleaks.toml",
    "scripts/check_license_policy.py --lockfile requirements.dev.lock --policy release/license-policy.json",
    "scripts/check_security_workflow_policy.py .",
    "scripts/check_security_attack_surface.py .",
    "security-events: write",
    "pull-requests: read",
    "allow-ghsas: GHSA-wrw7-89jp-8q8g",
]

ATTACK_SURFACE_WORKFLOW_SNIPPETS = [
    "name: Security Attack Surface Gate",
    "security-attack-surface:",
    "python scripts/check_security_attack_surface.py . --write-report dist/evidence/security-attack-surface.json",
    "tests/test_admin_auth.py",
    "tests/test_webhook_forced_signatures.py",
    "tests/test_device_signed_requests.py",
    "tests/test_agent_run_abuse_limits.py",
    "tests/test_files_path_escape_strict.py",
    "tests/test_browser_security.py",
]

LOCKFILES = [
    "requirements.lock",
    "requirements.runtime.lock",
    "requirements.bootstrap.lock",
    "requirements.dev.lock",
    "requirements.security.lock",
    "requirements.enterprise.lock",
]

DOCKER_SCAN_SNIPPETS = [
    "aquasecurity/trivy-action@ed142fd0673e97e23eac54620cfb913e5ce36c25",
    "severity: HIGH,CRITICAL",
    'exit-code: "1"',
]

REQUIRED_FILES = [
    ".github/workflows/security.yml",
    ".github/workflows/security-attack-surface.yml",
    ".github/workflows/docker-scan.yml",
    ".gitleaks.toml",
    "release/license-policy.json",
    "scripts/check_license_policy.py",
    "scripts/check_security_attack_surface.py",
]


def _missing_snippets(path: Path, snippets: list[str]) -> list[str]:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    return [snippet for snippet in snippets if snippet not in text]


def check(root: Path) -> list[str]:
    issues: list[str] = []
    for rel in REQUIRED_FILES:
        if not (root / rel).exists():
            issues.append(f"missing security policy asset: {rel}")
    security_workflow = root / ".github" / "workflows" / "security.yml"
    missing_security = _missing_snippets(security_workflow, SECURITY_SNIPPETS)
    if missing_security:
        issues.append("security.yml missing snippets: " + ", ".join(missing_security))
    attack_surface_workflow = root / ".github" / "workflows" / "security-attack-surface.yml"
    missing_attack_surface = _missing_snippets(attack_surface_workflow, ATTACK_SURFACE_WORKFLOW_SNIPPETS)
    if missing_attack_surface:
        issues.append("security-attack-surface.yml missing snippets: " + ", ".join(missing_attack_surface))
    security_text = security_workflow.read_text(encoding="utf-8") if security_workflow.exists() else ""
    for lockfile in LOCKFILES:
        if f"python scripts/check_lock_hashes.py {lockfile}" not in security_text:
            issues.append(f"security.yml must hash-check lockfile: {lockfile}")
        if f"pip-audit --disable-pip -r {lockfile}" not in security_text:
            issues.append(f"security.yml must pip-audit lockfile: {lockfile}")
    dependency_review_index = security_text.find("actions/dependency-review-action@")
    if dependency_review_index >= 0:
        dependency_review_block = security_text[dependency_review_index:dependency_review_index + 300]
        if "continue-on-error: true" in dependency_review_block:
            issues.append("dependency-review must be blocking; remove continue-on-error: true")
    allow_ghsa_lines = [
        line.strip()
        for line in security_text.splitlines()
        if line.strip().startswith("allow-ghsas:")
    ]
    if allow_ghsa_lines != ["allow-ghsas: GHSA-wrw7-89jp-8q8g"]:
        issues.append("dependency-review allow-ghsas must stay limited to GHSA-wrw7-89jp-8q8g")
    missing_docker = _missing_snippets(root / ".github" / "workflows" / "docker-scan.yml", DOCKER_SCAN_SNIPPETS)
    if missing_docker:
        issues.append("docker-scan.yml missing snippets: " + ", ".join(missing_docker))
    return issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify security workflows include CodeQL, secret scanning, dependency review, license policy, container scanning, and attack-surface gates.")
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
