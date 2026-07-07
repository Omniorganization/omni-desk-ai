#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


LOCKFILES = [
    "requirements.lock",
    "requirements.bootstrap.lock",
    "requirements.runtime.lock",
    "requirements.dev.lock",
    "requirements.security.lock",
    "requirements.enterprise.lock",
]

PRODUCTION_SURFACES = [
    "Dockerfile",
    ".github/workflows/release.yml",
    ".github/workflows/promote-production.yml",
    ".github/workflows/release-policy.yml",
    ".github/workflows/deploy-staging.yml",
    ".github/workflows/rollback-drill.yml",
    ".github/workflows/soak-test.yml",
    ".github/workflows/supply-chain.yml",
    "scripts/build_release.sh",
    "scripts/release_smoke_locked.sh",
]

FORBIDDEN_INSTALL_RE = re.compile(r"\bpip\s+install\b.*\.\[(?:all|prod|production|enterprise)\]", re.IGNORECASE)
PIP_INSTALL_RE = re.compile(r"\bpip\s+install\b", re.IGNORECASE)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _production_install_issues(rel: str, text: str) -> list[str]:
    issues: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not PIP_INSTALL_RE.search(stripped):
            continue
        if FORBIDDEN_INSTALL_RE.search(stripped):
            issues.append(f"{rel}:{lineno} forbids dependency-resolving package extras install")
            continue
        if "--upgrade pip" in stripped:
            continue
        if "--require-hashes" in stripped and "requirements." in stripped and ".lock" in stripped:
            continue
        if "--no-deps" in stripped and "--no-build-isolation" in stripped:
            continue
        if "--no-deps" in stripped and "*.whl" in stripped:
            continue
        issues.append(f"{rel}:{lineno} production install must use a hash lockfile or --no-deps artifact install")
    return issues


def check(root: Path) -> list[str]:
    issues: list[str] = []
    for lockfile in LOCKFILES:
        if not (root / lockfile).exists():
            issues.append(f"missing lockfile: {lockfile}")

    dockerfile = _read(root / "Dockerfile")
    for snippet in (
        "--require-hashes -r /tmp/requirements.bootstrap.lock",
        "--require-hashes -r /tmp/requirements.runtime.lock",
        "--require-hashes -r /tmp/requirements.enterprise.lock",
        "python -m pip install --no-cache-dir --no-deps /tmp/*.whl",
    ):
        if snippet not in dockerfile:
            issues.append(f"Dockerfile missing production locked install snippet: {snippet}")
    if ".[" in dockerfile:
        issues.append("Dockerfile must not install package extras directly")

    release_script = _read(root / "scripts" / "build_release.sh")
    for snippet in (
        '"schema_version": "omnidesk-lockfile-sbom/v1"',
        "requirements.lock",
        "requirements.runtime.lock",
        "requirements.bootstrap.lock",
        "requirements.enterprise.lock",
        "lockfile_sha256",
    ):
        if snippet not in release_script:
            issues.append(f"build_release.sh must generate SBOM from lockfiles: missing {snippet}")

    security = _read(root / ".github" / "workflows" / "security.yml")
    for lockfile in LOCKFILES:
        if f"python scripts/check_lock_hashes.py {lockfile}" not in security:
            issues.append(f"security.yml must hash-check lockfile: {lockfile}")
        if f"pip-audit --disable-pip -r {lockfile}" not in security:
            issues.append(f"security.yml must pip-audit lockfile: {lockfile}")

    for rel in PRODUCTION_SURFACES:
        issues.extend(_production_install_issues(rel, _read(root / rel)))

    release_workflow = _read(root / ".github" / "workflows" / "release.yml")
    release_policy = _read(root / ".github" / "workflows" / "release-policy.yml")
    if "scripts/check_production_install_policy.py ." not in release_workflow:
        issues.append("release.yml must run check_production_install_policy.py")
    if "scripts/check_production_install_policy.py ." not in release_policy:
        issues.append("release-policy.yml must run check_production_install_policy.py")
    return issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify production and release installs are lockfile-backed and release SBOM is generated from lockfiles.")
    parser.add_argument("root", nargs="?", default=".")
    args = parser.parse_args(argv)
    issues = check(Path(args.root))
    if issues:
        for issue in issues:
            print(issue, file=sys.stderr)
        return 1
    print("production install policy verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
